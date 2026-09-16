# pdf2epub-eink — detailed manual

This is the long-form documentation: how the two pipelines work, what every option
does, how the chapter menu is decided, the environment it needs, and what to do
when something goes wrong. For a quick overview see [README.md](README.md).

---

## 1. Files in this folder

| File | What it is |
| --- | --- |
| `start_ui.bat` | **Double-click this.** Starts the drag & drop UI with the right Python. |
| `pdf2epub_ui.py` | The drag & drop UI (a small local web site, stdlib only). |
| `pdf2epub.py` | The conversion pipeline itself; also usable from the command line. |
| `requirements.txt` | The Python packages, and the exact sequence that installs them. |
| `README.md` | Overview, requirements, verified results, contributing. |
| `LICENSE` | MIT — the licence of the code in this repository. |
| `THIRD-PARTY-NOTICES.md` | What the dependencies are licensed under (read this before commercial use). |
| `.gitignore` | Keeps books, converted output and scratch files out of the repository. |

---

## 2. Quick start – the UI

1. Double-click **`start_ui.bat`**. A console window opens and your browser
   shows `http://127.0.0.1:8765/`. (Keep the console window open while you work;
   closing it stops the UI. Set `PDF2EPUB_PYTHON` if your virtualenv lives
   somewhere other than `%USERPROFILE%\marker_env`.)
2. **Drag your PDF files (or EPUBs) onto the drop area** (or click it to browse).
   Each file is analysed and shows which pipeline it will use:
   * `digital` – the fast text-layer path
   * `scanned` – it will be OCR'd (this is the slow one)
   * `epub` – only its title/author (and cover, if you set one) will be rewritten,
     and its table of contents can be reviewed and fixed

   A dropdown next to every PDF lets you overrule that choice.
3. Give each book its **Title** and **Author** – those two fields are written into
   the EPUB metadata, and the title is also the filename. They come prefilled from
   the PDF's own metadata (falling back to the filename), so usually you only need
   to correct them. For an EPUB the fields show what is stored in the file.
4. Set the **output folder** (it is remembered between sessions).
5. Optionally add a **cover image** – it becomes the cover of a converted PDF, or
   is added to / replaces the cover of an EPUB (see section 4). The field shows a
   preview of the image you picked.
6. Save your edits, either way:
   * **Convert** writes the whole queue (title/author included) - that is what you
     press after dropping PDFs.
   * **save metadata** on a single book card writes just that book's title and
     author, without re-running the extraction. On a PDF it reuses the markdown
     from an earlier run, so it takes seconds rather than minutes.

   The button is labelled **Save metadata** by itself when the queue only holds
   EPUBs, and the line under it always tells you what it will do - or which field
   is still missing while it is greyed out. **Stop** cancels a running job, and the
   log at the bottom shows the progress (including `... 25/121 pages rendered`
   while a scan is being prepared).
7. When it finishes you get, in your output folder:
   * `Title.epub` – the file to copy onto your reader
   * `Title/` – the folder it was built from: the cleaned markdown and the
     extracted images. Edit the `.md` here if something is off, then tick
     **"Rebuild the EPUB only"** and press Convert again (takes seconds instead
     of re-running the 20-minute OCR).

---

## 3. Quick start – command line

Run the pipeline with the marker environment's Python (activating the
environment is not required when you use the full path):

```powershell
C:\Users\<you>\marker_env\Scripts\python.exe pdf2epub.py "G:\path\to\book.pdf" --output-dir "G:\path\to\converted"
```

More examples:

```powershell
# scanned book, explicit title/author and a cover image
... pdf2epub.py "book.pdf" --output-dir "G:\converted" --type scanned `
      --title "Title" --author "Author" --cover "G:\covers\cover.jpg"

# test run on a few pages first (cheap way to check the output quality)
... pdf2epub.py "book.pdf" --output-dir "G:\converted\test" --page-range 20-40

# fix the metadata or cover of an EPUB you already have
... pdf2epub.py "old title.epub" --output-dir "G:\converted" `
      --title "Right Title" --author "Right Author" --cover "G:\covers\new.jpg"

# after editing the markdown by hand: rebuild only, no extraction
... pdf2epub.py "book.pdf" --output-dir "G:\converted" --rebuild-only
```

`pdf2epub.py --help` lists everything.

### What the script actually runs

So you can always do it by hand if you want to:

```powershell
# 1. digital PDF: read the text layer
marker_single "book.pdf" --output_dir "out" --mode fast --disable_ocr

# 1b. scanned PDF instead: first rebuild the PDF as plain page images (that is
#     what pdf2epub.flatten_pdf() does - it renders every page at 300 dpi and
#     writes a new PDF with no text layer, so marker cannot reuse the bad one),
#     then run marker on that copy:
marker_single "book-flat.pdf" --output_dir "out"

# 2. build the EPUB, one internal file per level-2 heading
pandoc "out/book/book.md" -o "Book.epub" -t epub2 --toc --toc-depth=2 `
  --split-level=2 --resource-path "out/book" `
  --metadata title="Book" --metadata author="Author"
```

---

## 4. Fixing an EPUB you already have

Wrong title or author in your library? Drop the EPUB into the UI, correct the
**Title**/**Author** fields and press **Save metadata** (or **Convert**) – you get
a corrected copy in your output folder. The same button on a *PDF* card fixes a
book you already converted: it reuses the markdown that is already in the output
folder, so it takes seconds instead of re-running a 20-minute OCR pass.

From the command line:

```powershell
... pdf2epub.py "old title.epub" --output-dir "G:\converted" --title "Right Title" --author "Right Author"
```

The EPUB route changes nothing else about the book: the chapter files, the
stylesheet, the images, the ids and the table of contents are copied over as they
were, only the metadata and the title page are rewritten.

What gets updated inside the EPUB (all of it, so every reader shows the same
thing):

| Place | Contains |
| --- | --- |
| `EPUB/content.opf` | `<dc:title>` and `<dc:creator>` – the metadata a reader/library actually lists. A missing `<dc:creator>` is added. |
| `EPUB/toc.ncx` | the `<docTitle>` at the top of the chapter menu |
| `EPUB/nav.xhtml` | the document title |
| `EPUB/text/title_page.xhtml` | the first page you see when opening the book |
| `EPUB/media/cover.*`, `EPUB/text/cover.xhtml`, the manifest/spine/guide entries | the cover, when you set one – see below |

Leave a field empty to keep what is stored in the file (so an author-only fix
needs just the author). The corrected file is written as
`<output folder>\<Title>.epub`; your original file is never modified, because the
tool only ever works on its own copy.

### The cover image

The **Cover image** field works for both kinds of input:

* **PDF conversion** – the image is packaged as the book's cover (pandoc's
  `--epub-cover-image`, with a proper cover page at the front of the spine).
* **EPUB input** – the cover is added if the book has none, or replaces the
  existing one if it has. Everything else about the book is left alone.

Either way the cover is embedded *inside* the EPUB, so it shows up in the
library, on the lock screen and in the reader's book list. Details worth knowing:

* **You can check it in the UI.** Every EPUB you drop shows its current cover as a
  thumbnail right on its card ("the cover inside this book"), the cover field shows
  a preview of the image you picked ("will be used as the cover"), and every
  result shows the cover that ended up in the saved file. So there is no need to
  open the book somewhere else just to see whether the cover is right.
* Images that are not JPEG/PNG (webp, bmp, …) are converted to JPEG, and anything
  larger than 1600 px on the long edge is scaled down – `--cover` accepts a huge
  photo without bloating the book. The log says exactly what was done.
* The previous cover file is deleted when it is replaced, so the book does not
  accumulate cover images.
* `--cover "G:\path\to\image.jpg"` does the same from the command line, for both
  PDFs and EPUBs.

### The table of contents

Drop an EPUB and press **edit table of contents (N)** on its card to review and fix
the chapter menu:

* **« »** make a line one level shallower or deeper – so a flat list can become
  parts with chapters inside them.
* **↑ ↓** move a line; everything nested under it moves with it. A line never
  jumps over its own parent, and two lines at the same level simply swap.
* The **text field** is what the reader's menu shows – fix a title there if the
  conversion got it wrong.
* **show** (or clicking the row) opens that page in the preview pane next to the
  list, exactly as the reader will open it, with the book's own styling and
  images. Lines whose target is missing from the book are marked in orange.
* **Undo my changes** restores the menu you started from; **Close** hides the
  editor.

**Save to EPUB** writes a new copy into your output folder. Only the menu changes:
the chapter files, the reading order, the images and the cover are copied over
untouched. The tool updates both the NCX (what simple readers list) and the
navigation document, so the edit shows up everywhere. Books that have neither are
reported instead of being half-edited.

Two things worth knowing:

* Renaming a menu line changes **only the menu**, not the heading in the text –
  that is usually what you want, and it is why this cannot damage the book.
* After saving, the editor points itself at the file it just wrote, so the previews
  show the result and you can keep editing from there.

Editing the table of contents is UI-only; there is no command-line flag for it.

---

## 5. Why the pandoc flags are what they are

| Flag | Why |
| --- | --- |
| `-t epub2` | Writes EPUB2 with a traditional `toc.ncx`. Simple reader firmware (Crosspoint included) handles that much more reliably than EPUB3 nav. |
| `--split-level=N` | **The important one.** Physically splits the EPUB into one internal XHTML file per heading at level N (and at every shallower level). Without it the book is one giant file and chapter jumps fail. |
| `--toc --toc-depth=N` | Builds the reader's chapter menu. `toc-depth` is kept equal to the split level so **every** menu entry points at the start of a file instead of an anchor inside one. |
| `--resource-path=<folder of the .md>` | Lets pandoc find the images marker extracted next to the markdown. |
| `--epub-cover-image` | Only added when you supply a cover. |

---

## 6. How the chapter split level is chosen

marker's heading levels vary wildly per book, so the script counts them and picks
the shallowest level that has a substantial number of headings (at least 3, and
at least 40 % of the most frequent level's count). Verified against real cases:

| Book | Headings found | Split level | Result |
| --- | --- | --- | --- |
| *Ghosts of My Life* | h1:19, h2:37, h3:13, h4:1 | **h1** | 20 chapters (matches the book's real parts/pieces) |
| *More Brilliant than the Sun* | h1:16, h2:22, h3:173, h4:89 | **h3** | 212 chapters (its sections are tiny by design) |
| *Popol Vuh* (earlier) | h1:2, h4:30 | **h4** | 30 chapters |
| *The Cheese and the Worms* (earlier) | h1:1, h2:62 | **h2** | 62 chapters |

If the result is not what you want, override it: `--split-level 2` on the command
line, or the **Chapter split level** field in the UI. Then rebuild only.

**Splitting too deep is harmless** (smaller files, slightly longer menu);
splitting too shallow is what breaks navigation on the reader. If in doubt,
go one level deeper.

The printed table of contents that many PDFs have in the front matter is handled
separately: if marker reads it as a markdown table, it is rewritten into a plain
bullet list (its page numbers are meaningless in an EPUB) – that is the
**"Turn printed tables of contents into lists"** option. It is safe: the
untouched marker output is always kept next to it as `Title.raw.md`.

---

## 7. What you get in the output folder

```
G:\...\converted\
├── Ghosts of My Life Writings on Depression, Hauntology and Lost Futures.epub
└── [Mark_Fisher]_Ghosts_of_My_Life_Writings_on_Depre(BookZZ.org)\
    ├── [Mark_Fisher]_Ghosts_of_My_Life_Writings_on_Depre(BookZZ.org).md       ← cleaned, editable
    ├── [Mark_Fisher]_Ghosts_of_My_Life_Writings_on_Depre(BookZZ.org).raw.md   ← marker's original
    └── _page_50_Picture_1.jpeg                                                ← extracted images
```

The EPUB filename comes from the title, the sub-folder from the PDF filename.
Nothing is ever deleted; the `.raw.md` is your safety net when the automatic
cleanup (empty image tags, page-anchor markup, printed-TOC tables) does
something you did not want.

The cleanup step removes:

* `![]()` – image markers that point at no file
* `<span id="page-12-0"></span>` and links to `#page-12-0` – marker's page anchors
* markdown tables that are really a printed table of contents (optional)

### Is everything inside the EPUB?

Yes. Images (including the cover) and the stylesheet are copied into the file's
`EPUB/media` and `EPUB/styles` folders and referenced from inside it, so the
`.epub` is self-contained – copy that one file to the reader and nothing else.
The log and the result line report how many images were packed in (e.g.
`5 images + cover`).

Two things to know about that count:

* **Only figures marker's layout model recognises are extracted.** Running heads,
  decorations and full-page scans of text are not images to a reader, so a scanned
  book usually ends up with very few. The text itself is what gets OCR'd.
* **The `Title/` folder next to the EPUB is not part of the book.** It exists so
  you can edit the markdown and rebuild, or check the extracted images; the EPUB
  does not depend on it.

The same applies to web images: if a chapter references an image over `http(s)://`,
pandoc would have to download it. This pipeline works offline, so only local
images end up in the book.

### Editing the markdown and rebuilding

Common manual fixes after a first run:

* Delete the printed `# CONTENTS` section – the reader builds its own menu.
* Fix a heading that marker invented out of ordinary text (a long sentence
  suddenly marked `##`) – remove the `#`s or demote it.
* Give bare numbered headings a real name: `## 1` → `## 1. Menocchio`.
* Remove running heads/footers that survived (`marker` usually catches these).

Then rebuild in seconds:

```powershell
... pdf2epub.py "book.pdf" --output-dir "G:\converted" --rebuild-only
```

or tick **"Rebuild the EPUB only"** in the UI.

---

## 8. Options

| UI option | CLI flag | Meaning |
| --- | --- | --- |
| pipeline: auto / digital / scanned | `--type auto\|digital\|scanned` | Which pipeline to use. `auto` (default) decides from the PDF: many full-page images or almost no text ⇒ scanned. |
| Fast OCR | `--fast-ocr` | Scanned PDFs only: OCR whole pages at once, ~2× faster, but pictures *inside* the scanned pages are not cropped out. |
| Turn printed tables of contents into lists | `--no-toc-tables` (to disable) | See section 6. |
| Rebuild the EPUB only | `--rebuild-only` | Reuse the markdown already in the output folder. |
| Chapter split level | `--split-level N` | Override the detected heading level. |
| Cover image | `--cover PATH` | Sets the EPUB's cover: packaged for a PDF conversion, added/replaced for an EPUB input. Big images are scaled to 1600 px and re-encoded as JPEG. |
| Title / Author (per file) | `--title`, `--author` | Written into the EPUB metadata; the title is also the EPUB filename. Defaults: the PDF's own metadata, then the filename. For an EPUB input these default to what the file already contains. |
| – | `--page-range 20-40` | Convert only some pages (good for test runs). |
| – | `--toc-depth N` | Defaults to the split level. |
| – | `--work-dir PATH` | Where scratch files go, see below. |

### Where the scratch files live

Re-runs copy, flatten and re-render whole books, so intermediates do **not** go
into your output folder but into:

```
%LOCALAPPDATA%\pdf2epub\_work\        (uploads, flattened scans, marker's raw output)
```

That is deliberate: a flattened 121-page scan is ~78 MB. Deleting that folder is
always safe. On macOS/Linux it falls back to the system temp directory.

---

## 9. Machine setup (what this was built against)

| Piece | Location / version | Needed for |
| --- | --- | --- |
| `marker_env` virtual environment | a venv with Python 3.11, torch 2.6.0+cu124, marker-pdf 2.0.0, surya-ocr 0.22.1 | everything |
| CUDA on the GPU | verified with `torch.cuda.is_available()` | OCR speed |
| `llama-server.exe` | e.g. `C:\Tools\llama.cpp\llama-server.exe` (build b10199 tested) | OCR (scanned PDFs) |
| Pandoc | 3.1.7 or newer (3.10 tested), on `PATH` | the EPUB |

Why `llama-server.exe` is needed at all: marker's OCR engine (surya) runs its
vision model behind an inference server. On an NVIDIA machine its default choice
is vLLM, which wants **Docker** – not available here. The script therefore pins
surya to the local `llama.cpp` backend and points it at `llama-server.exe`. Born-
digital PDFs do not need any of that.

### The surya grammar patch (applied automatically)

`surya-ocr` sends a regex containing `\d` to llama-server for its guided-layout
decoding. llama.cpp builds newer than ~b4800 (including b10199) reject `\d` as an
invalid escape and answer
`400 – Failed to initialize samplers: failed to parse grammar`, which marker
reports as an endless "Inference error". `pdf2epub.py` rewrites those patterns to
`[0-9]` (same meaning, universally accepted) in
`marker_env\Lib\site-packages\surya\inference\prompts.py` before every OCR run,
and logs `patched surya grammar`. It is idempotent.

**If you ever `pip install -U surya-ocr`, the patch is wiped** – that is fine, the
next run re-applies it. This is an upstream bug, not something wrong with your
setup.

### Helper processes are cleaned up after every run

surya shuts its model server down with `os.kill(pid, 0)`, which on Windows
terminates nothing – it sends `CTRL_C_EVENT` to a console process group. Two
consequences, both handled by the tool:

* The leftover `llama-server` / `surya.fast_layout.server` process keeps ~1.5 GB of
  RAM and a port until the next reboot. `pdf2epub.py` records which helper
  servers existed before a run and stops the ones that run started (and orphans
  from an interrupted run), logging `stopped N helper process(es)`. It never
  touches unrelated processes.
* The stray `Ctrl+C` used to abort the conversion (it reached the parent process).
  Child processes now get their own console window, so it cannot.

### Rebuild the environment from scratch

```powershell
python -m venv C:\Users\<you>\marker_env
C:\Users\<you>\marker_env\Scripts\Activate.ps1     # if this fails: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install --upgrade pip
pip install marker-pdf
pip install torch==2.6.0+cu124 torchvision --index-url https://download.pytorch.org/whl/cu124 --force-reinstall
```

Do **not** run `pip install -r requirements.txt` on top of a working CUDA setup:
marker-pdf's metadata asks for `torch>=2.7`, no CUDA 12.4 wheel of 2.7 exists, and
pip would silently swap your CUDA torch for the CPU-only build from PyPI (which
makes OCR crawl). pip's "dependency conflicts" warnings about torch and pillow are
expected and harmless here.

Check the GPU is visible:

```powershell
C:\Users\<you>\marker_env\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

If `llama-server.exe` is missing, grab a llama.cpp Windows CUDA build from
<https://github.com/ggml-org/llama.cpp/releases>, unpack `llama-server.exe` into
`C:\Tools\llama.cpp\` (or anywhere on your PATH, or point `LLAMA_CPP_BINARY` at
it) – that is all the tool needs. Pandoc:
`winget install --exact --id JohnMacFarlane.Pandoc`.

---

## 10. Verified results

Both test PDFs were converted end to end with this code:

| Book | Type detected | Time | Result |
| --- | --- | --- | --- |
| *Ghosts of My Life* (208 pages) | digital | **~1.5 min** | 20 chapters, 21 menu entries, 5 images, 0.85 MB |
| *More Brilliant than the Sun* (121 pages) | scanned | **~22 min** (±2 min of that is rendering the pages) | 212 chapters, 213 menu entries, 2 images, 0.38 MB |

Both EPUBs contain a `toc.ncx` plus one XHTML file per chapter, which is what the
chapter menu on the reader navigates.

OCR quality check on the scanned book – the old text layer said
"Turntobilizotion = AutoDestruction", the OCR'd version says
"Turntabilization = AutoDestruction".

The metadata rewrite was verified the same way: feeding the corrected-title EPUB
back in keeps all 212 chapter files, the images and the 213-entry chapter menu
byte-for-byte, while `dc:title`, `dc:creator`, the NCX `docTitle`, the nav title
and the title page all pick up the new values (an EPUB without a `<dc:creator>`
gets one added).

Covers were checked on both paths: a PDF conversion packages the image as
`EPUB/media/file1.jpg` with a cover page first in the spine and a guide reference;
adding a cover to an EPUB that had none lands it as `EPUB/media/cover.jpg` plus
`EPUB/text/cover.xhtml`, and replacing an existing cover leaves exactly one cover
image and one cover page behind (the old file is deleted, no duplicate manifest
entries). A self-containment audit of all test EPUBs found every image reference
resolving to a file inside the `.epub` and no unreferenced media files.

The table-of-contents editor was round-tripped as well: renaming a line and
re-nesting a flat menu into three levels survives a save and re-read (labels,
hrefs and depths identical), the NCX and nav document stay well-formed, the
`dtb:depth` follows the new nesting, and everything except those two menu files is
byte-for-byte identical to the input book. Both the cover and every chapter page
can be previewed straight out of the file, which is how the UI shows them.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker binary not found` | surya fell back to its vLLM backend | Use `pdf2epub.py`/the UI (they pin the llama.cpp backend), or set `$env:SURYA_INFERENCE_BACKEND="llamacpp"` yourself. |
| `llama-server binary not found` | OCR needs the binary | Put `llama-server.exe` in `C:\Tools\llama.cpp\` or on your PATH, or set `LLAMA_CPP_BINARY`. |
| Endless `Inference error: Error code: 400 … failed to parse grammar` | The surya `\d` grammar bug | Update the tool (the patch is applied automatically); if you run marker by hand, patch `surya\inference\prompts.py` yourself. |
| `marker_single is not recognized` | Wrong Python / environment not active | Always go through `start_ui.bat` or the full path to the virtualenv's `python.exe`. |
| A traceback ending in `KeyboardInterrupt` after a successful run | surya killing its llama-server at exit | Harmless. The tool filters it out; you will not see it in the UI. |
| A ~1.5 GB `python.exe` (`surya.fast_layout.server`) stays in the task manager after a run | surya's shutdown on Windows does not really stop its model server | Nothing to do – the tool stops these itself after every run and logs `stopped N helper process(es)`. If you interrupted a run with Ctrl+C, find and end leftovers with `tasklist | findstr python` and `taskkill /PID <pid> /F`. |
| The EPUB text is gibberish for a scanned book | The PDF was classified as `digital` and its old, wrong text layer was reused | Set the pipeline to `scanned` for that file (UI dropdown) or `--type scanned`. |
| The chapter menu does nothing / one giant chapter | The split level landed on a level with too few headings | Set `--split-level`/the UI field to the level the chapter titles actually use. |
| Chapter titles in the menu are just numbers | The source marks chapters as `## 1`, `## 2`, … | Rename them in the `.md` (`## 1. Menocchio`) and rebuild, or rename the menu lines in the TOC editor. |
| Missing pictures | marker only extracts what its layout model sees as a figure | Manual: keep the page scan. For scanned books, the layout-aware OCR (default) extracts more than Fast OCR. |
| Conversion takes 20+ minutes | That is normal for a scanned book (full OCR at ~10–20 s/page, depending on page size and how many parallel slots fit in the VRAM) | Use it as a coffee break, queue several books in one go, or tick **Fast OCR** to roughly halve the time. |
| The title/author on the reader is wrong | The PDF's metadata was missing, or wrong | Drop the EPUB into the UI, fix Title/Author, Convert (see section 4). It only rewrites the metadata. |
| The cover changed in the file but the reader still shows the old one | Readers and Calibre cache covers by book | Check the thumbnail in the UI result first (that is the cover that is really in the file). If it is right, delete the book on the device and copy the new file over, or refresh the library entry. |
| `edit table of contents` is missing on a card | The EPUB has no NCX and no navigation document | Nothing to edit; the card only offers it when the book has a menu. |
| A menu line is marked orange in the editor | Its target page is not in the book | That entry came from the source EPUB; leave it, or rename it if it is only a label. |
| `pandoc: command not found` | Pandoc missing | `winget install --exact --id JohnMacFarlane.Pandoc`, then open a new terminal. |
| UI will not start: "missing marker, pypdfium2, PIL" | Started with the system Python instead of the environment | Use `start_ui.bat`, or set `PDF2EPUB_PYTHON`. |

---

## 12. Known limits (things worth knowing)

* **Scans are read through the layout model.** A page that is one big image gives
  the OCR a lot to interpret; expect a good but not perfect transcription of the
  *text*, and few extracted figures. Fine OCR errors are normal.
* **The tool never deletes anything** (except marker's own leftover helper
  processes, and the previous cover file when a cover is replaced). If the
  automatic cleanup gets something wrong, compare with `Title.raw.md` and rebuild.
* **PDFs whose text layer is a bad OCR layer** look "digital" to the detector
  (there *is* text). If the output is nonsense, force `scanned` – that path
  throws the old layer away and re-OCRs, which is exactly what those files need.
* The UI is a local web page: it binds to `127.0.0.1` only, and the files you drop
  are copied into `%LOCALAPPDATA%\pdf2epub\_work\uploads` because a browser
  cannot hand over the original path. That is also why fixing an EPUB's metadata
  produces a corrected copy instead of editing your file in place.
* A title change renames the file on purpose (that is usually the point), so an
  older file with the previous name stays where it was - delete it yourself once
  you are happy.
* Editing the table of contents changes the menu only. If a chapter's *heading in
  the text* is wrong, fix it in the markdown and rebuild instead (section 7).

  ---

  ## 13. Licensing

  The code here is **MIT** ([LICENSE](LICENSE)). Everything it depends on is
  permissively licensed too (marker-pdf and surya-ocr are Apache-2.0, PyTorch is
  BSD-3-Clause, llama.cpp and Pillow are MIT-ish, pypdfium2/PDFium is
  Apache-2.0/BSD-3-Clause), so an MIT project can depend on them freely.

  Two things worth knowing:

  * **Pandoc is GPL-2.0-or-later.** It is only ever *executed* as a separate program
    (never imported or linked), which is the accepted way for permissively licensed
    tools to use GPL tools — no copyleft reaches this code. The same goes for
    `llama-server`. If you ever *vendor* code from any of these projects into this
    repository, that changes: you would have to keep their licence texts and
    notices, and GPL code could not be copied in at all.
  * **marker's model weights are not Apache-2.0.** The OCR path downloads them from
    Hugging Face under datalab's modified **OpenRAIL-M** licence, which allows
    personal, research and commercial use but restricts organisations above a US$5M
    revenue/funding threshold, requires attribution, and passes its use restrictions
    on to whoever you distribute to. The digital/text-layer path does not touch those
    weights.

  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) has the full component list, the
  licence summary and what to do if you redistribute this tool. Nothing there
  changes the MIT licence of this repository's code.

  Finally, the obvious one: converting a book does not give you rights to it. Use
  this on documents you are allowed to convert.
