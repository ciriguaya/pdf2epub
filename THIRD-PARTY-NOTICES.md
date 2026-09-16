# Third-party components

The code in this repository is MIT licensed (see [LICENSE](LICENSE)). It does not
bundle or copy anything from the projects below — it installs and calls them — but
their terms still apply to their own parts, and one of them has to be passed on to
users. This file is here so that neither you nor anyone using this tool is
surprised by that.

## Libraries (installed with pip, permissively licensed)

| Component | License | How this project uses it |
| --- | --- | --- |
| [marker-pdf](https://github.com/datalab-to/marker) | Apache-2.0 | Python library; drives PDF → markdown |
| [surya-ocr](https://github.com/datalab-to/surya) | Apache-2.0 | pulled in by marker |
| [transformers](https://github.com/huggingface/transformers) | Apache-2.0 | pulled in by marker |
| [pdftext](https://github.com/datalab-to/pdftext) | Apache-2.0 | pulled in by marker |
| [PyTorch](https://github.com/pytorch/pytorch) | BSD-3-Clause | runs the models |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (and PDFium) | Apache-2.0 / BSD-3-Clause | renders pages and inspects PDFs |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | builds the flattened scan and the cover |

Apache-2.0, BSD-3-Clause and MIT-CMU are all permissive and compatible with an MIT
project depending on them. Nothing needs to be done unless this project ever
*vendors* their source, in which case their licence texts and notices have to be
kept alongside the copied files.

## Programs run as separate processes

| Program | License | Why that is fine here |
| --- | --- | --- |
| [Pandoc](https://pandoc.org) | GPL-2.0-or-later | It is only ever **executed** as a separate program (`pandoc …`), never linked or imported, so its copyleft does not reach this project's code. The same applies to anything else a user runs. |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) | MIT | separate program started as a subprocess |

## Model weights — the one part that is not permissive

marker's **code** is Apache-2.0, but its **model weights** are not. The first time
the OCR (scanned-PDF) path runs, marker downloads them from Hugging Face:

* [`datalab-to/surya-ocr-2`](https://huggingface.co/datalab-to/surya-ocr-2) — OCR / layout VLM
* [`datalab-to/surya_layout2`](https://huggingface.co/datalab-to/surya_layout2) — layout detection
* `datalab-to/surya-ocr-2-gguf` — the GGUF copies used by `llama-server`

They are licensed under the **AI Pubs OpenRAIL-M License (modified)**. In plain
terms, that licence:

* allows personal, research and commercial use, but **not** if you (or the
  organisation you work for) had more than **US$5,000,000** in gross revenue or
  raised more than that in funding in the prior year, unless the use is personal or
  research — above that, datalab sells a commercial licence;
* forbids competing with datalab's own products and services;
* requires **attribution** to datalab and a copy of the licence for anyone you pass
  the model (or output derived from it) on to;
* **passes its use restrictions down**: clause 4(a) says the use restrictions must
  appear as an enforceable provision in any agreement governing distribution, and
  users must be told about them;
* includes a **share-alike clause** (8) that the licence text applies to the model,
  its derivatives *and its output*.

Because the tool fetches these models at runtime, the person running it is the one
who accepts that licence — but this project is what leads them there, so:

* if you use the **scanned/OCR path**, read the model card and licence above before
  using it commercially;
* if you **redistribute this tool** (in any form), keep this notice with it, or
  point users at the licence, so the flow-down requirement is satisfied;
* the **digital/text-layer path does not use those model weights** at all.

Nothing here changes the MIT licence of the code in this repository, and none of
the model licence restrictions apply to the repository itself — they apply to what
the models are used for and to their output.

## The books themselves

Converting a book does not grant you rights to it. Use this tool on documents you
are allowed to convert, and no book files are distributed in this repository (see
`.gitignore`).
