#!/usr/bin/env python3
"""Convert PDF books into e-ink friendly EPUB files.

Two very different kinds of PDF need two different pipelines, and this script
picks the right one automatically:

* **Born-digital PDF** (the pages contain a real text layer)
  -> ``marker_single --mode fast --disable_ocr``: marker reads the text layer
  with pdftext and only uses its lightweight layout detectors. Fast, no GPU
  vision model, good heading detection.

* **Scanned PDF** (every page is just one big image)
  -> the pages are re-rendered into a text-free copy of the PDF (this throws
  away the useless/garbled OCR layer that most scanned PDFs carry), then marker
  runs its full layout + OCR pipeline on it. Text comes out clean and figures
  are still extracted.

Both paths then hand the markdown to ``pandoc``, which splits the book into one
internal HTML file per chapter (``--split-level``). Without that split, e-readers
such as Crosspoint show the whole book as a single chapter and the chapter menu
does nothing.

Run it inside the marker virtual environment (it needs marker-pdf, torch,
pypdfium2 and Pillow). See MANUAL.md.
"""

from __future__ import annotations

import argparse
import io
import itertools
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator
from xml.etree import ElementTree

from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]

# Pages whose embedded text layer has fewer characters than this are treated as
# image-only (scanned) pages.
MIN_CHARS_PER_PAGE = 100
# If this share of the pages is dominated by a single full-page image, the PDF is
# a scan.
SCANNED_PAGE_RATIO = 0.5
# Marker renders its OCR input at 192 dpi by default; 300 dpi here keeps the
# flattened copy of a scan at least as sharp as the original.
FLATTEN_DPI = 300

# Windows-only: run child processes on their own (hidden) console, see run_command.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
EMPTY_IMAGE_RE = re.compile(r"!\[\]\(\s*\)")
# marker sprinkles invisible page anchors through the text: <span id="page-12-0"></span>
# and links pointing at them. They mean nothing in a reflowable EPUB.
PAGE_ANCHOR_SPAN_RE = re.compile(r'<span id="page-\d+-\d+">\s*</span>')
PAGE_ANCHOR_LINK_RE = re.compile(r"\[([^\]]*)\]\(#page-\d+-\d+\)")
EMPTY_HEADING_RE = re.compile(r"^#{1,6}\s*$", re.MULTILINE)
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
LIST_TABLE_CELL_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")


class ConversionError(RuntimeError):
    """Something went wrong that the user needs to act on."""


# --------------------------------------------------------------------------- #
# Logging / process helpers
# --------------------------------------------------------------------------- #


def log_stdout(message: str) -> None:
    print(message, flush=True)


# Command-line fragments of the model servers marker/surya start for themselves.
# They are never user processes, which is what makes reaping them safe.
HELPER_MARKERS = ("fast_layout.server", "ocr_error", "llama-server")


class HelperReaper:
    """Stops the model servers that marker/surya start and then fail to stop.

    surya shuts its servers down with ``os.kill(pid, 0)``, which on Windows
    terminates nothing - it sends CTRL_C_EVENT to a console process group. The
    llama-server / fast_layout server can therefore outlive the conversion and
    keep ~1.5 GB of RAM and its port until the next reboot (observed: a finished
    run left one running).

    Walking the process tree does not work for this: ``marker_single.exe`` is a
    launcher that exits at once, so the process we started is gone while the real
    work continues in a process we never see, and the servers are spawned by that
    one. Instead, helpers are recognised by their command line and their start
    time: anything that did not exist before this run started is ours.

    Leftovers from an earlier run are only touched when their parent process is
    gone as well (a crashed run), and nothing is killed while another marker run
    is active.
    """

    def __init__(self, log: LogFn) -> None:
        self.log = log
        self.started_at = time.time()
        self.before: set[int] = set()
        self._psutil: Any = None  # optional: psutil ships with marker's dependencies
        try:
            import psutil
            self._psutil = psutil
        except ImportError:
            return
        self.before = set(self._helpers())

    def _helpers(self) -> dict[int, float]:
        """Every live helper server: pid -> creation time."""
        found: dict[int, float] = {}
        for process in self._psutil.process_iter(["pid", "cmdline", "create_time"]):
            command = " ".join(process.info["cmdline"] or [])
            if any(marker in command for marker in HELPER_MARKERS):
                found[process.info["pid"]] = process.info["create_time"] or 0.0
        return found

    def _is_orphan(self, pid: int) -> bool:
        try:
            parent = self._psutil.Process(pid).ppid()
        except Exception:
            return False
        if not parent:
            return True
        try:
            return not self._psutil.pid_exists(parent)
        except Exception:
            return False

    def reap(self) -> list[int]:
        """Terminate the helper servers that are still running after our run."""
        if self._psutil is None:
            return []
        if self._another_marker_is_running():
            # A second conversion may be attached to the same server; leave it alone.
            return []
        killed: list[int] = []
        for pid, created in self._helpers().items():
            ours = pid not in self.before and created >= self.started_at - 5
            if not ours and not self._is_orphan(pid):
                continue
            try:
                process = self._psutil.Process(pid)
                process.terminate()
                try:
                    process.wait(timeout=3)
                except self._psutil.TimeoutExpired:
                    process.kill()
                killed.append(pid)
            except Exception:
                continue
        if killed:
            self.log(f"   stopped {len(killed)} helper process(es) marker left running")
        return killed

    def _another_marker_is_running(self) -> bool:
        for process in self._psutil.process_iter(["pid", "cmdline"]):
            command = " ".join(process.info["cmdline"] or [])
            if "marker_single" in command or "marker.scripts.convert_single" in command:
                return True
        return False


def _iter_output(stream) -> Iterable[str]:
    """Yield cleaned lines from a subprocess pipe, splitting on \\r as well as \\n.

    Needed because marker/tqdm draw progress bars with carriage returns.
    """
    buffer = ""
    while True:
        data = stream.read(4096)
        if not data:
            break
        buffer += data.decode("utf-8", errors="replace")
        parts = re.split(r"\r\n|\r|\n", buffer)
        buffer = parts.pop()  # trailing partial line
        for part in parts:
            part = ANSI_RE.sub("", part).rstrip()
            if part.strip():
                yield part
    tail = ANSI_RE.sub("", buffer).rstrip()
    if tail.strip():
        yield tail


class _SuryaNoiseFilter:
    """Drops the traceback surya prints when it shuts llama-server down.

    It always ends a successful run (a deliberate KeyboardInterrupt inside an
    atexit handler) and means nothing else, so it is filtered out of the log.
    """

    def __init__(self) -> None:
        self._in_block = False

    def __call__(self, line: str) -> bool:
        if "Exception ignored in atexit callback" in line and "attach_or_spawn" in line:
            self._in_block = True
            return True
        if self._in_block:
            # Traceback bodies are indented; a fresh unindented line ends the block.
            if line.startswith((" ", "\t")) or line.startswith(
                ("Traceback", "File ", "KeyboardInterrupt")
            ):
                return True
            self._in_block = False
        return False




def run_command(
    cmd: list[str],
    log: LogFn,
    env: dict | None = None,
    cwd: Path | None = None,
    should_stop: StopFn | None = None,
    echo_output: bool = False,
) -> int:
    """Run a command, forwarding its output to ``log``. Returns the exit code."""
    log(f"$ {' '.join(cmd)}")
    reaper = HelperReaper(log)  # must snapshot the process list before we start
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(cwd) if cwd else None,
        # Windows: give the child its own console. marker/surya shuts its
        # llama-server down with os.kill(pid, 0), which on Windows means
        # "GenerateConsoleCtrlEvent(CTRL_C_EVENT)" - without an own console that
        # Ctrl+C also hits us and aborts the whole conversion.
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdout is not None
    is_noise = _SuryaNoiseFilter()
    try:
        for line in _iter_output(process.stdout):
            if is_noise(line):
                continue
            log(line)
            if echo_output:
                log_stdout(line)
            if should_stop and should_stop():
                process.kill()
                log("!! cancelled")
                return -1
    finally:
        process.stdout.close()
        process.wait()
        reaper.reap()
    return process.returncode


# --------------------------------------------------------------------------- #
# Locating the external tools
# --------------------------------------------------------------------------- #


def find_marker_command() -> list[str]:
    """Find marker_single in (or next to) the interpreter that runs this script."""
    exe_name = "marker_single.exe" if os.name == "nt" else "marker_single"
    candidate = Path(sys.executable).parent / exe_name
    if candidate.exists():
        return [str(candidate)]
    found = shutil.which("marker_single")
    if found:
        return [found]
    # Last resort: call the click entry point directly in this interpreter.
    try:
        import marker  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConversionError(
            "marker_single was not found and marker-pdf is not installed in this "
            f"Python environment ({sys.executable}).\n"
            "Run this script with the Python of your marker virtual environment, e.g.\n"
            r'  C:\Users\<you>\marker_env\Scripts\python.exe pdf2epub.py --help'
        ) from exc
    return [
        sys.executable,
        "-c",
        "from marker.scripts.convert_single import convert_single_cli; convert_single_cli()",
    ]


def find_pandoc() -> str:
    found = shutil.which("pandoc")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Pandoc" / "pandoc.exe"
    if local.exists():
        return str(local)
    raise ConversionError(
        "pandoc was not found. Install it with:  winget install --exact --id JohnMacFarlane.Pandoc"
    )


def find_llama_server() -> str | None:
    """llama-server is only needed for OCR (scanned PDFs / VLM layout)."""
    override = os.environ.get("LLAMA_CPP_BINARY")
    if override and Path(override).exists():
        return override
    for candidate in (
        Path(os.environ.get("SystemDrive", "C:") + "/Tools/llama.cpp/llama-server.exe"),
        Path.home() / "Tools" / "llama.cpp" / "llama-server.exe",
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("llama-server")
    return found


def marker_environment(needs_vlm: bool, log: LogFn) -> dict:
    """Environment for marker: force the local llama.cpp backend instead of Docker.

    Surya picks vLLM (which wants a Docker container) whenever an NVIDIA GPU is
    present, so the backend has to be pinned to llama.cpp explicitly.
    """
    env = dict(os.environ)
    llama_server = find_llama_server()
    if llama_server:
        env["LLAMA_CPP_BINARY"] = llama_server
        env["SURYA_INFERENCE_BACKEND"] = "llamacpp"
        log(f"   llama-server:    {llama_server}")
        return env
    if not needs_vlm:
        # The text-layer path never loads the vision model.
        return env
    raise ConversionError(
        "OCR needs the llama-server binary (marker's vLLM backend would want Docker,\n"
        "which is not usable here).\n"
        "Download a llama.cpp Windows CUDA build from\n"
        "  https://github.com/ggml-org/llama.cpp/releases\n"
        "unpack llama-server.exe somewhere (e.g. C:\\Tools\\llama.cpp\\) and either\n"
        "add that folder to your PATH or set the LLAMA_CPP_BINARY environment variable."
    )


def patch_surya_grammar(log: LogFn) -> None:
    """Fix surya's JSON grammars for current llama.cpp builds.

    surya sends a regex pattern containing ``\\d`` to llama-server for guided
    layout decoding. ``\\d`` is an invalid escape in llama.cpp's grammar parser
    (build ~b4800 and newer, including the b10199 build installed here), so the
    server rejects the request with "400 - Failed to initialize samplers: failed
    to parse grammar" and marker loops on "Inference error". ``[0-9]`` means the
    same thing and every llama.cpp version accepts it.

    This is idempotent, but it has to be redone after every ``pip install -U
    surya-ocr``, which is why the script checks it on every run.
    """
    try:
        import surya
    except ImportError:
        return
    prompts = Path(surya.__file__).parent / "inference" / "prompts.py"
    if not prompts.exists():
        return
    lines = prompts.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    for index, line in enumerate(lines):
        if '"pattern"' in line and "\\d" in line:
            lines[index] = line.replace("\\d", "[0-9]")
            changed += 1
    if changed:
        prompts.write_text("".join(lines), encoding="utf-8")
        log(f"   patched surya grammar ({changed} pattern(s)) in {prompts}")
        log("   (redo this after upgrading surya-ocr: it is a known upstream bug)")


# --------------------------------------------------------------------------- #
# Inspecting the PDF
# --------------------------------------------------------------------------- #


@dataclass
class PdfInfo:
    path: Path
    pages: int
    chars_per_page: list[int]
    image_pages: int
    meta_title: str
    meta_author: str

    @property
    def text_pages(self) -> int:
        return sum(1 for c in self.chars_per_page if c >= MIN_CHARS_PER_PAGE)

    @property
    def image_page_ratio(self) -> float:
        return self.image_pages / self.pages if self.pages else 0.0

    def describe(self) -> str:
        return (
            f"{self.pages} pages, {self.text_pages} with a text layer, "
            f"{self.image_pages} full-page images"
        )


def read_pdf_info(pdf: Path) -> PdfInfo:
    """Cheaply measure how much real text and how many page-images a PDF has."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    document = pdfium.PdfDocument(pdf)
    chars_per_page: list[int] = []
    image_pages = 0
    for page in document:
        chars_per_page.append(len(page.get_textpage().get_text_range().strip()))
        width, height = page.get_size()
        area = width * height
        for obj in page.get_objects():
            # PdfObject.type is documented pypdfium2 API but missing from its type stubs.
            if obj.type != raw.FPDF_PAGEOBJ_IMAGE:  # type: ignore
                continue
            x0, y0, x1, y1 = obj.get_bounds()
            if abs((x1 - x0) * (y1 - y0)) / area > 0.7:
                image_pages += 1
                break
    meta = document.get_metadata_dict() or {}
    return PdfInfo(
        path=pdf,
        pages=len(document),
        chars_per_page=chars_per_page,
        image_pages=image_pages,
        meta_title=(meta.get("Title") or "").strip(),
        meta_author=(meta.get("Author") or "").strip(),
    )


def classify_pdf(info: PdfInfo) -> str:
    """Return 'scanned' or 'digital'."""
    if info.image_page_ratio >= SCANNED_PAGE_RATIO:
        return "scanned"
    if not info.chars_per_page:
        return "scanned"
    text_pages = info.text_pages
    if text_pages / info.pages < 0.3:
        return "scanned"
    return "digital"


# --------------------------------------------------------------------------- #
# Flattening a scan (drop the old, wrong text layer)
# --------------------------------------------------------------------------- #


def flatten_pdf(pdf: Path, target: Path, log: LogFn, dpi: int = FLATTEN_DPI,
                should_stop: StopFn | None = None) -> Path:
    """Re-render every page into a new PDF that contains no text layer.

    Scanned PDFs are page images plus a text layer that came from whichever OCR
    tool made the file - that layer is frequently full of errors ("Turntobilizotion"
    instead of "Turntablization") and marker trusts it by default, so the errors
    survive into the EPUB. Removing it forces marker to OCR the pages properly.

    Pages are written out one at a time (through temporary JPEGs) so memory stays
    flat even for 500 page books.
    """
    from PIL import Image, JpegImagePlugin  # noqa: F401 - registers the JPEG encoder
    import pypdfium2 as pdfium

    target.parent.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72
    with tempfile.TemporaryDirectory(prefix="pdf2epub-flat-") as tmp:
        tmp_dir = Path(tmp)
        page_files: list[Path] = []
        document = pdfium.PdfDocument(pdf)
        log(f"   rendering {len(document)} pages at {dpi} dpi ...")
        for index, page in enumerate(document):
            if should_stop and should_stop():
                raise ConversionError("cancelled")
            out = tmp_dir / f"page-{index:05d}.jpg"
            # render() accepts a float scale (the stub claims int).
            page.render(scale=scale).to_pil().convert("RGB").save(  # type: ignore
                out, format="JPEG", quality=92, optimize=True
            )
            page_files.append(out)
            if (index + 1) % 25 == 0 or index + 1 == len(document):
                log(f"   ... {index + 1}/{len(document)} pages rendered")

        # Pillow assembles the new PDF; each JPEG is only loaded while written.
        frames = [Image.open(path) for path in page_files]
        try:
            frames[0].save(
                target,
                save_all=True,
                append_images=frames[1:],
                resolution=dpi,
                title=pdf.stem,
            )
        finally:
            for frame in frames:
                frame.close()
    log(f"   flattened copy: {target} ({target.stat().st_size / 1_048_576:.0f} MB)")
    return target


# --------------------------------------------------------------------------- #
# Marker
# --------------------------------------------------------------------------- #


def run_marker(
    pdf: Path,
    out_root: Path,
    kind: str,
    fast_ocr: bool,
    log: LogFn,
    should_stop: StopFn | None = None,
    page_range: str | None = None,
) -> Path:
    """Convert one PDF with marker and return the path of the markdown it wrote."""
    needs_vlm = kind == "scanned"
    if needs_vlm:
        patch_surya_grammar(log)
    env = marker_environment(needs_vlm, log)

    cmd = find_marker_command() + [str(pdf), "--output_dir", str(out_root)]
    if kind == "digital":
        # Read the PDF's own text layer; skip the vision model for layout and OCR.
        cmd += ["--mode", "fast", "--disable_ocr"]
        log("   pipeline:        fast (text layer, no OCR)")
    elif fast_ocr:
        # Let the VLM OCR whole pages. Fastest OCR path, but figures inside the
        # scanned pages are not cropped out.
        cmd += ["--force_ocr"]
        log("   pipeline:        force OCR (fast, no figure cropping)")
    else:
        log("   pipeline:        full layout + OCR (keeps figures)")
    if page_range:
        cmd += ["--page_range", page_range]

    out_root.mkdir(parents=True, exist_ok=True)
    code = run_command(cmd, log, env=env, should_stop=should_stop)
    if should_stop and should_stop():
        raise ConversionError("cancelled")
    if code != 0:
        raise ConversionError(
            f"marker_single failed with exit code {code}. Scroll up in the log for the "
            "first error message."
        )

    markdown = sorted(out_root.rglob("*.md"))
    if not markdown:
        raise ConversionError(f"marker did not write a markdown file into {out_root}")
    # marker writes <out_root>/<pdf stem>/<pdf stem>.md
    for path in markdown:
        if path.parent.name == pdf.stem:
            return path
    return markdown[0]


# --------------------------------------------------------------------------- #
# Markdown cleanup
# --------------------------------------------------------------------------- #


def referenced_images(markdown: Path) -> set[str]:
    """Filenames of the images a markdown file links to.

    marker's output directory is reused between runs, so it can still hold images
    from an earlier conversion of the same book; only what the markdown points at
    belongs in the output folder.
    """
    text = markdown.read_text(encoding="utf-8")
    return {Path(match).name for match in IMAGE_REF_RE.findall(text)}


def strip_empty_image_links(text: str) -> tuple[str, int]:
    """Turn ``![]()`` (an image marker without a file) back into plain text."""
    count = len(EMPTY_IMAGE_RE.findall(text))
    return EMPTY_IMAGE_RE.sub("", text), count


def strip_page_anchors(text: str) -> tuple[str, int]:
    """Remove marker's ``<span id="page-12-0"></span>`` page anchors and links to them."""
    text, spans = PAGE_ANCHOR_SPAN_RE.subn("", text)
    text, links = PAGE_ANCHOR_LINK_RE.subn(r"\1", text)
    return text, spans + links


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells) \
        and any(cells)


def _looks_like_index_table(rows: list[list[str]]) -> bool:
    """True for printed tables of contents / indexes (title ... page number)."""
    if len(rows) < 4:
        return False
    numbered = 0
    for row in rows:
        if any(LIST_TABLE_CELL_NUMBER_RE.match(cell) for cell in row):
            numbered += 1
    return numbered / len(rows) >= 0.6


def convert_index_tables(text: str) -> tuple[str, int]:
    """Rewrite tables that are really a printed table of contents.

    A printed TOC picked up as a markdown table becomes a rigid HTML <table> in
    the EPUB, which reflows badly on an e-reader, and the page numbers are
    meaningless anyway. Those tables become bullet lists instead; real data
    tables are left untouched.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    converted = 0
    while index < len(lines):
        line = lines[index]
        if "|" in line and index + 1 < len(lines) and _is_separator_row(lines[index + 1]):
            block_end = index
            while block_end < len(lines) and "|" in lines[block_end]:
                block_end += 1
            block = lines[index:block_end]
            rows = [_split_table_row(row) for row in block if not _is_separator_row(row)]
            rows = [row for row in rows if any(row)]
            if _looks_like_index_table(rows):
                if out and out[-1].strip():
                    out.append("")
                for row in rows:
                    for cell in row:
                        cell = cell.strip()
                        if not cell or LIST_TABLE_CELL_NUMBER_RE.match(cell):
                            continue
                        # Drop the trailing print page number, if there is one.
                        cell = re.sub(r"\s+\d{1,4}$", "", cell).strip()
                        if not cell:
                            continue
                        number = re.match(r"^(\d{1,4})[.)]?\s+(\S.*)$", cell)
                        if number:
                            out.append(f"* **{number.group(1)}.** {number.group(2)}")
                        else:
                            out.append(f"* {cell}")
                out.append("")
                converted += 1
                index = block_end
                continue
            index = block_end
            continue
        out.append(line)
        index += 1
    return "\n".join(out), converted


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_markdown(md_path: Path, log: LogFn, convert_toc_tables: bool = True) -> dict:
    text = md_path.read_text(encoding="utf-8")
    text, empty_images = strip_empty_image_links(text)
    text, anchors = strip_page_anchors(text)
    tables = 0
    if convert_toc_tables:
        text, tables = convert_index_tables(text)
    text = EMPTY_HEADING_RE.sub("", text)
    md_path.write_text(collapse_blank_lines(text), encoding="utf-8")
    if empty_images:
        log(f"   removed {empty_images} empty image tag(s)")
    if anchors:
        log(f"   removed {anchors} page anchor reference(s)")
    if tables:
        log(f"   rewrote {tables} printed table(s) of contents as bullet lists")
    return {"empty_images": empty_images, "anchors": anchors, "tables": tables}


def heading_levels(text: str) -> dict[int, int]:
    """Count markdown headings per level, ignoring fenced code blocks."""
    counts: dict[int, int] = {}
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            counts[level] = counts.get(level, 0) + 1
    return counts


def pick_split_level(counts: dict[int, int], minimum: int = 3, ratio: float = 0.4) -> int:
    """Choose the heading level that should start a new internal file.

    The shallowest heading level with a substantial number of headings is the one
    that behaves like "chapter" in practice, and the counts are what separates a
    real level from decoration:

    * 19 titles at h1 and 37 sub-sections at h2  -> h1 (the h1s are the chapters)
    * 2 occurrences of the book title at h1 and 30 sections at h4 -> h4
    * 62 chapters at h2 and a single book title at h1 -> h2

    A heading level counts as substantial when it has at least ``minimum``
    headings and at least ``ratio`` of the most common level's count.
    """
    if not counts:
        return 1
    threshold = max(minimum, ratio * max(counts.values()))
    for level in sorted(counts):
        if counts[level] >= threshold:
            return level
    return min(counts)


# --------------------------------------------------------------------------- #
# Pandoc
# --------------------------------------------------------------------------- #


def build_epub(
    markdown: Path,
    epub: Path,
    title: str,
    author: str,
    split_level: int,
    toc_depth: int,
    cover: Path | None,
    log: LogFn,
    should_stop: StopFn | None = None,
) -> None:
    """Build an EPUB2 file whose chapters are separate files (e-reader friendly)."""
    pandoc = find_pandoc()
    epub.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        pandoc,
        str(markdown),
        "-o",
        str(epub),
        "-t",
        "epub2",  # EPUB2 + toc.ncx, which is what simple e-reader firmware handles
        "--toc",
        f"--toc-depth={toc_depth}",
        f"--split-level={split_level}",  # one internal HTML file per heading at this level
        "--resource-path",
        str(markdown.parent),  # so the extracted images are found
        "--metadata",
        f"title={title}",
        "--metadata",
        f"author={author}",
        "--metadata",
        "lang=en",
    ]
    if cover and cover.exists():
        cmd += ["--epub-cover-image", str(cover)]
    elif cover:
        log(f"   !! cover image not found: {cover}")
    code = run_command(cmd, log, should_stop=should_stop, echo_output=True)
    if code != 0:
        raise ConversionError(f"pandoc failed with exit code {code}")


def inspect_epub(epub: Path) -> dict:
    """Report how the EPUB is actually structured, so problems are visible."""
    with zipfile.ZipFile(epub) as archive:
        names = archive.namelist()
        chapters = sorted(name for name in names if re.search(r"text/ch\d+\.xhtml$", name))
        images = [n for n in names if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg"))]
        nav_points = 0
        ncx = next((n for n in names if n.endswith(".ncx")), None)
        if ncx:
            nav_points = archive.read(ncx).decode("utf-8", errors="replace").count("<navPoint")
    return {
        "chapters": len(chapters),
        "images": len(images),
        "toc_entries": nav_points,
        "size_mb": round(epub.stat().st_size / 1_048_576, 2),
    }


# --------------------------------------------------------------------------- #
# Title / author of an existing EPUB
# --------------------------------------------------------------------------- #

OPF_PATH_RE = re.compile(r'full-path="([^"]+)"')
DC_TITLE_RE = re.compile(r"(<dc:title\b[^>]*>)(.*?)(</dc:title>)", re.S)
DC_CREATOR_RE = re.compile(r"(<dc:creator\b[^>]*>)(.*?)(</dc:creator>)", re.S)
NCX_TITLE_RE = re.compile(r"(<docTitle>\s*<text\b[^>]*>)(.*?)(</text>)", re.S)
HTML_TITLE_RE = re.compile(r"(<title\b[^>]*>)(.*?)(</title>)", re.S)
TITLE_PAGE_TITLE_RE = re.compile(r'(<h1 class="title"[^>]*>)(.*?)(</h1>)', re.S)
TITLE_PAGE_AUTHOR_RE = re.compile(r'(<p class="author"[^>]*>)(.*?)(</p>)', re.S)
MANIFEST_RE = re.compile(r"(<manifest\b[^>]*>)(.*?)(</manifest>)", re.S)
SPINE_RE = re.compile(r"(<spine\b[^>]*>)(.*?)(</spine>)", re.S)
GUIDE_RE = re.compile(r"(<guide\b[^>]*>)(.*?)(</guide>)", re.S)
XML_ITEM_RE = re.compile(r"<item\b[^>]*/?>", re.S)
XML_ITEMREF_RE = re.compile(r"<itemref\b[^>]*/?>", re.S)
XML_REFERENCE_RE = re.compile(r"<reference\b[^>]*/?>", re.S)
COVER_META_RE = re.compile(r'\s*<meta\b[^>]*\bname="cover"[^>]*/?>')
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# Long edge of the embedded cover. E-reader screens are small; anything bigger is
# wasted space in the file (and most readers scale it down anyway).
COVER_MAX_EDGE = 1600

COVER_PAGE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
  <title>{title}</title>
  <style type="text/css">body {{ margin: 0; padding: 0; }} div {{ text-align: center; }}</style>
</head>
<body id="cover">
<div id="cover-image">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid">
<image width="{width}" height="{height}" xlink:href="{href}" />
</svg>
</div>
</body>
</html>
"""


def _tag_attributes(tag: str) -> dict[str, str]:
    """Attributes of an XML start tag, as a dict."""
    return {match.group(1): match.group(2)
            for match in re.finditer(r'([\w:-]+)\s*=\s*"([^"]*)"', tag)}


def _prepare_cover_image(cover: Path, log: LogFn) -> tuple[bytes, str, str, tuple[int, int]]:
    """Normalise a cover for embedding. Returns (data, extension, media type, size).

    Formats no e-reader understands are re-encoded as JPEG, and oversized images
    are scaled down so a 12 MP photo does not bloat the book.
    """
    cover = cover.expanduser()
    if not cover.is_file():
        raise ConversionError(f"cover image not found: {cover}")
    suffix = cover.suffix.lower()
    conversion_needed = suffix not in (".jpg", ".jpeg", ".png")
    try:
        from PIL import Image
        with Image.open(cover) as image:
            image.load()
            width, height = image.size
            long_edge = max(width, height)
            if conversion_needed or long_edge > COVER_MAX_EDGE:
                scale = min(1.0, COVER_MAX_EDGE / long_edge)
                if scale < 1.0:
                    image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=88, optimize=True)
                log(
                    f"   cover: resized {width}x{height} ({suffix or 'unknown'}) -> "
                    f"{image.size[0]}x{image.size[1]} JPEG, {len(buffer.getvalue()) / 1024:.0f} KB"
                )
                return buffer.getvalue(), ".jpg", "image/jpeg", image.size
    except ConversionError:
        raise
    except Exception as error:  # unreadable image: embed it as it is and let the reader try
        log(f"   !! could not process the cover image ({error}); embedding it unchanged")
    return cover.read_bytes(), suffix, MEDIA_TYPES.get(suffix, "image/jpeg"), (0, 0)


def _find_cover_items(opf: str) -> tuple[set[str], set[str], set[str]]:
    """Find what the OPF uses as cover: (image ids, page ids, files to delete)."""
    manifest = MANIFEST_RE.search(opf)
    if not manifest:
        return set(), set(), set()
    cover_meta = COVER_META_RE.search(opf)
    declared_id = _tag_attributes(cover_meta.group(0)).get("content", "") if cover_meta else ""
    image_ids: set[str] = set()
    page_ids: set[str] = set()
    files: set[str] = set()
    for tag in XML_ITEM_RE.findall(manifest.group(2)):
        attrs = _tag_attributes(tag)
        item_id = attrs.get("id", "")
        href = attrs.get("href", "")
        media_type = attrs.get("media-type", "")
        looks_like_cover = "cover" in item_id.lower() or "cover" in href.lower()
        if media_type.startswith("image/"):
            if "cover-image" in attrs.get("properties", "") or looks_like_cover or item_id == declared_id:
                image_ids.add(item_id)
                files.add(href)
        elif media_type == "application/xhtml+xml" and (looks_like_cover or item_id == declared_id):
            page_ids.add(item_id)
            files.add(href)
    guide = GUIDE_RE.search(opf)
    if guide:
        for tag in XML_REFERENCE_RE.findall(guide.group(2)):
            attrs = _tag_attributes(tag)
            if attrs.get("type") == "cover" and attrs.get("href"):
                files.add(attrs["href"])
    return image_ids, page_ids, files


def _replace_manifest(opf: str, drop_ids: set[str], extra_items: list[str]) -> str:
    match = MANIFEST_RE.search(opf)
    if not match:
        return opf
    kept = [
        tag.strip() for tag in XML_ITEM_RE.findall(match.group(2))
        if _tag_attributes(tag).get("id") not in drop_ids
    ]
    body = "\n    " + "\n    ".join(kept + [item.strip() for item in extra_items]) + "\n  "
    return opf[: match.start(2)] + body + opf[match.end(2) :]


def _replace_spine(opf: str, drop_ids: set[str], first_itemref: str) -> str:
    """Put the cover page first in the spine and drop a stale cover entry."""
    match = SPINE_RE.search(opf)
    if not match:
        return opf
    kept = [
        tag.strip() for tag in XML_ITEMREF_RE.findall(match.group(2))
        if _tag_attributes(tag).get("idref") not in drop_ids
    ]
    body = "\n    " + first_itemref + "\n    " + "\n    ".join(kept) + "\n  "
    return opf[: match.start(2)] + body + opf[match.end(2) :]


def _replace_guide(opf: str, cover_href: str) -> str:
    reference = f'<reference type="cover" title="Cover" href="{cover_href}" />'
    match = GUIDE_RE.search(opf)
    if match:
        kept = [
            tag.strip() for tag in XML_REFERENCE_RE.findall(match.group(2))
            if _tag_attributes(tag).get("type") != "cover"
        ]
        body = "\n    " + "\n    ".join(kept + [reference]) + "\n  "
        return opf[: match.start(2)] + body + opf[match.end(2) :]
    # No guide (EPUB3 style package): add one, EPUB2 readers look for it.
    return opf.replace(
        "</package>", f"  <guide>\n    {reference}\n  </guide>\n</package>", 1
    )


def _apply_cover(entries: dict[str, bytes], opf_path: str, cover: Path,
                 log: LogFn) -> tuple[list[str], str]:
    """Embed (or replace) the cover image, its page, and the OPF wiring for it.

    Returns the files it touched and the zip entry the cover image lives in.
    """
    opf = entries[_opf_path(entries)].decode("utf-8", errors="replace")
    data, extension, media_type, size = _prepare_cover_image(cover, log)

    opf_dir = PurePosixPath(opf_path).parent
    old_image_ids, old_page_ids, old_files = _find_cover_items(opf)
    old_page_ids = {item for item in old_page_ids if item}

    image_href = str(opf_dir / "media" / f"cover{extension}")
    if entries.get(image_href) and "cover" not in image_href.lower():
        image_href = str(opf_dir / "media" / f"cover-image{extension}")
    # Reuse the existing cover page if the book already has one.
    cover_page = None
    for name in entries:
        if name.lower().endswith(("cover.xhtml", "cover.htm", "coverpage.xhtml")):
            cover_page = name
            break
    page_href = cover_page or str(opf_dir / "text" / "cover.xhtml")

    entries[image_href] = data
    entries[page_href] = COVER_PAGE_TEMPLATE.format(
        title="Cover",
        width=size[0] or 1000,
        height=size[1] or 1500,
        href=os.path.relpath("/" + image_href, "/" + str(PurePosixPath(page_href).parent)).replace("\\", "/"),
    ).encode("utf-8")

    image_id = "cover-image"
    page_id = "cover-page"
    opf = _replace_manifest(
        opf,
        old_image_ids | old_page_ids | {image_id, page_id},
        [
            f'<item id="{image_id}" href="{image_href[len(str(opf_dir)) + 1:]}" '
            f'media-type="{media_type}" properties="cover-image" />',
            f'<item id="{page_id}" href="{page_href[len(str(opf_dir)) + 1:]}" '
            f'media-type="application/xhtml+xml" />',
        ],
    )
    opf = _replace_spine(opf, old_page_ids | {page_id}, f'<itemref idref="{page_id}" />')
    opf = _replace_guide(opf, page_href[len(str(opf_dir)) + 1:])
    opf = COVER_META_RE.sub("", opf)
    opf = opf.replace(
        "</metadata>", f'    <meta name="cover" content="{image_id}" />\n  </metadata>', 1
    )
    entries[opf_path] = opf.encode("utf-8")

    # Drop the previous cover files if nothing points at them any more.
    for stale in old_files:
        gone = stale if stale in entries else str(opf_dir / stale)
        if gone in entries and gone not in (image_href, page_href):
            del entries[gone]
    log(f"   cover: {media_type}, {page_href}" + (" (replaced)" if old_image_ids else ""))
    return [opf_path, page_href, image_href], image_href


def _read_epub_entries(epub: Path) -> dict[str, bytes]:
    """Every file inside the EPUB, in archive order. EPUBs are small enough to hold."""
    try:
        with zipfile.ZipFile(epub) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except zipfile.BadZipFile as error:
        raise ConversionError(f"{epub.name} is not a readable EPUB (not a zip file)") from error


def _opf_path(entries: dict[str, bytes]) -> str:
    container = entries.get("META-INF/container.xml")
    if not container:
        raise ConversionError("not an EPUB: META-INF/container.xml is missing")
    match = OPF_PATH_RE.search(container.decode("utf-8", errors="replace"))
    if not match or match.group(1) not in entries:
        raise ConversionError("not an EPUB: no package document (OPF) referenced in container.xml")
    return match.group(1)


def _inner_text(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text)
    return xml_unescape(match.group(2)).strip() if match else ""


def read_epub_metadata(epub: Path) -> tuple[str, str]:
    """The title and author as they are stored inside the EPUB."""
    entries = _read_epub_entries(epub)
    opf = entries[_opf_path(entries)].decode("utf-8", errors="replace")
    return _inner_text(DC_TITLE_RE, opf), _inner_text(DC_CREATOR_RE, opf)


def read_epub_cover(epub: Path) -> tuple[bytes, str, str] | None:
    """The cover image inside an EPUB: (data, media type, zip entry name)."""
    entries = _read_epub_entries(epub)
    opf_path = _opf_path(entries)
    opf = entries[opf_path].decode("utf-8", errors="replace")
    image_ids, _, _ = _find_cover_items(opf)
    if not image_ids:
        return None
    manifest = MANIFEST_RE.search(opf)
    if not manifest:
        return None
    for tag in XML_ITEM_RE.findall(manifest.group(2)):
        attrs = _tag_attributes(tag)
        if attrs.get("id") not in image_ids:
            continue
        name = _resolve_href(opf_path, attrs.get("href", ""))
        if name in entries:
            return entries[name], attrs.get("media-type") or "image/jpeg", name
    return None


# --------------------------------------------------------------------------- #
# Table of contents of an existing EPUB
# --------------------------------------------------------------------------- #


@dataclass
class TocEntry:
    """One line of the reader's chapter menu."""

    label: str
    href: str  # e.g. "text/ch004.xhtml" or "text/ch004.xhtml#anchor"
    depth: int = 1  # 1 = top level, 2 = nested inside the entry above it


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_menu_paths(opf_path: str, opf: str, book: dict[str, bytes]) -> tuple[str | None, str | None]:
    """Locate the NCX and the EPUB3 navigation document inside a book.

    Not every tool tags the nav document with ``properties="nav"`` (pandoc does
    not), so the id/href is checked as well, and as a last resort the XHTML files
    are searched for the ``epub:type="toc"`` marker.
    """
    ncx_path = nav_path = None
    xhtml_candidates: list[str] = []
    manifest = MANIFEST_RE.search(opf)
    if not manifest:
        return None, None
    for tag in XML_ITEM_RE.findall(manifest.group(2)):
        attrs = _tag_attributes(tag)
        href = _resolve_href(opf_path, attrs.get("href", ""))
        media_type = attrs.get("media-type", "")
        if media_type == "application/x-dtbncx+xml":
            ncx_path = href
        elif media_type == "application/xhtml+xml":
            marker = " ".join(
                (attrs.get("id", ""), attrs.get("href", ""), attrs.get("properties", ""))
            ).lower()
            if "nav" in marker:
                nav_path = href
            xhtml_candidates.append(href)
    if nav_path is None:
        for href in xhtml_candidates:
            content = book.get(href)
            if content and b'epub:type="toc"' in content:
                nav_path = href
                break
    return ncx_path, nav_path


def _resolve_href(opf_path: str, href: str) -> str:
    """Turn a manifest href (relative to the OPF) into a zip entry name."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("/"):
        return posixpath.normpath(href.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))


def _parse_ncx(ncx_text: str) -> list[TocEntry]:
    """Flatten an NCX navMap into entries, remembering how deeply each one nests."""

    def walk(node, depth: int, collected: list[TocEntry]) -> None:
        for child in node:
            if _local_name(child.tag) != "navPoint":
                continue
            label, href = "", ""
            for part in child:
                name = _local_name(part.tag)
                if name == "navLabel":
                    for text in part:
                        if _local_name(text.tag) == "text":
                            label = (text.text or "").strip()
                elif name == "content":
                    href = part.get("src", "").strip()
            collected.append(TocEntry(label=label, href=href, depth=depth))
            walk(child, depth + 1, collected)

    try:
        root = ElementTree.fromstring(ncx_text)
    except ElementTree.ParseError as error:
        raise ConversionError(f"could not read the table of contents: {error}") from error
    nav_map = None
    for node in root.iter():
        if _local_name(node.tag) == "navMap":
            nav_map = node
            break
    if nav_map is None:
        return []
    collected: list[TocEntry] = []
    walk(nav_map, 1, collected)
    return collected


NAV_TOKENS_RE = re.compile(r"</?ol\b[^>]*>|<a\b[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>", re.S | re.I)


def _parse_nav(nav_text: str) -> list[TocEntry]:
    """Fallback for EPUBs whose menu lives in nav.xhtml instead of an NCX."""
    block = nav_text
    match = re.search(r'<nav\b[^>]*epub:type="toc"[^>]*>(.*?)</nav>', nav_text, re.S | re.I)
    if match:
        block = match.group(1)
    collected: list[TocEntry] = []
    depth = 0
    for token in NAV_TOKENS_RE.finditer(block):
        text = token.group(0)
        if re.match(r"</ol", text, re.I):
            depth = max(1, depth - 1)
        elif re.match(r"<ol", text, re.I):
            depth += 1
        else:
            label = re.sub(r"<[^>]+>", "", token.group(2) or "").strip()
            collected.append(TocEntry(label=label, href=(token.group(1) or "").strip(),
                                      depth=max(1, depth)))
    return collected


def read_epub_toc(epub: Path) -> dict:
    """Read the book's chapter menu.

    Returns ``{title, entries, source, toc_path, base}``. ``toc_path`` is where
    the menu lives inside the zip and ``base`` is the folder its hrefs are
    relative to, which is what a caller needs to resolve an entry to a file.

    The NCX wins when the book has one (it is what most e-readers list); the
    EPUB3 navigation document is the fallback.
    """
    book = _read_epub_entries(epub)
    opf_path = _opf_path(book)
    opf = book[opf_path].decode("utf-8", errors="replace")
    title = _inner_text(DC_TITLE_RE, opf)
    ncx_path, nav_path = _find_menu_paths(opf_path, opf, book)
    for path, source, parser in (
        (ncx_path, "ncx", _parse_ncx),
        (nav_path, "nav", _parse_nav),
    ):
        if path and path in book:
            entries = parser(book[path].decode("utf-8", errors="replace"))
            return {
                "title": title,
                "entries": entries,
                "source": source,
                "toc_path": path,
                "base": posixpath.dirname(path),
            }
    raise ConversionError(
        "this EPUB has no table of contents to edit (neither an NCX nor a nav document)"
    )


def _normalise_depths(entries: list[TocEntry]) -> list[TocEntry]:
    """Clamp depths so the list always forms a valid tree (no skipped levels)."""
    fixed: list[TocEntry] = []
    for entry in entries:
        depth = max(1, min(int(entry.depth or 1), (fixed[-1].depth + 1) if fixed else 1))
        fixed.append(TocEntry(label=entry.label, href=entry.href, depth=depth))
    return fixed


def _toc_tree(entries: list[TocEntry]) -> list[tuple[TocEntry, list]]:
    """Nest a flat, depth-tagged list into (entry, children) pairs."""
    roots: list[tuple[TocEntry, list]] = []
    stack: list[tuple[TocEntry, list]] = []
    for entry in entries:
        node: tuple[TocEntry, list] = (entry, [])
        while stack and stack[-1][0].depth >= entry.depth:
            stack.pop()
        if stack:
            stack[-1][1].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _render_ncx_nodes(nodes: list[tuple[TocEntry, list]], indent: int,
                      order: Iterator[int]) -> list[str]:
    lines: list[str] = []
    for entry, children in nodes:
        number = next(order)
        pad = "  " * indent
        lines.append(f'{pad}<navPoint id="navPoint-{number}" playOrder="{number}">')
        lines.append(f"{pad}  <navLabel>")
        lines.append(f"{pad}    <text>{xml_escape(entry.label)}</text>")
        lines.append(f"{pad}  </navLabel>")
        lines.append(f'{pad}  <content src="{xml_escape(entry.href)}" />')
        lines.extend(_render_ncx_nodes(children, indent + 1, order))
        lines.append(f"{pad}</navPoint>")
    return lines


def build_ncx(title: str, uid: str, entries: list[TocEntry]) -> str:
    """Render a complete toc.ncx from the entry list."""
    lines = _render_ncx_nodes(_toc_tree(entries), 2, itertools.count(1))
    depth_attr = max((entry.depth for entry in entries), default=1)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="{xml_escape(uid)}" />\n'
        f'    <meta name="dtb:depth" content="{depth_attr}" />\n'
        '    <meta name="dtb:totalPageCount" content="0" />\n'
        '    <meta name="dtb:maxPageNumber" content="0" />\n'
        "  </head>\n"
        f"  <docTitle>\n    <text>{xml_escape(title)}</text>\n  </docTitle>\n"
        "  <navMap>\n" + "\n".join(lines) + "\n  </navMap>\n</ncx>\n"
    )


def _render_nav_nodes(nodes: list[tuple[TocEntry, list]], indent: int) -> list[str]:
    lines: list[str] = []
    for entry, children in nodes:
        pad = "  " * indent
        link = f'<a href="{xml_escape(entry.href)}">{xml_escape(entry.label)}</a>'
        if children:
            lines.append(f"{pad}<li>{link}")
            lines.append(f"{pad}  <ol>")
            lines.extend(_render_nav_nodes(children, indent + 2))
            lines.append(f"{pad}  </ol>")
            lines.append(f"{pad}</li>")
        else:
            lines.append(f"{pad}<li>{link}</li>")
    return lines


NAV_LIST_RE = re.compile(r"<ol\b[^>]*>", re.I)
NAV_TAG_RE = re.compile(r"</?ol\b[^>]*>", re.I)
TOC_NAV_RE = re.compile(r'<nav\b[^>]*epub:type="toc"[^>]*>', re.I)


def update_nav_document(nav_text: str, entries: list[TocEntry]) -> tuple[str, bool]:
    """Replace the list inside a nav document, leaving its wrapper alone.

    pandoc writes ``<div id="toc"><h1>…</h1><ol class="toc">…</ol></div>`` while
    other tools use ``<nav epub:type="toc"><ol>…</ol></nav>``; keeping whatever
    is already there means the book's own styling and ids survive. When the book
    has a proper toc nav, only that one is touched (landmarks and other navs have
    their own lists).
    """
    toc_nav = TOC_NAV_RE.search(nav_text)
    start = NAV_LIST_RE.search(nav_text, toc_nav.start() if toc_nav else 0)
    if not start:
        start = NAV_LIST_RE.search(nav_text)
    if not start:
        return nav_text, False
    items = _render_nav_nodes(_toc_tree(entries), 3)
    depth = 1
    position = start.end()
    while True:
        token = NAV_TAG_RE.search(nav_text, position)
        if not token:
            return nav_text, False
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return (
                nav_text[: start.end()] + "\n" + "\n".join(items) + "\n" + nav_text[token.start():],
                True,
            )
        position = token.end()


def write_epub_toc(
    epub: Path,
    output_dir: Path,
    entries: list[TocEntry],
    log: LogFn = log_stdout,
) -> dict:
    """Write an edited table of contents back into a copy of an EPUB.

    Only the menu changes: the chapter files, the spine (reading order), the
    images and the cover are copied over untouched, so a wrong label or a level
    that nests badly can be fixed without touching the book itself.
    """
    epub = epub.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not epub.exists():
        raise ConversionError(f"EPUB not found: {epub}")
    book_entries = _read_epub_entries(epub)
    opf_path = _opf_path(book_entries)
    opf = book_entries[opf_path].decode("utf-8", errors="replace")
    title = _inner_text(DC_TITLE_RE, opf) or epub.stem
    uid = _inner_text(re.compile(r"(<dc:identifier\b[^>]*>)(.*?)(</dc:identifier>)", re.S), opf)

    cleaned = [TocEntry(label=(e.label or "").strip(), href=(e.href or "").strip(),
                        depth=int(e.depth or 1)) for e in entries]
    cleaned = [e for e in cleaned if e.href]
    if not cleaned:
        raise ConversionError("the table of contents would be empty - nothing was written")
    cleaned = _normalise_depths(cleaned)

    ncx_path, nav_path = _find_menu_paths(opf_path, opf, book_entries)

    # Entries are relative to the menu file, so that is the folder to resolve against.
    base = posixpath.dirname(ncx_path or nav_path or opf_path)
    missing = [
        entry.href for entry in cleaned
        if posixpath.normpath(posixpath.join(base, entry.href.split("#")[0])) not in book_entries
    ]

    log(f"== {epub.name}")
    log(f"   table of contents: {len(cleaned)} entries, {max(e.depth for e in cleaned)} level(s)")
    if missing:
        log(f"   !! {len(missing)} entr(ies) point at a page that is not in the book,")
        log(f"      e.g. {missing[0]} - those menu lines will not open anything")

    if ncx_path and ncx_path in book_entries:
        book_entries[ncx_path] = build_ncx(title, uid, cleaned).encode("utf-8")
    else:
        log("   !! this EPUB has no NCX, so simple readers will keep their old menu")
    if nav_path and nav_path in book_entries:
        updated, ok = update_nav_document(
            book_entries[nav_path].decode("utf-8", errors="replace"), cleaned
        )
        if ok:
            book_entries[nav_path] = updated.encode("utf-8")
        else:
            log("   !! could not find the list in the navigation document; left as it was")

    report = inspect_epub(epub)
    target = output_dir / f"{sanitize_name(title)}.epub"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_epub(target, book_entries)
    log(f"== done: {target}  ({report['chapters']} chapters kept, {len(cleaned)} menu entries)")
    return {
        "epub": target,
        "kind": "epub",
        "mode": "toc",
        "title": title,
        "author": _inner_text(DC_CREATOR_RE, opf),
        "toc_entries": len(cleaned),
        **report,
    }


def _set_element(opf: str, pattern: re.Pattern, tag: str, value: str) -> str:
    """Replace the text of an OPF metadata element, adding it if it is missing."""
    opf, count = pattern.subn(lambda m: m.group(1) + xml_escape(value) + m.group(3), opf, count=1)
    if count:
        return opf
    return re.sub(
        r"\s*</metadata>",
        f"\n    <{tag}>{xml_escape(value)}</{tag}>\n  </metadata>",
        opf,
        count=1,
    )


def _write_epub(target: Path, entries: dict[str, bytes]) -> None:
    """Write an EPUB: 'mimetype' first and uncompressed, the rest deflated."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if "mimetype" in entries:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, entries["mimetype"])
        for name, data in entries.items():
            if name != "mimetype":
                archive.writestr(name, data)


def update_epub_metadata(
    epub: Path,
    output_dir: Path,
    title: str | None = None,
    author: str | None = None,
    cover: Path | None = None,
    log: LogFn = log_stdout,
) -> dict:
    """Rewrite the title/author/cover of an existing EPUB, leaving everything else alone.

    The book's own files are edited directly - the OPF metadata, the NCX
    docTitle, the navigation document, the title page and the cover - so the
    chapter structure, stylesheet, images and ids come through untouched. That is
    much safer than converting the EPUB back through pandoc just to fix a name.
    """
    epub = epub.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not epub.exists():
        raise ConversionError(f"EPUB not found: {epub}")

    entries = _read_epub_entries(epub)
    opf_path = _opf_path(entries)
    opf = entries[opf_path].decode("utf-8", errors="replace")
    old_title = _inner_text(DC_TITLE_RE, opf)
    old_author = _inner_text(DC_CREATOR_RE, opf)
    new_title = (title if title is not None else old_title).strip()
    new_author = (author if author is not None else old_author).strip()
    if not new_title:
        raise ConversionError(
            "this EPUB contains no title and none was given - fill in the Title field"
        )

    log(f"== {epub.name}")
    log(f"   stored:          {old_title or '(none)'} | {old_author or '(none)'}")
    log(f"   writing:         {new_title} | {new_author or '(none)'}")

    touched: list[str] = []
    if new_title != old_title:
        opf = _set_element(opf, DC_TITLE_RE, "dc:title", new_title)
        touched.append(opf_path)
    if new_author != old_author:
        opf = _set_element(opf, DC_CREATOR_RE, "dc:creator", new_author)
        if opf_path not in touched:
            touched.append(opf_path)
    entries[opf_path] = opf.encode("utf-8")

    has_cover = False
    cover_entry = ""
    if cover:
        touched_by_cover, cover_entry = _apply_cover(entries, opf_path, cover, log)
        for name in touched_by_cover:
            if name not in touched:
                touched.append(name)
        has_cover = True

    for name in list(entries):
        lowered = name.lower()
        if not lowered.endswith((".xhtml", ".html", ".ncx")):
            continue
        text = entries[name].decode("utf-8", errors="replace")
        updated = text
        if lowered.endswith(".ncx"):
            # The NCX docTitle and the table-of-contents labels.
            updated = NCX_TITLE_RE.subn(
                lambda m: m.group(1) + xml_escape(new_title) + m.group(3), updated, count=1
            )[0]
        else:
            if old_title:
                # Only documents that carry the book title in <title> (pandoc's nav
                # document does, chapter files carry their own filename).
                updated = HTML_TITLE_RE.subn(
                    lambda m: m.group(1) + xml_escape(new_title) + m.group(3)
                    if xml_unescape(m.group(2)).strip() == old_title else m.group(0),
                    updated,
                )[0]
            updated = TITLE_PAGE_TITLE_RE.subn(
                lambda m: m.group(1) + xml_escape(new_title) + m.group(3), updated, count=1
            )[0]
            if new_author:
                # Add the author paragraph if the title page has none.
                if TITLE_PAGE_AUTHOR_RE.search(updated):
                    updated = TITLE_PAGE_AUTHOR_RE.subn(
                        lambda m: m.group(1) + xml_escape(new_author) + m.group(3),
                        updated, count=1,
                    )[0]
                elif '<h1 class="title"' in updated:
                    updated = updated.replace(
                        "</h1>", f"</h1>\n  <p class=\"author\">{xml_escape(new_author)}</p>", 1
                    )
        if updated != text:
            entries[name] = updated.encode("utf-8")
            if name not in touched:
                touched.append(name)

    target = output_dir / f"{sanitize_name(new_title)}.epub"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_epub(target, entries)
    log(f"   updated:         {', '.join(touched) or 'nothing'}")

    report = inspect_epub(target)
    log(f"== done: {target}  ({report['chapters']} chapters kept, {report['size_mb']} MB)")
    return {
        "epub": target,
        "kind": "epub",
        "mode": "metadata",
        "title": new_title,
        "author": new_author,
        "cover": has_cover,
        "cover_entry": cover_entry,
        **report,
    }


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #


def sanitize_name(name: str, fallback: str = "book") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:90] or fallback


def title_from_filename(stem: str) -> str:
    """Turn a scene-style PDF filename into something worth showing in a library."""
    name = stem
    name = re.sub(r"^_?(?:OceanofPDF\.com|z-lib\.org|libgen)[_\-\s]*", "", name, flags=re.I)
    name = re.sub(r"[\[(](?:BookZZ\.org|BookFi|z-lib\.org|OceanofPDF)[\])]", "", name, flags=re.I)
    name = re.sub(r"^\s*\[[^\]]{1,60}\]\s*", "", name)  # leading [Author_Name]
    name = re.sub(r"^\s*\([^)]{1,60}\)\s*", "", name)
    name = name.replace("_", " ")
    name = re.sub(r"\s*-\s*", " - ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -_.")
    return name or stem


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


@dataclass
class ConvertOptions:
    pdf: Path
    output_dir: Path
    title: str | None = None
    author: str | None = None
    cover: Path | None = None
    kind: str = "auto"  # auto | digital | scanned
    fast_ocr: bool = False  # scanned only: skip layout/figure extraction
    split_level: int | None = None  # None = detect from the markdown
    toc_depth: int | None = None
    toc_tables_to_lists: bool = True
    rebuild_only: bool = False  # reuse an existing markdown, only rebuild the EPUB
    work_dir: Path | None = None
    page_range: str | None = None
    keep_raw_markdown: bool = True


def work_directory(override: Path | None = None) -> Path:
    """Scratch space for uploads, flattened scans and marker's raw output.

    Deliberately outside the project folder: intermediate files are large, and
    nobody wants a 300 MB flattened scan syncing to Google Drive.
    """
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "pdf2epub" / "_work"


def book_folder(output_dir: Path, pdf: Path) -> Path:
    return output_dir / sanitize_name(pdf.stem)


def convert(
    options: ConvertOptions,
    log: LogFn = log_stdout,
    should_stop: StopFn | None = None,
) -> dict:
    """Run the whole PDF -> markdown -> EPUB pipeline. Returns a summary dict."""
    pdf = options.pdf.expanduser().resolve()
    output_dir = options.output_dir.expanduser().resolve()
    if not pdf.exists():
        raise ConversionError(f"PDF not found: {pdf}")
    output_dir.mkdir(parents=True, exist_ok=True)

    work = work_directory(options.work_dir)
    out_folder = book_folder(output_dir, pdf)
    out_folder.mkdir(parents=True, exist_ok=True)

    log(f"== {pdf.name}")
    log(f"   output folder:   {output_dir}")

    info: PdfInfo | None = None
    kind = options.kind
    if not options.rebuild_only or options.kind == "auto":
        info = read_pdf_info(pdf)
        log(f"   pdf:             {info.describe()}")
    if kind == "auto":
        assert info is not None
        kind = classify_pdf(info)
    log(f"   pdf type:        {kind}" + ("" if options.kind != "auto" else " (auto-detected)"))

    raw_markdown: Path
    if options.rebuild_only:
        raw_markdown = find_existing_markdown(out_folder, pdf)
        log(f"   reusing markdown: {raw_markdown}")
    else:
        source_pdf = pdf
        if kind == "scanned" and not options.fast_ocr:
            source_pdf = flatten_pdf(
                pdf, work / "flat" / f"{sanitize_name(pdf.stem)}-flat.pdf", log,
                should_stop=should_stop,
            )
        raw_markdown = run_marker(
            source_pdf,
            work / "extract",
            kind,
            options.fast_ocr,
            log,
            should_stop=should_stop,
            page_range=options.page_range,
        )

    # Keep a copy of marker's untouched output, then clean a working copy that
    # the user can edit and re-convert with rebuild-only.
    stem = sanitize_name(pdf.stem)
    markdown = out_folder / f"{stem}.md"
    raw_copy = out_folder / f"{stem}.raw.md"
    if raw_markdown.resolve() != markdown.resolve():
        if options.keep_raw_markdown and raw_markdown.resolve() != raw_copy.resolve():
            shutil.copy2(raw_markdown, raw_copy)
        shutil.copy2(raw_markdown, markdown)
        for name in referenced_images(raw_markdown):
            image = raw_markdown.parent / name
            if image.is_file():
                shutil.copy2(image, out_folder / name)
            else:
                log(f"   !! image referenced but missing next to the markdown: {name}")
    else:
        log("   markdown already lives in the output folder")

    clean_markdown(markdown, log, convert_toc_tables=options.toc_tables_to_lists)

    counts = heading_levels(markdown.read_text(encoding="utf-8"))
    summary = ", ".join(f"h{level}: {count}" for level, count in sorted(counts.items()))
    log(f"   headings:        {summary or 'none'}")
    split_level = options.split_level or pick_split_level(counts)
    toc_depth = options.toc_depth or split_level
    log(f"   split level:     h{split_level} (toc depth {toc_depth})")

    title = options.title or (info.meta_title if info else "") or title_from_filename(pdf.stem)
    author = options.author or (info.meta_author if info else "") or "Unknown"
    epub = output_dir / f"{sanitize_name(title)}.epub"
    log(f"   title:           {title}")
    log(f"   author:          {author}")

    build_epub(
        markdown, epub, title, author, split_level, toc_depth, options.cover, log,
        should_stop=should_stop,
    )

    report = inspect_epub(epub)
    cover_entry = ""
    if options.cover and Path(options.cover).expanduser().exists():
        # pandoc embedded it; find where, so the UI can show the cover back.
        found = read_epub_cover(epub)
        cover_entry = found[2] if found else ""
    log(
        f"== done: {epub}  "
        f"({report['chapters']} chapters, {report['toc_entries']} toc entries, "
        f"{report['images']} images, {report['size_mb']} MB)"
    )
    if report["chapters"] < 2:
        log("   !! only one chapter file was produced - check the heading levels and set")
        log("      the split level manually if the book has chapters.")
    elif report["toc_entries"] < report["chapters"]:
        log("   note: the chapter menu has fewer entries than there are chapter files -")

    return {
        "epub": epub,
        "markdown": markdown,
        "book_folder": out_folder,
        "kind": kind,
        "mode": "convert",
        "title": title,
        "author": author,
        "cover": bool(options.cover and Path(options.cover).expanduser().exists()),
        "cover_entry": cover_entry,
        "split_level": split_level,
        **report,
    }


def find_existing_markdown(folder: Path, pdf: Path) -> Path:
    """Find the markdown to rebuild from (prefers the cleaned file, not the .raw.md)."""
    candidates = [
        path for path in sorted(folder.glob("*.md"))
        if not path.name.endswith(".raw.md")
    ]
    if not candidates:
        raise ConversionError(
            f"there is no earlier conversion of this book in {folder}, so there is "
            "nothing to write the title/author into - convert the PDF first, or turn "
            "off 'Rebuild the EPUB only'."
        )
    exact = [path for path in candidates if path.stem == pdf.stem]
    return exact[0] if exact else candidates[0]


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert PDF books to e-ink friendly EPUB files (marker for text "
                    "extraction, pandoc for the EPUB), or rewrite the title/author of "
                    "an EPUB that already exists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               '  python pdf2epub.py "book.pdf" --output-dir "D:\\Books\\epub"\n'
               '  python pdf2epub.py "wrong title.epub" --output-dir "D:\\Books\\epub" '
               '--title "Right Title" --author "Right Author"\n',
    )
    parser.add_argument("input", type=Path,
                        help="the PDF to convert, or an EPUB whose title/author to rewrite")
    parser.add_argument("-o", "--output-dir", type=Path, required=True,
                        help="folder for the EPUB and the markdown/images it was built from")
    parser.add_argument("--type", dest="kind", choices=["auto", "digital", "scanned"],
                        default="auto", help="PDF kind (default: detect automatically)")
    parser.add_argument("--fast-ocr", action="store_true",
                        help="scanned PDFs: OCR whole pages instead of keeping the "
                             "layout model (about 2x faster, but figures inside the "
                             "scanned pages are not extracted)")
    parser.add_argument("--title", help="book title (default: metadata inside the file)")
    parser.add_argument("--author", help="author (default: metadata inside the file)")
    parser.add_argument("--cover", type=Path, help="image to use as the EPUB cover")
    parser.add_argument("--split-level", type=int,
                        help="heading level that starts a new chapter file (default: detect)")
    parser.add_argument("--toc-depth", type=int, help="TOC depth (default: split level)")
    parser.add_argument("--page-range", help="only convert these pages, e.g. 0,5-10,20")
    parser.add_argument("--rebuild-only", action="store_true",
                        help="skip extraction and rebuild the EPUB from the existing "
                             "markdown in the output folder (edit it first!)")
    parser.add_argument("--no-toc-tables", action="store_true",
                        help="keep printed table-of-contents tables as tables instead of "
                             "converting them to bullet lists")
    parser.add_argument("--work-dir", type=Path,
                        help="scratch folder (default: %%LOCALAPPDATA%%\\pdf2epub\\_work)")
    args = parser.parse_args(argv)

    try:
        if args.input.suffix.lower() == ".epub":
            # An EPUB in, the same EPUB with corrected title/author/cover out.
            update_epub_metadata(
                args.input, args.output_dir, title=args.title, author=args.author,
                cover=args.cover,
            )
            return 0
        options = ConvertOptions(
            pdf=args.input,
            output_dir=args.output_dir,
            title=args.title,
            author=args.author,
            cover=args.cover,
            kind=args.kind,
            fast_ocr=args.fast_ocr,
            split_level=args.split_level,
            toc_depth=args.toc_depth,
            toc_tables_to_lists=not args.no_toc_tables,
            rebuild_only=args.rebuild_only,
            work_dir=args.work_dir,
            page_range=args.page_range,
        )
        convert(options, log_stdout)
    except ConversionError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
