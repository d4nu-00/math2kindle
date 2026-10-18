# math2kindle

Turn a maths paper — an arXiv id/URL, a LaTeX source tree or a PDF — into an
EPUB that reads well on an e-ink Kindle, with **every formula pre-rendered as a
crisp grayscale image**, and optionally e-mail it to your Kindle.

```sh
math2kindle 2101.00001                       # arXiv id (LaTeX source when available)
math2kindle https://arxiv.org/abs/math/0601001v2 --send
math2kindle paper.pdf --out ~/Books          # PDF -> marker (ML) backend (arXiv PDFs: LaTeX source first)
math2kindle source.tar.gz --azw3             # also write an AZW3 with calibre
python -m math2kindle paper.tex --keep-work -v
```

## The converter

### Pipeline

1. **Input.** arXiv ids (`2101.00001`, `2101.00001v2`, `math/0601001`,
   `arXiv:…`) and any `arxiv.org/abs|pdf|e-print/…` URL: the source is fetched
   from `https://arxiv.org/e-print/<id>` (gzipped tarball, gzipped single
   `.tex`, or a PDF when no source exists) and title/authors/abstract from the
   `export.arxiv.org` Atom API. Downloads are unpacked into a fresh working
   directory with path-traversal and symlink protection; nothing in them is
   executed. A **PDF downloaded from arXiv** is recognised by the identifier
   stamp arXiv prints in the margin of page 1 (`arXiv:1406.7309v1 [math.MG]
   27 Jun 2014`, or old-style `math/0601001`): math2kindle then fetches that
   paper's LaTeX source and metadata and uses the LaTeX backend, and converts
   the PDF itself only if arXiv has no source (PDF-only submission) or the
   network fails. `--no-arxiv-source` skips this (no network). The stamp is
   never used as the book title.
2. **LaTeX backend** (preferred, exact maths, no OCR): finds the main file
   (`\documentclass` + `\begin{document}`, preferring the one matching the
   `.bbl`), flattens `\input`/`\include`, harvests `\newcommand`, `\def`,
   `\DeclareMathOperator`, `\DeclarePairedDelimiter`, `\newtheorem`,
   `\theoremstyle`, `\numberwithin`… and then makes one pass over the body
   that pulls every formula out (pandoc never sees the maths), numbers
   sections/theorems/equations/figures/tables like LaTeX, resolves `\ref`,
   `\eqref`, `\cref`, `\autoref`, `\cite` (from the `.bbl`), expands the
   author's text macros, converts PDF/EPS figures to grayscale PNG and compiles
   TikZ pictures. Pandoc turns the rest into HTML. Unknown constructs are
   passed through or dropped with a warning; the conversion never aborts on
   odd LaTeX.
3. **marker backend** (PDF only): [marker-pdf](https://github.com/datalab-to/marker)
   produces Markdown with `$…$`/`$$…$$` maths and figure images, which pandoc
   turns into HTML. The device is auto-detected (CUDA → MPS → CPU).
   marker-pdf ≥ 2 runs its vision model in llama.cpp's `llama-server`
   (vLLM on NVIDIA), so on a Mac it needs `brew install llama.cpp` (or
   `LLAMA_CPP_BINARY=/path/to/llama-server`); math2kindle checks for it before
   starting marker. marker also starts small model servers that it keeps
   running after it exits; math2kindle stops the ones started by its own run
   (set `M2K_KEEP_MARKER_SERVERS=1` to keep them for faster repeat runs).
   **OCR is forced by default** (`force_ocr`): marker ignores the PDF's text
   layer and OCRs every page, so inline maths comes out as `$…$` LaTeX.
   Without it marker reuses the text layer and only converts blocks it
   classifies as display equations; on publisher PDFs with custom maths font
   encodings that text is junk (`A!` for A′, curly quotes for Greek letters).
   `--no-force-ocr` is faster but only safe for PDFs with a clean text layer.
   Before marker runs, math2kindle samples a few pages with PyMuPDF and logs
   whether the text layer looks garbled (private-use glyphs, U+FFFD, `!` and
   curly quotes glued to letters, modifier-letter "superscripts", maths fonts
   without a ToUnicode map); with `--no-force-ocr` it warns if so. After
   conversion it warns loudly if the Markdown has few formulas but that kind
   of junk in its prose. The log line `backend: …` says which route ran
   (`marker 2.0.0 (force_ocr …)`, `marker … (no force_ocr …)`, `pdftext
   fallback`, `latex`). The hand-run equivalent of the default is
   `marker_single paper.pdf --output_format markdown --force_ocr --ocr_inline_math True`.
   If marker is not installed or cannot run, the conversion **fails** with the
   cause and the fix instead of shipping garbled maths. `--allow-text-fallback`
   writes a plain-text PyMuPDF EPUB anyway (formulas garbled; title, chapters
   and table of contents from the PDF metadata/outline or numbered headings);
   such an EPUB is never e-mailed.
4. **Maths rendering.** All distinct formulas go to *one* Node.js process
   running **MathJax 4** (TeX → standalone SVG) and **resvg** (SVG → PNG):
   * displays are kept on one line when they fit within the line width
     (`--width-em`, default **16 em**) plus 25%. Wider ones are broken by
     math2kindle itself at measured break points: explicit `\quad`/`\qquad`
     gaps first, then top-level relations, then top-level + and − (a line may
     run to about 1.4× the width rather than split a bracket group); only if
     that fails, inside brackets, where the continuation is indented past the
     opening bracket and always right of the `=` column. Never inside braces,
     short bracket groups or `\left…\right` pairs, and never between a big
     operator and its operand. Explicit `\qquad` spacing is preserved where
     no break is made. Rows of `align`, `gather`, `multline` and `split` are
     laid out individually; aligned rows that fit keep their alignment, and
     continuation rows of a broken `multline` share one indent;
   * a display made of several lines is one group whose sizes are
     percentages of its widest line, so on a narrow page (large fonts) every
     line is scaled by the same factor;
   * equation numbers are **real text**, attached to the last line of the
     display: at the right margin when there is room, otherwise wrapped onto
     the line just below it, flush right;
   * second-level scripts (fractions in exponents) never drop below 66% of
     the text size, and fractions in table cells use display size;
   * long inline formulas are split at MathJax's break points into several
     images so the reader can wrap them;
   * images are grayscale, opaque white background (visible in Kindle dark
     mode and in the apps), ~48 px per em (≈300 ppi at normal sizes), glyphs
     slightly emboldened for e-ink (`--math-weight`), sized in **em** with a
     baseline-correct `vertical-align`, so they scale with the reader's font
     size. `alt` holds the TeX.
   * formulas MathJax rejects are compiled with real LaTeX (`pdflatex` +
     `preview`, the paper's own packages and macros, shell-escape off); if that
     fails too, the TeX source is shown in monospace.
5. **EPUB 3**: one chapter per top-level section, nav + NCX table of contents,
   theorem/lemma/proof styling (bold heads, italic plain-style bodies, proofs
   end with ∎), Kindle pop-up footnotes, linked citations and equation
   references, title page with abstract. Validated with `epubcheck`.

### Installation

Requirements: Python ≥ 3.11, Node.js ≥ 18 (with npm), pandoc (bundled through
`pypandoc_binary`), and for the LaTeX fallback/TikZ a TeX distribution
(optional).

**macOS**

```sh
brew install node             # Node.js + npm
brew install --cask mactex-no-gui   # optional: LaTeX fallback + TikZ figures
python3 -m venv ~/.venvs/m2k && source ~/.venvs/m2k/bin/activate
pip install -e ".[send]"
pip install marker-pdf        # optional ML backend; uses Apple-silicon MPS automatically
brew install llama.cpp        # needed by marker-pdf >= 2 (llama-server runs its vision model)
```

**Linux (Debian/Ubuntu)**

```sh
sudo apt install nodejs npm python3-venv ghostscript
sudo apt install texlive-latex-extra texlive-pictures   # optional
python3 -m venv ~/.venvs/m2k && . ~/.venvs/m2k/bin/activate
pip install -e ".[send]"
# CPU-only torch keeps marker's install small; use the default wheel for CUDA:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install marker-pdf
# marker-pdf >= 2 also needs llama-server on PATH (CPU/AMD): a llama.cpp release
# from https://github.com/ggml-org/llama.cpp/releases, or `brew install llama.cpp`
```

On the first maths render math2kindle runs `npm ci` once to install MathJax 4
and resvg into its cache (`~/Library/Caches/math2kindle` on macOS,
`~/.cache/math2kindle` on Linux; override with `MATH2KINDLE_CACHE`).
**marker downloads several GB of models (and a font) on its first run**; check
it once with

```sh
python -m math2kindle.backends.marker_backend tests/fixtures/paper.pdf
M2K_RUN_MARKER=1 pytest tests/test_marker_real.py -s
```

Calibre's `ebook-convert` is only needed for `--azw3`. Send-to-Kindle e-mail
accepts EPUB (not AZW3), so EPUB is the default.

### Useful options

| option | effect |
| --- | --- |
| `--backend auto\|latex\|marker` | force a backend (auto: LaTeX source if available) |
| `--no-force-ocr` | marker: reuse the PDF's text layer (faster; garbles inline maths in many publisher PDFs) |
| `--width-em 20` | wider display lines (fewer breaks) if you always read at small font sizes |
| `--math-scale 1.0` | maths size relative to body text (default 1.06) |
| `--math-weight 0` | turn off stroke emboldening |
| `--justify` | justified paragraphs (default ragged-right: wide formulas justify badly) |
| `--send [--dry-run] [--config FILE]` | e-mail the EPUB (setup: [docs/SEND_TO_KINDLE.md](docs/SEND_TO_KINDLE.md)) |
| `--keep-work` / `--work-dir DIR` | keep intermediate files for debugging |
| `--no-arxiv-source` | for a PDF with an arXiv stamp, convert the PDF instead of fetching its LaTeX source |
| `--allow-text-fallback` | if marker cannot run, write a text-only EPUB (garbled maths; never e-mailed) |

### Tests

```sh
pip install -e ".[test]"
pytest -q                      # needs node; epubcheck/pdflatex tests skip if absent
python tests/fixtures/build_fixtures.py   # rebuild paper.pdf / paper_src.tar.gz
```

`tests/fixtures/paper_src/` is a 6-page analytic-number-theory paper (Perron's
formula, explicit formula, theorem environments, `align`/`multline`/`gather`,
`cases`, matrices, TikZ-cd, a PDF figure, footnotes, `.bbl`); the arXiv network
layer is tested with a fake fetcher.

### Known limitations

* Page-number references (`\pageref`), `subequations` letters, `\cref` ranges
  and exotic bibliography styles are approximated.
* Very wide unbreakable displays (big matrices, long left-hand sides in
  `align`) are scaled down to the page width; tap the image on the Kindle to
  zoom. Lines may overshoot the 16 em width by up to 25% rather than break
  inside a bracket pair; at normal font sizes that still fits the page.
* "Words" like `1-form` / `2-dimensional` are kept together with a
  `white-space: nowrap` span rather than a U+2011 hyphen (not every Kindle
  font has that glyph).
* TikZ/xy-pic need a local LaTeX installation; otherwise a placeholder is shown.
* marker output quality (and its equation numbering) depends on the PDF.
