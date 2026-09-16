#!/usr/bin/env python3
"""Drag & drop web UI for the PDF -> EPUB pipeline in pdf2epub.py.

Drop PDFs on the page, pick an output folder, press Convert. Everything runs
locally: a small http.server on localhost serves the page and starts the
conversion in a background thread.

Must be started with the Python of the marker virtual environment (it needs
marker-pdf, torch, pypdfium2 and Pillow), for example:

    C:\\Users\\<you>\\marker_env\\Scripts\\python.exe pdf2epub_ui.py

or just double-click start_ui.bat next to this file.
"""

from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf2epub as pipeline  # noqa: E402

DEFAULT_PORT = 8765
MAX_LOG_LINES = 5000
REQUIRED_MODULES = ("marker", "pypdfium2", "PIL")


# --------------------------------------------------------------------------- #
# Job bookkeeping
# --------------------------------------------------------------------------- #


class Job:
    """State of the conversion run, shared with the browser through polling."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.status = "idle"  # idle | running | done | failed | cancelled
        self.results: list[dict] = []
        self.active = ""
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def log(self, message: str) -> None:
        with self.lock:
            self.lines.append(message)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def snapshot(self, since: int) -> dict:
        with self.lock:
            start = since if 0 <= since <= len(self.lines) else 0
            return {
                "status": self.status,
                "lines": self.lines[start:],
                "next": len(self.lines),
                "results": list(self.results),
                "active": self.active,
            }

    def reset(self) -> None:
        with self.lock:
            self.lines.clear()
            self.results.clear()
            self.active = ""


JOB = Job()
UPLOAD_LOCK = threading.Lock()


def uploads_directory() -> Path:
    directory = pipeline.work_directory(None) / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def epub_entry_url(epub_path: str | Path, entry: str = "") -> str:
    """URL that serves a file from inside an EPUB (cover, chapter preview, ...)."""
    url = "/api/epub-file?path=" + urllib.parse.quote(str(epub_path))
    if entry:
        url += "&entry=" + urllib.parse.quote(entry)
    return url


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


# --------------------------------------------------------------------------- #
# Uploads and dialogs
# --------------------------------------------------------------------------- #


def safe_upload_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if not name.lower().endswith((".pdf", ".epub")):
        name = f"{name}.pdf"
    return name


def store_upload(name: str, data: bytes) -> Path:
    target = uploads_directory() / safe_upload_name(name)
    with UPLOAD_LOCK:
        target.write_bytes(data)
    return target


DIALOG_SNIPPET = """
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
kind = {kind!r}
if kind == "dir":
    path = filedialog.askdirectory(title="Choose the folder for your EPUB files")
else:
    path = filedialog.askopenfilename(
        title="Choose a cover image",
        filetypes=[("Images", "*.jpg *.jpeg *.png *.gif *.webp"), ("All files", "*.*")],
    )
print(path or "")
"""


def ask_native_dialog(kind: str) -> str:
    """Open the platform file/folder picker in a short-lived child process."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", DIALOG_SNIPPET.format(kind=kind)],
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=pipeline.CREATE_NO_WINDOW,
        )
    except Exception:  # pragma: no cover - depends on the desktop
        return ""
    return (result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else ""


def open_in_file_manager(path: str) -> None:
    target = Path(path)
    if os.name == "nt":
        if target.is_dir():
            os.startfile(str(target))  # noqa: S606
        else:
            subprocess.Popen(["explorer", f"/select,{target}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])


# --------------------------------------------------------------------------- #
# Running the conversion
# --------------------------------------------------------------------------- #


def analyze_upload(path: Path) -> dict:
    """Describe a freshly uploaded file so the UI can offer the right action."""
    base = {"path": str(path), "name": path.name, "size": path.stat().st_size,
            "kind": "", "description": "", "title": "", "author": "", "stored": "",
            "cover_url": "", "toc_count": 0, "toc_error": ""}

    if path.suffix.lower() == ".epub":
        try:
            title, author = pipeline.read_epub_metadata(path)
            report = pipeline.inspect_epub(path)
        except Exception as error:
            return {**base, "kind": "epub",
                    "description": f"could not read this EPUB ({error})"}
        cover = pipeline.read_epub_cover(path)
        try:
            toc = pipeline.read_epub_toc(path)
            toc_count, toc_error = len(toc["entries"]), ""
        except Exception as error:
            toc_count, toc_error = 0, str(error)
        description = f"EPUB, {report['chapters']} chapters" if report["chapters"] else "EPUB"
        if cover:
            description += ", has a cover"
        return {
            **base,
            "kind": "epub",
            "description": description,
            "title": title,
            "author": author,
            "stored": f"stored in the file: {title or '(no title)'} \u2014 {author or '(no author)'}",
            "cover_entry": cover[2] if cover else "",
            "toc_count": toc_count,
            "toc_error": toc_error,
        }

    try:
        info = pipeline.read_pdf_info(path)
    except Exception as error:
        return {**base, "kind": "digital",
                "description": f"could not read the PDF ({error})",
                "title": pipeline.title_from_filename(path.stem)}
    return {
        **base,
        "kind": pipeline.classify_pdf(info),
        "description": info.describe(),
        "title": info.meta_title or pipeline.title_from_filename(path.stem),
        "author": info.meta_author,
    }


def run_job(config: dict) -> None:
    files = config.get("files", [])
    output_dir = Path(config.get("output_dir", ""))
    options = config.get("options", {})
    cover = Path(options["cover"]) if options.get("cover") else None
    should_stop = JOB.stop_event.is_set

    uploaded_root = uploads_directory().resolve()
    total = len(files)
    failures = 0
    for index, entry in enumerate(files, start=1):
        if JOB.stop_event.is_set():
            break
        source = Path(entry.get("path", "")).resolve()
        # Only convert files that were uploaded through the UI.
        if uploaded_root not in source.parents or not source.exists():
            JOB.log(f"[{index}/{total}] !! skipped unknown file: {source}")
            failures += 1
            continue
        JOB.active = entry.get("name", source.name)
        JOB.log(f"[{index}/{total}] {source.name}")
        try:
            title = (entry.get("title") or "").strip() or None
            author = (entry.get("author") or "").strip() or None
            if options.get("action") == "toc":
                # An edited table of contents is written back to a copy of the book.
                toc = [
                    pipeline.TocEntry(
                        label=str(item.get("label", "")),
                        href=str(item.get("href", "")),
                        depth=int(item.get("depth") or 1),
                    )
                    for item in entry.get("toc", [])
                ]
                result = pipeline.write_epub_toc(source, output_dir, toc, log=JOB.log)
            elif source.suffix.lower() == ".epub":
                # An EPUB in: only its title/author/cover are rewritten.
                result = pipeline.update_epub_metadata(
                    source, output_dir, title=title, author=author, cover=cover, log=JOB.log,
                )
            else:
                result = pipeline.convert(
                    pipeline.ConvertOptions(
                        pdf=source,
                        output_dir=output_dir,
                        title=title,
                        author=author,
                        cover=cover,
                        kind=entry.get("kind") or options.get("kind") or "auto",
                        fast_ocr=bool(options.get("fast_ocr")),
                        split_level=options.get("split_level") or None,
                        toc_tables_to_lists=options.get("toc_tables", True),
                        rebuild_only=bool(options.get("rebuild_only"))
                        or bool(options.get("metadata_only")),
                    ),
                    log=JOB.log,
                    should_stop=should_stop,
                )
            JOB.results.append(
                {
                    "name": source.name,
                    "epub": str(result["epub"]),
                    "book_folder": str(result.get("book_folder", "")),
                    "kind": result["kind"],
                    "mode": result.get("mode", "convert"),
                    "title": result.get("title", ""),
                    "author": result.get("author", ""),
                    "cover": bool(result.get("cover", False)),
                    "cover_entry": result.get("cover_entry", ""),
                    "chapters": result["chapters"],
                    "toc_entries": result["toc_entries"],
                    "images": result["images"],
                    "size_mb": result["size_mb"],
                    "error": "",
                }
            )
        except pipeline.ConversionError as error:
            failures += 1
            JOB.log(f"!! {error}")
            JOB.results.append(
                {"name": source.name, "epub": "", "book_folder": "", "error": str(error)}
            )
        except Exception as error:  # keep going with the remaining files
            failures += 1
            JOB.log(f"!! unexpected error: {type(error).__name__}: {error}")
            JOB.results.append(
                {"name": source.name, "epub": "", "book_folder": "", "error": str(error)}
            )
        finally:
            JOB.active = ""

    JOB.active = ""
    if JOB.stop_event.is_set():
        JOB.status = "cancelled"
        JOB.log("== stopped")
    elif failures:
        JOB.status = "failed"
        JOB.log(f"== finished with {failures} of {total} file(s) failing")
    else:
        JOB.status = "done"
        JOB.log(f"== finished: {total} file(s)")


def start_job(config: dict) -> None:
    JOB.reset()
    JOB.stop_event.clear()
    JOB.status = "running"
    JOB.log(f"output folder: {config.get('output_dir')}")
    JOB.thread = threading.Thread(target=run_job, args=(config,), daemon=True)
    JOB.thread.start()


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "pdf2epub-ui"

    def log_message(self, *args, **kwargs) -> None:  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------- #
    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", code)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # -- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            query = urllib.parse.parse_qs(parsed.query)
            since = int((query.get("since") or ["0"])[0])
            self._send_json(JOB.snapshot(since))
            return
        if parsed.path == "/api/info":
            self._send_json(
                {
                    "python": sys.executable,
                    "pandoc": shutil.which("pandoc") or pipeline.find_pandoc(),
                    "llama_server": pipeline.find_llama_server() or "",
                    "work_dir": str(pipeline.work_directory(None)),
                    "uploads_dir": str(uploads_directory()),
                    "default_output_dir": str(Path.home() / "Documents" / "epub"),
                }
            )
            return
        if parsed.path == "/api/image":
            # Preview a cover image straight from disk (the cover field is a path).
            query = urllib.parse.parse_qs(parsed.query)
            candidate = Path((query.get("path") or [""])[0])
            if not candidate.is_file() or candidate.suffix.lower() not in IMAGE_SUFFIXES:
                self._send_json({"error": "not an image file"}, 404)
                return
            self._send(candidate.read_bytes(),
                       mimetypes.guess_type(candidate.name)[0] or "image/jpeg")
            return
        if parsed.path == "/api/epub-file":
            # Serve a file from inside an EPUB: cover thumbnails and the chapter
            # preview pane load through this, so relative css/image links work.
            query = urllib.parse.parse_qs(parsed.query)
            book = Path((query.get("path") or [""])[0])
            entry = (query.get("entry") or [""])[0]
            if not book.is_file() or book.suffix.lower() != ".epub" or not entry:
                self._send_json({"error": "needs an EPUB and an entry"}, 400)
                return
            try:
                with zipfile.ZipFile(book) as archive:
                    data = archive.read(entry)
            except Exception:
                self._send_json({"error": f"no {entry} in this book"}, 404)
                return
            self._send(data, mimetypes.guess_type(entry)[0] or "application/octet-stream")
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route == "/api/upload":
            query = urllib.parse.parse_qs(parsed.query)
            name = (query.get("name") or ["upload.pdf"])[0]
            data = self._read_body()
            if not data:
                self._send_json({"error": "empty upload"}, 400)
                return
            path = store_upload(name, data)
            self._send_json(analyze_upload(path))
            return
        if route == "/api/browse":
            payload = self._read_json()
            self._send_json({"path": ask_native_dialog(payload.get("kind", "dir"))})
            return
        if route == "/api/open":
            payload = self._read_json()
            try:
                open_in_file_manager(payload.get("path", ""))
                self._send_json({"ok": True})
            except Exception as error:
                self._send_json({"error": str(error)}, 500)
            return
        if route == "/api/start":
            if JOB.status == "running":
                self._send_json({"error": "a conversion is already running"}, 409)
                return
            payload = self._read_json()
            if not payload.get("output_dir") or not payload.get("files"):
                self._send_json({"error": "an output folder and at least one file are required"}, 400)
                return
            start_job(payload)
            self._send_json({"ok": True})
            return
        if route == "/api/save":
            # Save the title/author of a single file without re-running the extraction.
            if JOB.status == "running":
                self._send_json({"error": "a job is already running"}, 409)
                return
            payload = self._read_json()
            if not payload.get("path"):
                self._send_json({"error": "no file given"}, 400)
                return
            if not payload.get("output_dir"):
                self._send_json({"error": "choose an output folder first"}, 400)
                return
            start_job({
                "output_dir": payload["output_dir"],
                "options": {"metadata_only": True, "cover": payload.get("cover", "")},
                "files": [payload],
            })
            self._send_json({"ok": True})
            return
        if route == "/api/toc":
            # Read a book's chapter menu for the editor.
            payload = self._read_json()
            source = Path(payload.get("path", ""))
            if not source.is_file() or source.suffix.lower() != ".epub":
                self._send_json({"error": "no EPUB given"}, 400)
                return
            try:
                toc = pipeline.read_epub_toc(source)
            except Exception as error:
                self._send_json({"error": str(error)}, 400)
                return
            entries = []
            for entry in toc["entries"]:
                target, _, anchor = entry.href.partition("#")
                resolved = posixpath.normpath(posixpath.join(toc["base"], target)) if target else ""
                entries.append({
                    "label": entry.label,
                    "href": entry.href,
                    "depth": entry.depth,
                    "preview": epub_entry_url(source, resolved) + (f"#{anchor}" if anchor else ""),
                    "missing": not resolved,
                })
            self._send_json({
                "title": toc["title"],
                "source": toc["source"],
                "path": str(source),
                "name": source.name,
                "entries": entries,
            })
            return
        if route == "/api/toc/save":
            if JOB.status == "running":
                self._send_json({"error": "a job is already running"}, 409)
                return
            payload = self._read_json()
            source = Path(payload.get("path", ""))
            if not source.is_file() or not payload.get("output_dir"):
                self._send_json({"error": "an EPUB and an output folder are required"}, 400)
                return
            entries = [
                {
                    "label": str(item.get("label", "")),
                    "href": str(item.get("href", "")),
                    "depth": int(item.get("depth") or 1),
                }
                for item in payload.get("entries", [])
            ]
            start_job({
                "output_dir": payload["output_dir"],
                "options": {"action": "toc"},
                "files": [{"path": str(source), "name": source.name, "toc": entries}],
            })
            self._send_json({"ok": True})
            return
        if route == "/api/stop":
            JOB.stop_event.set()
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, 404)


def check_environment() -> bool:
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if not missing:
        return True
    print(
        "ERROR: this Python is missing " + ", ".join(missing) + "\n"
        f"       running interpreter: {sys.executable}\n\n"
        "Start the UI with the Python of your marker virtual environment, e.g.\n"
        "  C:\\Users\\<you>\\marker_env\\Scripts\\python.exe pdf2epub_ui.py\n"
        "or use start_ui.bat in this folder.",
        file=sys.stderr,
    )
    return False


def main() -> int:
    if not check_environment():
        return 1
    port = int(os.environ.get("PDF2EPUB_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"PDF -> EPUB UI running at {url}")
    print(f"  python:    {sys.executable}")
    print(f"  pandoc:    {shutil.which('pandoc') or pipeline.find_pandoc()}")
    print(f"  llama:     {pipeline.find_llama_server() or 'NOT FOUND (OCR will not work)'}")
    print(f"  work dir:  {pipeline.work_directory(None)}")
    print("Press Ctrl+C to stop the server.")
    if not os.environ.get("PDF2EPUB_NO_BROWSER"):
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF &rarr; EPUB</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9; --panel: #fff; --border: #d9dde3; --text: #1c2024;
    --muted: #6b7280; --accent: #2f6fed; --good: #157f3d; --warn: #a8540a;
    --bad: #c0392b; --log: #14181d; --log-text: #d7e0ea;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181c; --panel:#1e2126; --border:#343941; --text:#e8ebef;
            --muted:#9aa4b1; --accent:#5b8dff; --good:#4ec37f; --warn:#e0a458;
            --bad:#ef7263; --log:#101317; --log-text:#cfd8e3; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  main { max-width: 1080px; margin: 0 auto; padding: 24px 20px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
       color: var(--muted); margin: 26px 0 10px; }
  p.hint { color: var(--muted); margin: 0 0 18px; font-size: 13px; }
  .panel { background: var(--panel); border:1px solid var(--border); border-radius:10px;
           padding:16px; margin-bottom:14px; }
  #drop { border:2px dashed var(--border); border-radius:12px; padding:30px 16px;
          text-align:center; color:var(--muted); transition:.15s; cursor:pointer; }
  #drop.hot { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent);
              color: var(--text); }
  #drop strong { color: var(--text); display:block; font-size:16px; margin-bottom:4px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .row > label { min-width: 150px; font-weight:600; }
  input[type=text], input[type=number], select {
    flex:1; min-width:220px; padding:8px 10px; border:1px solid var(--border);
    border-radius:8px; background:var(--bg); color:var(--text); font:inherit; }
  input.needed { border-color: var(--warn); box-shadow:0 0 0 2px color-mix(in srgb, var(--warn) 25%, transparent); }
  input[type=number] { flex:0 0 110px; min-width:0; }
  button { padding:8px 14px; border-radius:8px; border:1px solid var(--border);
           background:var(--panel); color:var(--text); font:inherit; cursor:pointer; }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  button.ghost { padding:4px 8px; font-size:12px; }
  label.check { display:flex; gap:8px; align-items:flex-start; margin:6px 0; font-weight:400; }
  label.check input { margin-top:3px; }
  label.check span small { display:block; color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border);
           vertical-align:top; font-size:13px; }
  th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
       letter-spacing:.05em; }
  td input { width:100%; padding:5px 7px; font-size:13px; }
  .badge { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px;
           border:1px solid var(--border); color:var(--muted); white-space:nowrap; }
  .badge.scanned, .badge.epub { color:var(--warn); border-color:currentColor; }
  .badge.digital { color:var(--good); border-color:currentColor; }
  .badge.idle { color:var(--muted); }
  .badge.running { color:var(--accent); border-color:currentColor; }
  .badge.done { color:var(--good); border-color:currentColor; }
  .badge.failed, .badge.cancelled { color:var(--bad); border-color:currentColor; }
  #log { background:var(--log); color:var(--log-text); border-radius:10px; padding:12px;
         height:340px; overflow:auto; font:12px/1.45 ui-monospace, Consolas, monospace;
         white-space:pre-wrap; word-break:break-word; margin:0; }
  .meta { color:var(--muted); font-size:12px; }
  .meta code { color:var(--text); }
  .hidden { display:none; }
  .result { border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:8px; }
  .result.bad { border-color: var(--bad); }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size:12px; word-break:break-all; }
  .filecard { border:1px solid var(--border); border-radius:10px; padding:12px 14px;
              margin-bottom:10px; background:var(--bg); }
  .filecard .cardtop { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
                       margin-bottom:10px; }
  .filecard .cardtop .name { font-weight:600; }
  .filecard .cardtop .spacer { flex:1; }
  .fieldrow { display:flex; gap:10px; align-items:center; margin-top:6px; }
  .fieldrow > label { width:64px; color:var(--muted); font-size:12px; font-weight:600;
                      text-transform:uppercase; letter-spacing:.04em; }
  .fieldrow > input { flex:1; }
  .fieldrow > select { flex:0 0 190px; min-width:0; }
  .filecard .note { margin:8px 0 0; }
  .coverthumb { width:56px; height:84px; object-fit:cover; border:1px solid var(--border);
                border-radius:4px; background:var(--bg); display:block; }
  .coverrow { display:flex; gap:10px; align-items:center; margin-top:10px; }
  .tocgrid { display:grid; grid-template-columns:minmax(340px, 1fr) minmax(340px, 1fr);
             gap:14px; margin-top:12px; }
  @media (max-width: 980px) { .tocgrid { grid-template-columns:1fr; } }
  .toclist { max-height:520px; overflow:auto; border:1px solid var(--border);
             border-radius:8px; padding:6px; background:var(--bg); }
  .tocrow { display:flex; gap:4px; align-items:center; padding:2px 4px;
            border-radius:6px; }
  .tocrow.active { background:color-mix(in srgb, var(--accent) 16%, transparent); }
  .tocrow input { flex:1; min-width:90px; padding:4px 6px; font-size:13px; }
  .tocrow button { padding:2px 7px; font-size:12px; line-height:1.2; }
  .tocrow .level { color:var(--muted); font-size:11px; width:14px; text-align:center; }
  .tocrow .target { color:var(--muted); font-size:11px; max-width:120px; overflow:hidden;
                    text-overflow:ellipsis; white-space:nowrap; }
  .tocrow.missing input { border-color:var(--warn); }
  #tocFrame { width:100%; height:520px; border:1px solid var(--border);
              border-radius:8px; background:#fff; }
  .tochead { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
</style>
</head>
<body>
<main>
  <h1>PDF &rarr; EPUB</h1>
  <p class="hint">
    Drop PDF books (or EPUBs whose title/author needs fixing) below, choose where
    the EPUBs should go, press Convert. Scanned PDFs are re-OCR'd automatically;
    digital PDFs are read directly. Long books take a while - the log shows progress.
  </p>

  <div class="panel">
    <div id="drop">
      <strong>Drop PDF or EPUB files here</strong>
      or click to choose files &middot; every file is processed one after another
    </div>
    <input type="file" id="picker" accept="application/pdf,.pdf,application/epub+zip,.epub"
           multiple class="hidden">

    <div id="fileWrap" class="hidden" style="margin-top:16px">
      <div id="fileCards"></div>
      <p class="meta" id="detected"></p>
      <p class="hint" style="margin:12px 0 0">
        <strong>Title</strong> and <strong>Author</strong> are written into the EPUB
        metadata, and the title is also the EPUB's filename. They are prefilled from the
        file, so you normally only need to correct them: edit the fields, then press
        <em>Convert</em> (or <em>save metadata</em> on a single book) to write them into
        the EPUB. For an EPUB input only that metadata - plus the cover image below, if
        you set one - is rewritten; the book itself is copied over unchanged. Leave a
        field empty to keep what the file already has.
      </p>
    </div>
  </div>

  <h2>Output</h2>
  <div class="panel">
    <div class="row" style="margin-bottom:10px">
      <label for="outDir">Output folder</label>
      <input type="text" id="outDir" placeholder="e.g. G:\My Drive\_Books\Conversions">
      <button id="browseOut">Browse&hellip;</button>
    </div>
    <div class="row">
      <label for="cover">Cover image</label>
      <input type="text" id="cover" placeholder="optional - jpg/png: sets the cover of a PDF conversion, adds or replaces the cover of an EPUB">
      <button id="browseCover">Browse&hellip;</button>
      <button id="clearCover" class="ghost">clear</button>
    </div>
    <div class="coverrow hidden" id="coverPreviewWrap">
      <img id="coverPreview" class="coverthumb" alt="cover preview">
      <span class="meta" id="coverPreviewText"></span>
    </div>
  </div>

  <h2>Options</h2>
  <div class="panel">
    <label class="check">
      <input type="checkbox" id="fastOcr">
      <span>Fast OCR (scanned PDFs only)
        <small>About 2x faster, but pictures inside the scanned pages are not
        extracted. Layout-aware OCR is used otherwise.</small></span>
    </label>
    <label class="check">
      <input type="checkbox" id="tocTables" checked>
      <span>Turn printed tables of contents into lists
        <small>A TOC that marker reads as a table reflows badly on an e-reader, and its
        page numbers are meaningless anyway.</small></span>
    </label>
    <label class="check">
      <input type="checkbox" id="rebuildOnly">
      <span>Rebuild the EPUB only (no extraction)
        <small>Reuses the markdown already in the output folder - use it after editing
        the .md by hand. Needs a previous run in the same output folder.</small></span>
    </label>
    <div class="row" style="margin-top:10px">
      <label for="splitLevel">Chapter split level</label>
      <input type="number" id="splitLevel" min="1" max="6" placeholder="auto">
      <span class="meta">Heading level that starts a new chapter file. Leave empty to detect it.</span>
    </div>
  </div>

  <div class="row" style="margin:18px 0 6px">
    <button id="start" class="primary" disabled>Convert</button>
    <button id="stop" disabled>Stop</button>
    <span id="status" class="badge idle">idle</span>
    <span id="active" class="meta"></span>
  </div>
  <p class="hint" id="startHint" style="margin:0 0 14px"></p>

  <div id="tocPanel" class="panel hidden">
    <div class="tochead">
      <h2 style="margin:0">Table of contents</h2>
      <span class="meta" id="tocBook"></span>
      <span style="flex:1"></span>
      <span class="badge idle" id="tocStatus">not saved yet</span>
      <button id="tocSave" class="primary">Save to EPUB</button>
      <button id="tocReset">Undo my changes</button>
      <button id="tocClose">Close</button>
    </div>
    <p class="hint" style="margin:10px 0 0">
      <strong>&laquo; &raquo;</strong> change how deep a line sits,
      <strong>&#8593; &#8595;</strong> move it (together with everything nested under it),
      the text field is what the reader's menu shows, and <strong>show</strong> opens that
      page in the preview. Saving writes a new copy into your output folder; the chapter
      files and the book's text are never touched, only the menu.
    </p>
    <div class="tocgrid">
      <div class="toclist" id="tocList"></div>
      <div>
        <div class="row" style="margin-bottom:6px">
          <span class="meta" id="tocPreviewLabel">pick a line to preview its page</span>
        </div>
        <iframe id="tocFrame" title="chapter preview"></iframe>
      </div>
    </div>
  </div>

  <div id="results"></div>
  <pre id="log">Ready. Waiting for PDFs.</pre>
  <p class="meta" id="env"></p>
</main>

<script>
const $ = (id) => document.getElementById(id);
const state = { files: [], polling: null, logOffset: 0, running: false, toc: null, tocRow: -1 };

function fmtSize(bytes) {
  if (bytes > 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
  return Math.round(bytes / 1024) + ' KB';
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderFiles() {
  const wrap = $('fileWrap');
  wrap.classList.toggle('hidden', state.files.length === 0);
  $('fileCards').innerHTML = state.files.map((file, index) => {
    const isEpub = file.detected === 'epub';
    const switcher = isEpub
      ? '<span class="badge epub">metadata only</span>'
      : `<select data-field="kind" data-index="${index}" title="pipeline">
           <option value=""${file.kind ? '' : ' selected'}>auto (${esc(file.detected)})</option>
           <option value="digital"${file.kind === 'digital' ? ' selected' : ''}>digital</option>
           <option value="scanned"${file.kind === 'scanned' ? ' selected' : ''}>scanned / OCR</option>
         </select>`;
    const note = isEpub
      ? (file.stored || '') + (file.description ? ' &middot; ' + esc(file.description) : '')
      : esc(file.description || '') + ' &middot; the title is also the EPUB filename';
    const cover = isEpub
      ? `<div class="coverrow">
           ${file.cover_entry
             ? `<img class="coverthumb" alt="cover" src="${esc(epubEntryUrl(file.path, file.cover_entry))}">
                <span class="meta">the cover inside this book</span>`
             : '<span class="meta">no cover in this book yet - set one below (or replace it)</span>'}
         </div>`
      : '';
    const tocButton = isEpub && file.toc_count
      ? `<button class="ghost" data-toc-open="${index}">edit table of contents (${file.toc_count})</button>`
      : '';
    return `<div class="filecard">
      <div class="cardtop">
        <span class="name">${esc(file.name)}</span>
        <span class="meta">${fmtSize(file.size)}</span>
        <span class="badge ${esc(file.detected)}">${esc(file.detected)}</span>
        ${switcher}
        <span class="spacer"></span>
        ${tocButton}
        <button class="ghost" data-save="${index}">save metadata</button>
        <button class="ghost" data-remove="${index}">remove</button>
      </div>
      <div class="fieldrow">
        <label for="t${index}">Title</label>
        <input type="text" id="t${index}" data-field="title" data-index="${index}"
               value="${esc(file.title)}" placeholder="book title">
      </div>
      <div class="fieldrow">
        <label for="a${index}">Author</label>
        <input type="text" id="a${index}" data-field="author" data-index="${index}"
               value="${esc(file.author)}" placeholder="author">
      </div>
      ${cover}
      <p class="meta note">${note}</p>
    </div>`;
  }).join('');
  $('detected').innerHTML = state.files
    .map((file) => `<span class="badge ${esc(file.detected)}">${esc(file.detected)}</span> ${esc(file.name)}: ${esc(file.description || '')}`)
    .join('<br>');
  $('fileCards').querySelectorAll('input[data-field]').forEach((input) => {
    input.addEventListener('input', () => {
      state.files[Number(input.dataset.index)][input.dataset.field] = input.value;
      // Nothing is written to the EPUB until Convert/save metadata is pressed.
      $('startHint').textContent =
        'Unsaved edit - press Convert (or save metadata) to write it into the EPUB.';
    });
  });
  $('fileCards').querySelectorAll('select[data-field]').forEach((select) => {
    select.addEventListener('change', () => {
      state.files[Number(select.dataset.index)].kind = select.value;
    });
  });
  $('fileCards').querySelectorAll('button[data-toc-open]').forEach((button) => {
    button.addEventListener('click', () => openToc(Number(button.dataset.tocOpen)));
  });
  $('fileCards').querySelectorAll('button[data-save]').forEach((button) => {
    button.addEventListener('click', () => saveOne(Number(button.dataset.save)));
  });
  $('fileCards').querySelectorAll('button[data-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      state.files.splice(Number(button.dataset.remove), 1);
      renderFiles();
    });
  });
  updateButtons();
}

function updateButtons() {
  const hasFiles = state.files.length > 0;
  const hasOutput = Boolean($('outDir').value.trim());
  const epubOnly = hasFiles && state.files.every((file) => file.detected === 'epub');
  const label = epubOnly ? 'Save metadata' : 'Convert';
  const missing = [];
  if (!hasFiles) missing.push('drop a PDF or EPUB above');
  if (!hasOutput) missing.push('choose an output folder below');

  $('start').textContent = label;
  $('start').disabled = state.running || missing.length > 0;
  $('outDir').classList.toggle('needed', !hasOutput && hasFiles);
  if (state.running) {
    $('startHint').textContent = 'Working - see the log below. Stop cancels.';
  } else if (missing.length) {
    $('startHint').textContent = 'To enable ' + label.toLowerCase() + ': ' + missing.join(', ') + '.';
  } else {
    $('startHint').textContent = 'Press ' + label +
      ' to write the Title and Author above into the EPUB.';
  }

  $('fileCards').querySelectorAll('button[data-save]').forEach((button) => {
    button.disabled = state.running || !hasOutput;
    button.title = hasOutput
      ? 'Write this book\'s title and author into its EPUB now, without re-running the extraction'
      : 'Choose an output folder first';
  });
  $('fileCards').querySelectorAll('button[data-toc-open]').forEach((button) => {
    button.disabled = state.running;
  });
  $('tocSave').disabled = state.running || !hasOutput || !state.toc;
}

async function upload(fileList) {
  const files = Array.from(fileList).filter((file) => /\.(pdf|epub)$/i.test(file.name));
  if (!files.length) return;
  for (const file of files) {
    appendLog('uploading ' + file.name + ' ...');
    const response = await fetch('/api/upload?name=' + encodeURIComponent(file.name),
                                 { method: 'POST', body: file });
    const info = await response.json();
    if (info.error) { appendLog('!! ' + info.error); continue; }
    state.files.push({ ...info, detected: info.kind, kind: "" });
    appendLog('   ' + info.name + ' -> ' + info.kind + ' (' + (info.description || '') + ')');
    renderFiles();
  }
}

function appendLog(line) {
  const log = $('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent += '\n' + line;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function setStatus(status) {
  const badge = $('status');
  badge.textContent = status;
  badge.className = 'badge ' + status;
}

function renderResults(results) {
  const coverThumb = (result) => (result.cover && result.cover_entry)
    ? `<div class="coverrow"><img class="coverthumb" alt="cover in the saved file"
            src="${esc(epubEntryUrl(result.epub, result.cover_entry))}">
         <span class="meta">the cover in the saved file</span></div>`
    : (result.cover
      ? '<div class="meta">a cover was set (this EPUB tool could not read it back)</div>'
      : '');
  $('results').innerHTML = results.map((result) => result.error
    ? `<div class="result bad"><strong>${esc(result.name)}</strong>
         <div class="meta">failed: ${esc(result.error)}</div></div>`
    : result.mode === 'toc'
    ? `<div class="result">
         <strong>${esc(result.epub.split(/[\\/]/).pop())}</strong>
         <div class="mono">${esc(result.epub)}</div>
         <div class="meta">table of contents saved &middot; ${result.toc_entries} menu entries
           &middot; ${result.chapters} chapters kept &middot; ${result.size_mb} MB</div>
         <div style="margin-top:6px">
           <button class="ghost" data-open="${esc(result.epub)}">show in Explorer</button>
         </div></div>`
    : result.mode === 'metadata'
    ? `<div class="result">
         <strong>${esc(result.epub.split(/[\\/]/).pop())}</strong>
         <div class="mono">${esc(result.epub)}</div>
         <div class="meta">metadata updated${result.cover ? ' &middot; cover set' : ''} &middot;
           title: ${esc(result.title)} &middot; author: ${esc(result.author || '(none)')} &middot;
           ${result.chapters} chapters kept &middot; ${result.size_mb} MB</div>
         ${coverThumb(result)}
         <div style="margin-top:6px">
           <button class="ghost" data-open="${esc(result.epub)}">show in Explorer</button>
         </div></div>`
    : `<div class="result">
         <strong>${esc(result.epub.split(/[\\/]/).pop())}</strong>
         <div class="mono">${esc(result.epub)}</div>
         <div class="meta">${esc(result.kind)} &middot; ${result.chapters} chapters &middot;
           ${result.toc_entries} menu entries &middot; ${result.images} images${result.cover ? ' + cover' : ''} &middot;
           ${result.size_mb} MB</div>
         ${coverThumb(result)}
         <div style="margin-top:6px">
           <button class="ghost" data-open="${esc(result.epub)}">show in Explorer</button>
         </div></div>`).join('');
  $('results').querySelectorAll('button[data-open]').forEach((button) => {
    button.addEventListener('click', () => fetch('/api/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: button.dataset.open }) }));
  });
}

const coverPath = () => $('cover').value.trim();

const options = () => ({
  fast_ocr: $('fastOcr').checked,
  toc_tables: $('tocTables').checked,
  rebuild_only: $('rebuildOnly').checked,
  split_level: $('splitLevel').value ? Number($('splitLevel').value) : null,
  cover: coverPath(),
});

async function start() {
  const payload = {
    output_dir: $('outDir').value.trim(),
    options: options(),
    files: state.files.map((file) => ({
      path: file.path, name: file.name, title: file.title,
      author: file.author, kind: file.kind || 'auto',
    })),
  };
  localStorage.setItem('pdf2epub.output', payload.output_dir);
  const info = await postJson('/api/start', payload);
  if (info.error) { appendLog('!! ' + info.error); return; }
  beginRun();
}

async function saveOne(index) {
  const file = state.files[index];
  const info = await postJson('/api/save', {
    path: file.path, name: file.name, title: file.title, author: file.author,
    kind: file.kind || 'auto', output_dir: $('outDir').value.trim(),
    cover: coverPath(),
  });
  if (info.error) { appendLog('!! ' + info.error); return; }
  appendLog('saving title/author of ' + file.name + ' ...');
  beginRun();
}

// ---------------------------------------------------------------- cover preview

function epubEntryUrl(path, entry) {
  return '/api/epub-file?path=' + encodeURIComponent(path) +
         (entry ? '&entry=' + encodeURIComponent(entry) : '');
}

function updateCoverPreview() {
  const path = coverPath();
  const wrap = $('coverPreviewWrap');
  if (!path) {
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  $('coverPreview').src = '/api/image?path=' + encodeURIComponent(path);
  $('coverPreviewText').textContent = 'will be used as the cover: ' + path;
}

$('coverPreview').addEventListener('error', () => {
  $('coverPreviewText').textContent =
    'could not read that image - check the path (jpg, png, webp, gif or bmp)';
});

// ------------------------------------------------------------- toc editor

async function openToc(index) {
  const file = state.files[index];
  if (!file) return;
  appendLog('reading the table of contents of ' + file.name + ' ...');
  const info = await postJson('/api/toc', { path: file.path });
  if (info.error) {
    appendLog('!! ' + info.error);
    $('startHint').textContent = 'could not read the table of contents: ' + info.error;
    return;
  }
  state.tocOwner = file.path;
  loadTocInto(info);
}

function loadTocInto(info, focus = true) {
  state.toc = {
    path: info.path, name: info.name, title: info.title,
    entries: info.entries.map((entry) => ({ ...entry })),
    original: info.entries.map((entry) => ({ ...entry })),
  };
  $('tocPanel').classList.remove('hidden');
  $('tocBook').textContent = info.name + (info.source === 'ncx' ? ' \u00b7 ncx menu' : ' \u00b7 nav menu');
  $('tocStatus').textContent = $('outDir').value.trim()
    ? 'not saved yet'
    : 'set an output folder to save';
  $('tocStatus').className = 'badge idle';
  renderToc();
  if (!focus) {
    if (state.tocRow >= 0) showTocPage(state.tocRow);
    return;
  }
  state.tocRow = -1;
  $('tocFrame').src = 'about:blank';
  $('tocPreviewLabel').textContent = 'pick a line to preview its page';
  $('tocPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  // preview the first real chapter straight away
  const first = state.toc.entries.findIndex((entry) => !entry.href.includes('title_page') && !entry.missing);
  if (first >= 0) showTocPage(first);
}

function tocDepthClamp() {
  let previous = 0;
  state.toc.entries.forEach((entry) => {
    entry.depth = Math.max(1, Math.min(entry.depth, previous + 1));
    previous = entry.depth;
  });
}

function tocSubtreeEnd(index) {
  const depth = state.toc.entries[index].depth;
  let end = index + 1;
  while (end < state.toc.entries.length && state.toc.entries[end].depth > depth) end += 1;
  return end;
}

function renderToc() {
  const entries = state.toc ? state.toc.entries : [];
  $('tocList').innerHTML = entries.map((entry, index) => `
    <div class="tocrow${index === state.tocRow ? ' active' : ''}${entry.missing ? ' missing' : ''}"
         data-row="${index}" style="padding-left:${4 + (entry.depth - 1) * 16}px">
      <span class="level" title="level ${entry.depth}">${entry.depth}</span>
      <button data-toc="out" data-index="${index}" title="make this line one level shallower">&laquo;</button>
      <button data-toc="in" data-index="${index}" title="nest this line under the one above it">&raquo;</button>
      <button data-toc="up" data-index="${index}" title="move up">&#8593;</button>
      <button data-toc="down" data-index="${index}" title="move down">&#8595;</button>
      <input type="text" data-toc-label="${index}" value="${esc(entry.label)}"
             placeholder="chapter title" title="what the reader's menu shows">
      <button class="ghost" data-toc="show" data-index="${index}"
              title="${esc(entry.href)}${entry.missing ? ' (not in the book!)' : ''}">show</button>
    </div>`).join('');

  $('tocList').querySelectorAll('button[data-toc]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      const index = Number(button.dataset.index);
      const action = button.dataset.toc;
      if (action === 'show') { showTocPage(index); return; }
      if (action === 'in') tocIndent(index, 1);
      else if (action === 'out') tocIndent(index, -1);
      else if (action === 'up') tocMove(index, -1);
      else if (action === 'down') tocMove(index, 1);
    });
  });
  $('tocList').querySelectorAll('input[data-toc-label]').forEach((input) => {
    input.addEventListener('input', () => {
      state.toc.entries[Number(input.dataset.tocLabel)].label = input.value;
      tocDirty();
    });
  });
  $('tocList').querySelectorAll('.tocrow').forEach((row) => {
    row.addEventListener('click', () => showTocPage(Number(row.dataset.row)));
  });
}

function tocDirty() {
  if (!state.toc) return;
  $('tocStatus').textContent = 'edited - not saved';
  $('tocStatus').className = 'badge scanned';
}

function tocIndent(index, delta) {
  const entries = state.toc.entries;
  const entry = entries[index];
  if (delta > 0 && index > 0 && entries[index - 1].depth >= entry.depth) entry.depth += 1;
  else if (delta < 0 && entry.depth > 1) entry.depth -= 1;
  else return;
  tocDepthClamp();
  renderToc();
  tocDirty();
}

function tocMove(index, delta) {
  const entries = state.toc.entries;
  const start = index;
  const end = tocSubtreeEnd(index);
  const depth = entries[start].depth;
  if (delta < 0) {
    // The previous line at the same level; deeper lines above belong to that sibling.
    let previous = -1;
    for (let i = start - 1; i >= 0; i -= 1) {
      if (entries[i].depth === depth) { previous = i; break; }
      if (entries[i].depth < depth) break;  // reached the parent: nothing to swap with
    }
    if (previous < 0) return;
    const block = entries.splice(start, end - start);
    entries.splice(previous, 0, ...block);
    state.tocRow = previous;
  } else {
    // The next line at the same level, skipping our own children and its children.
    let next = end;
    while (next < entries.length && entries[next].depth > depth) next += 1;
    if (next >= entries.length || entries[next].depth !== depth) return;
    const nextEnd = tocSubtreeEnd(next);
    const block = entries.splice(start, end - start);
    entries.splice(nextEnd - block.length, 0, ...block);
    state.tocRow = nextEnd - block.length;
  }
  tocDepthClamp();
  renderToc();
  tocDirty();
}

function showTocPage(index) {
  const entry = state.toc && state.toc.entries[index];
  if (!entry) return;
  state.tocRow = index;
  $('tocList').querySelectorAll('.tocrow').forEach((row) => {
    row.classList.toggle('active', Number(row.dataset.row) === index);
  });
  $('tocPreviewLabel').textContent = entry.label + '  \u2192  ' + entry.href;
  $('tocFrame').src = entry.preview;
}

async function saveToc() {
  if (!state.toc) return;
  const info = await postJson('/api/toc/save', {
    path: state.toc.path,
    output_dir: $('outDir').value.trim(),
    entries: state.toc.entries.map((entry) =>
      ({ label: entry.label, href: entry.href, depth: entry.depth })),
  });
  if (info.error) { appendLog('!! ' + info.error); return; }
  appendLog('writing the edited table of contents ...');
  beginRun();
}

async function handleFinished(result) {
  if (result.error) return;
  if (result.mode === 'toc' && result.epub) {
    // Point the editor (and its previews) at the file we just wrote.
    const info = await postJson('/api/toc', { path: result.epub });
    if (!info.error) {
      loadTocInto(info, false);
      $('tocStatus').textContent = 'saved';
      $('tocStatus').className = 'badge done';
      appendLog('the preview now shows the saved file: ' + result.epub);
    }
  }
}

async function postJson(url, payload) {
  try {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) });
    return await response.json();
  } catch (error) {
    return { error: 'could not reach the local server: ' + error };
  }
}

function beginRun() {
  state.running = true;
  state.handledResults = 0;
  state.logOffset = 0;
  $('log').textContent = '';
  $('results').innerHTML = '';
  setStatus('running');
  updateButtons();
  poll();
}

async function poll() {
  let data;
  try {
    const response = await fetch('/api/status?since=' + state.logOffset);
    data = await response.json();
  } catch (error) {
    // Server gone or a hiccup: never leave the buttons stuck in 'running'.
    appendLog('!! lost contact with the local server (' + error + ')');
    appendLog('   reload this page if the buttons stay greyed out.');
    state.running = false;
    setStatus('failed');
    updateButtons();
    return;
  }
  if (Array.isArray(data.lines)) data.lines.forEach(appendLog);
  if (typeof data.next === 'number') state.logOffset = data.next;
  if (Array.isArray(data.results) && data.results.length) {
    renderResults(data.results);
    data.results.slice(state.handledResults || 0).forEach(handleFinished);
    state.handledResults = data.results.length;
  }
  setStatus(data.status || 'idle');
  $('active').textContent = data.active ? 'working on ' + data.active : '';
  state.running = data.status === 'running';
  $('stop').disabled = !state.running;
  updateButtons();
  if (state.running) setTimeout(poll, 700);
}

async function browse(kind, target) {
  const response = await fetch('/api/browse', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind }) });
  const data = await response.json();
  if (data.path) { $(target).value = data.path; updateButtons(); }
}

$('drop').addEventListener('click', () => $('picker').click());
$('picker').addEventListener('change', (event) => upload(event.target.files));
['dragenter', 'dragover'].forEach((type) => document.addEventListener(type, (event) => {
  event.preventDefault();
  $('drop').classList.add('hot');
}));
document.addEventListener('dragleave', (event) => {
  event.preventDefault();
  if (!event.relatedTarget) $('drop').classList.remove('hot');
});
document.addEventListener('drop', (event) => {
  event.preventDefault();
  $('drop').classList.remove('hot');
  upload(event.dataTransfer.files);
});
$('browseOut').addEventListener('click', () => browse('dir', 'outDir'));
$('browseCover').addEventListener('click', () => browse('image', 'cover'));
$('cover').addEventListener('input', updateCoverPreview);
$('cover').addEventListener('change', updateCoverPreview);
$('clearCover').addEventListener('click', () => {
  $('cover').value = '';
  updateCoverPreview();
});
$('tocSave').addEventListener('click', saveToc);
$('tocReset').addEventListener('click', () => {
  if (!state.toc) return;
  state.toc.entries = state.toc.original.map((entry) => ({ ...entry }));
  renderToc();
  $('tocStatus').textContent = 'changes undone';
  $('tocStatus').className = 'badge idle';
});
$('tocClose').addEventListener('click', () => {
  $('tocPanel').classList.add('hidden');
  $('tocFrame').src = 'about:blank';
});

$('outDir').value = localStorage.getItem('pdf2epub.output') || '';
$('splitLevel').value = localStorage.getItem('pdf2epub.split') || '';
$('splitLevel').addEventListener('input', () =>
  localStorage.setItem('pdf2epub.split', $('splitLevel').value));
$('outDir').addEventListener('input', updateButtons);
$('start').addEventListener('click', start);
$('stop').addEventListener('click', () => fetch('/api/stop', { method: 'POST' }));
updateCoverPreview();
updateButtons();

fetch('/api/info').then((response) => response.json()).then((info) => {
  $('env').innerHTML =
    'python: <code>' + esc(info.python) + '</code> &middot; ' +
    'pandoc: <code>' + esc(info.pandoc) + '</code> &middot; ' +
    'llama-server: <code>' + esc(info.llama_server || 'not found (OCR unavailable)') + '</code>' +
    '<br>temp files: <code>' + esc(info.work_dir) + '</code>';
  if (!localStorage.getItem('pdf2epub.output') && info.default_output_dir) {
    // So the buttons are never mysteriously disabled on a first run.
    $('outDir').value = info.default_output_dir;
  }
  updateButtons();
});

// Reattach to a job that is already running (e.g. after reloading the page).
fetch('/api/status?since=0').then((response) => response.json()).then((data) => {
  if (data.status === 'running') { state.logOffset = 0; setStatus('running'); poll(); }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
