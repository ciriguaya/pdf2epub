# pdf2epub-eink

Turn PDF books into **reflowable EPUBs with a chapter menu that actually works on
e-ink readers** — including the ones with very simple firmware (Crosspoint,
older Kindle/Kobo, and anything that only understands EPUB2 + `toc.ncx`).

It handles the two kinds of PDF in the wild, and works out which is which by
itself:

| | |
| --- | --- |
| **Born-digital PDF** | Reads the PDF's own text layer. Fast, CPU-only, ~70 s for 200 pages. |
| **Scanned PDF** | Re-renders the pages without their (usually bad) OCR layer, then runs full layout-aware OCR on your GPU. |

Two PDFs of the same book can be wildly different, so the tool checks how much
real text and how many full-page images a PDF has and picks the right pipeline.
You can override that per book in the UI.

> ### Built with AI — and it is a work in progress
>
> This tool was built in an AI-assisted session: it started as a pile of shell
> commands and grew into a small pipeline with a UI, with every step tested on
> real books along the way. It is **not** a polished product.
>
> In practice it is **quite reliable at the parts it was built for**: the two test
> books (~330 pages total, one digital and one scanned) convert cleanly and repeat,
> the EPUB structure is verified after every run, and the EPUB-adjusting tools
> (cover, metadata, table of contents) edit the book's own files rather than
> rebuilding it, so they cannot damage the text.
>
> What that means for you: expect rough edges outside the tested paths, be
> suspicious of hard-coded assumptions, and keep your original files.
>
> **Suggestions, bug reports and pull requests are very welcome** — especially
> from anyone whose books look different from the two this was tested with
> (different languages, tables, footnotes, two-column layouts, EPUB3-only
> readers). See [Contributing](#contributing).

---

## What it can do

* **PDF → EPUB** with a chapter menu that navigates properly on e-ink hardware.
  The EPUB is EPUB2 with a real `toc.ncx`, split into **one internal file per
  chapter** — that last part is what makes chapter jumping work on readers whose
  firmware ignores anchors inside a file.
* **Sensible chapters without hand-holding.** marker's heading levels are
  inconsistent, so the tool counts them and picks the level that behaves like a
  chapter (calibrated against four real books), and tells you what it chose.
  You can override it.
* **Automatic pipeline choice** between text-layer extraction and full OCR.
* **Clean-up of marker's output**: empty image tags, invisible page anchors, and
  tables that are really a printed table of contents are turned into lists.
* **Cover images**: set or replace the cover of a converted book, or of an EPUB
  you already have. Oversized images are scaled down and re-encoded, and the UI
  shows you the cover that is actually inside the file.
* **Metadata fixes for existing EPUBs**: correct title/author/cover without
  touching a single chapter, image or stylesheet.
* **Table-of-contents editor**: rename menu lines, change how deeply they nest,
  move them (with their children), and preview the page each line opens —
  straight from inside the book, with the saved file kept in sync.
* **Runs entirely offline.** No cloud services, no uploads, no API keys.
  Images are embedded, so an EPUB is one self-contained file.

## Requirements

**Tested on:** Windows 11 + an NVIDIA GPU. The code is Windows-first (process
handling, file manager integration); the conversion core is portable, so
Linux/macOS should mostly work but has not been tested — see Contributing.

| Piece | Version used | Notes |
| --- | --- | --- |
| Python | 3.11 | in a dedicated virtualenv |
| [marker-pdf](https://github.com/datalab-to/marker) | 2.0.0 | text extraction, layout, OCR |
| surya-ocr | 0.22.1 | pulled in by marker |
| PyTorch | 2.6.0+cu124 | the newest CUDA 12.4 wheel |
| [llama.cpp](https://github.com/ggml-org/llama.cpp/releases) | `llama-server.exe`, b10199 tested | marker's OCR serves its vision model through this |
| [Pandoc](https://pandoc.org) | 3.1.7+ (3.10 used) | writes the EPUB |
| NVIDIA GPU | RTX 3090 (24 GB) | **only needed for scanned PDFs**; the text-layer path is CPU-only |

Rough sizing: a 200-page digital book converts in about a minute; a 120-page
scanned book takes about 20 minutes of GPU time.

## Install

```powershell
python -m venv C:\Users\<you>\marker_env
C:\Users\<you>\marker_env\Scripts\Activate.ps1
pip install --upgrade pip
pip install marker-pdf
pip install torch==2.6.0+cu124 torchvision --index-url https://download.pytorch.org/whl/cu124 --force-reinstall
```

Then, outside the virtualenv:

```powershell
winget install --exact --id JohnMacFarlane.Pandoc
```

and put `llama-server.exe` from a llama.cpp Windows CUDA build somewhere on your
`PATH` (or in `C:\Tools\llama.cpp\`, or point `LLAMA_CPP_BINARY` at it).

Two things that will otherwise bite you, and which the tool handles for you:

* surya shuts its model server down with `os.kill(pid, 0)`, which on Windows
  terminates nothing — it leaves a ~1.5 GB process behind. The tool stops those
  itself.
* surya sends a regex containing `\d` to llama-server, which current llama.cpp
  builds reject with `failed to parse grammar`. The tool patches that to `[0-9]`
  automatically before every OCR run.

> Known upstream quirk: marker-pdf's metadata asks for `torch>=2.7`, which does
> not exist for CUDA 12.4. pip's conflict warnings about torch and pillow are
> expected; do not "fix" them by installing a plain `torch`, or you will get a
> CPU-only build and very slow OCR.

## Use it

### The drag & drop UI (recommended)

```powershell
start_ui.bat
```

It starts a small local web UI on `http://127.0.0.1:8765` and opens your browser.
If your virtualenv is not `%USERPROFILE%\marker_env`, set `PDF2EPUB_PYTHON` to its
`python.exe` first.

Then: drag PDFs (or EPUBs) onto the page, check the detected pipeline, fill in
title/author, pick an output folder, press **Convert**. Progress streams into a
log pane; you can stop a job, and results show the chapter/menu/image counts of
what was produced.

### Command line

```powershell
C:\Users\<you>\marker_env\Scripts\python.exe pdf2epub.py "book.pdf" --output-dir "D:\Books\epub"

# a scanned book, with metadata and a cover
... pdf2epub.py "scan.pdf" --output-dir "D:\out" --type scanned `
      --title "Title" --author "Author" --cover "D:\art\cover.jpg"

# fix an EPUB you already converted
... pdf2epub.py "old title.epub" --output-dir "D:\out" --title "Right Title"

# after editing the markdown by hand: rebuild in seconds, no re-OCR
... pdf2epub.py "book.pdf" --output-dir "D:\out" --rebuild-only
```

`pdf2epub.py --help` lists everything. Each run leaves two things in the output
folder: the `.epub`, and a folder with the cleaned markdown plus the extracted
images (and an untouched `.raw.md` as a safety net) so you can fix subtitles or
headings by hand and rebuild.

## Verified on real books

| Book | Pages | Pipeline | Time | Result |
| --- | --- | --- | --- | --- |
| Mark Fisher, *Ghosts of My Life* | 208 | digital | ~1.5 min | 20 chapters, 21 menu entries, 5 images, 0.85 MB |
| Kodwo Eshun, *More Brilliant than the Sun* | 121 | scanned + OCR | ~22 min | 212 chapters, 213 menu entries, 0.38 MB |

The scanned book is the interesting one: its PDF carried a bad OCR layer
("Turntobilizotion = AutoDestruction"). Dropping that layer and re-OCRing gives
"Turntabilization = AutoDestruction".

Automated checks in the repo's development cycle covered: EPUB structure after
each run (chapter files, `toc.ncx` entries, embedded media, no unreferenced
images), the metadata and cover editors, and the table-of-contents round trip
(labels, nesting, and that only the two menu files change).

## How it works, in one paragraph

[marker](https://github.com/datalab-to/marker) converts the PDF to markdown
(either from its text layer, or via its vision model for scans), the markdown is
cleaned up, and [Pandoc](https://pandoc.org) writes an EPUB2 with
`--split-level=<chapter level>`, which produces one internal XHTML file per
chapter. The scanned path adds one trick: the PDF's pages are re-rendered into a
copy with no text layer first, so marker cannot trust the bad OCR layer that
ships inside most scanned PDFs. The EPUB-adjusting tools (cover, metadata, TOC)
edit the zip's own files instead of rebuilding the book, so nothing else can
change.

## Known limitations

* **Scans need a GPU** and take ~10–20 s per page.
* **Only figures are extracted as images.** A scanned page is not "an image" to a
  reader; for scanned books you should expect few pictures in the result.
* **A PDF whose text layer is bad OCR looks "digital"** to the detector, because
  there *is* text. If the output is nonsense, set the pipeline to `scanned` — that
  path throws the old layer away.
* **Two-column layouts, footnotes and complex tables** are only as good as
  marker's output. The markdown is always left in the output folder precisely so
  you can fix things and rebuild.
* **The TOC editor renames menu lines, not headings in the text.** To change the
  text itself, edit the markdown and rebuild.
* **Windows-first.** macOS/Linux support is untested (process cleanup and the
  native folder picker are the Windows-specific parts).

The detailed manual — every option, the split-level rules, the environment, and a
troubleshooting table — is in [MANUAL.md](MANUAL.md).

## Contributing

This is a side project, and it is genuinely open to help. Most useful right now:

* **Bug reports** with the log output (the log pane in the UI has everything) and,
  if you can share it, what the PDF looked like.
* **Books that break it**: a different language, a two-column academic PDF, heavy
  footnotes, an EPUB3-only reader, a book whose chapters are detected badly.
* **The chapter-split heuristic** (MANUAL.md §6) — it was calibrated on four books;
  more data points would make it better.
* **Platform support**: making the process handling and folder picker work on
  macOS/Linux.
* **Guessable defaults**: the tool auto-detects PDF type and chapter level, but
  those heuristics deserve more testing than one person can give them.

Pull requests are welcome. Nothing here is precious — the code is a few hundred
lines of Python with no dependencies beyond marker, Pillow, pypdfium2 and pandoc.

## Notes

* **Licence: MIT** — see [LICENSE](LICENSE). It is deliberately permissive so the
  code can be reused, forked and shipped. The pieces it depends on keep their own
  licences; the one that needs care is explained below.
* **No book files are in this repository.** `.gitignore` keeps PDFs, EPUBs and
  the sample output out of it. Use this on documents you are allowed to convert.
* Please report issues rather than opening PRs that add books as test fixtures.

### Dependencies and their licences

Everything the code depends on is permissively licensed (Apache-2.0, BSD-3-Clause,
MIT, MIT-CMU), and Pandoc — which is GPL — is only ever *executed* as a separate
program, never linked, so nothing here inherits its copyleft.

**One exception worth reading:** marker's model weights, which the *scanned-PDF*
path downloads on first use, are under datalab's **OpenRAIL-M** licence rather than
Apache-2.0. That licence permits personal, research and commercial use, but it
forbids use by organisations above a US$5M revenue/funding threshold, requires
attribution, and passes its use restrictions on to whoever you distribute to.

There is a short, plain-language breakdown — plus the full list of components and
licences — in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). If you only ever
use the digital/text-layer path, no model weights are involved at all.

## Files

| File | What it is |
| --- | --- |
| `pdf2epub_ui.py` | the drag & drop UI (stdlib `http.server`, no extra dependencies) |
| `pdf2epub.py` | the pipeline: detection, extraction, clean-up, Pandoc, EPUB editing |
| `start_ui.bat` | launcher that finds the marker virtualenv |
| `requirements.txt` | the packages and the exact install sequence |
| `MANUAL.md` | the detailed manual and troubleshooting table |
| `THIRD-PARTY-NOTICES.md` | licences of the components this depends on |
