"""PDF -> ConvertedDoc via marker-pdf (ML layout + OCR + LaTeX maths).

Heavy imports (torch, marker) happen only inside :func:`run_marker`, so the
LaTeX path never loads them.  Downstream handling of marker's Markdown is in
:func:`markdown_to_doc`, which is unit-tested with a synthetic fixture.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..document import ConvertedDoc
from ..figures import convert_graphic
from ..htmltree import E, parse_fragment, remove_keep_tail
from ..inputs import is_arxiv_stamp
from ..pandoc import markdown_to_html
from ..send import TEXT_FALLBACK_CLASS
from ..util import Math2KindleError, log, which


def detect_device() -> str:
    """cuda > mps > cpu (honours an explicit TORCH_DEVICE)."""
    if os.environ.get("TORCH_DEVICE"):
        return os.environ["TORCH_DEVICE"]
    try:
        import torch  # noqa: WPS433 (lazy on purpose)

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover - torch missing/broken
        pass
    return "cpu"


def marker_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("marker") is not None or which("marker_single") is not None


# ---------------------------------------------------------------------------- llama.cpp preflight

LLAMA_CPP_FIX = ("  Fix:  brew install llama.cpp        (or re-run: bash install_mac.sh --with-marker)\n"
                 "  or set LLAMA_CPP_BINARY=/path/to/llama-server")
_LLAMA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.homebrew/bin"))


def _surya_dir() -> Path | None:
    import importlib.util

    try:
        spec = importlib.util.find_spec("surya")       # locates the package without importing it
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    locs = list(spec.submodule_search_locations or [])
    return Path(locs[0]) if locs else (Path(spec.origin).parent if spec.origin else None)


def llama_server_problem(device: str) -> str | None:
    """None if marker's vision model can start, else an error naming the fix.

    marker-pdf >= 2 (surya >= 0.20) runs its VLM in llama.cpp's ``llama-server``
    on Apple Silicon / CPU (vLLM on NVIDIA). Without the binary marker fails
    only after loading its other models, so check up front."""
    sd = _surya_dir()
    if sd is None or not (sd / "inference" / "backends" / "llamacpp.py").exists():
        return None                                    # older marker: no llama.cpp involved
    if os.environ.get("SURYA_INFERENCE_URL"):
        return None                                    # user runs their own inference server
    backend = (os.environ.get("SURYA_INFERENCE_BACKEND") or "").lower()
    if backend == "vllm" or (not backend and device == "cuda"):
        return None
    configured = os.environ.get("LLAMA_CPP_BINARY")
    if configured:
        if (os.path.isfile(configured) and os.access(configured, os.X_OK)) or which(configured):
            return None
        return (f"LLAMA_CPP_BINARY={configured} is not an executable llama-server; marker-pdf needs it.\n"
                + LLAMA_CPP_FIX)
    if which("llama-server"):
        return None
    for d in _LLAMA_DIRS:                              # Homebrew not on PATH (launchd, Shortcuts)
        cand = os.path.join(d, "llama-server")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            os.environ["LLAMA_CPP_BINARY"] = cand
            return None
    return ("marker-pdf needs llama.cpp's `llama-server` binary (it runs marker's vision model) "
            "and it was not found.\n" + LLAMA_CPP_FIX)


# ---------------------------------------------------------------------------- surya helper servers
#
# surya (marker's model library) starts its layout / OCR-error / detection models
# as local HTTP servers.  Those batch-service servers are always started with
# keep_alive=True (hard-coded in surya.common.batch_service.client; there is no
# setting to turn it off), so they outlive a one-shot conversion.  surya records
# each in ~/.cache/datalab/surya/<name>_server.json (port, pid, cleanup_id); we
# stop the ones that were started during our run.  llama-server itself is
# stopped by surya's own atexit hook unless SURYA_INFERENCE_KEEP_ALIVE is set.

SURYA_CACHE = Path(os.path.expanduser("~/.cache/datalab/surya"))
_SURYA_OWN_CLEANUP = ("llamacpp", "vllm")


def surya_servers() -> dict[str, dict]:
    """{backend name: sentinel data} for the servers surya has recorded."""
    import json

    out: dict[str, dict] = {}
    try:
        files = sorted(SURYA_CACHE.glob("*_server.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out[f.name[: -len("_server.json")]] = data
    return out


def _is_surya_process(pid: int) -> bool:
    try:
        import psutil  # marker depends on it

        cmd = " ".join(psutil.Process(pid).cmdline())
    except ImportError:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception:
            return False
    except Exception:            # gone, zombie or not ours to inspect
        return False
    return "surya" in cmd


def _terminate(pid: int) -> None:
    import signal
    import time

    try:
        import psutil

        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=10)
        except psutil.TimeoutExpired:
            p.kill()
        return
    except ImportError:
        pass
    except Exception:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:       # our child: reap it
                    return
            except ChildProcessError:
                os.kill(pid, 0)                          # not our child: raises once it is gone
            time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_new_surya_servers(before: dict[str, dict]) -> list[str]:
    """Stop the keep-alive servers that appeared since ``before`` (a
    :func:`surya_servers` snapshot).  Servers that were already running belong
    to someone else and are left alone; so is everything when
    M2K_KEEP_MARKER_SERVERS=1 (faster repeated runs)."""
    if os.environ.get("M2K_KEEP_MARKER_SERVERS") == "1":
        return []
    stopped = []
    for name, data in surya_servers().items():
        pid = data.get("pid")
        if name in _SURYA_OWN_CLEANUP or data.get("cleanup_kind", "process") != "process":
            continue
        if not isinstance(pid, int) or pid <= 0 or (before.get(name) or {}).get("pid") == pid:
            continue
        if not _is_surya_process(pid):
            continue
        _terminate(pid)
        try:
            (SURYA_CACHE / f"{name}_server.json").unlink()
        except OSError:
            pass
        stopped.append(name)
    if stopped:
        log.info("Stopped marker's helper servers: %s (M2K_KEEP_MARKER_SERVERS=1 keeps them)", ", ".join(stopped))
    return stopped


def marker_version() -> str | None:
    """Installed marker-pdf version (None if not installed)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("marker-pdf")
        except PackageNotFoundError:
            return None
    except Exception:  # pragma: no cover
        return None


def parse_page_range(page_range: str | list[int] | None) -> list[int] | None:
    """'0,3-5' -> [0, 3, 4, 5] (marker's config wants a list; its CLI parses the string)."""
    if page_range is None or isinstance(page_range, list):
        return page_range
    pages: list[int] = []
    for part in str(page_range).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages += list(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def marker_config(*, force_ocr: bool = True, page_range: str | list[int] | None = None) -> dict:
    """Config dict for marker's PdfConverter (also written as --config_json for the CLI route).

    marker hands one flat dict to every builder/processor, and each picks the
    keys that match its attributes; keys a version does not know are ignored.
    So one dict serves both release lines:

    * ``force_ocr``         (1.x and 2.x, PdfProvider/LayoutBuilder): ignore the
      PDF's text layer and OCR every page.  The OCR model writes inline maths
      as <math> -> ``$...$``.  This is what fixes publisher PDFs whose maths
      fonts decode to junk ("A!" for A', curly quotes for Greek).
    * ``ocr_inline_math``   (2.x EquationProcessor): also OCR text blocks that
      look like they contain inline maths.  Its default (None) means "balanced
      mode only", and marker 2 picks *fast* mode on CPU/MPS (every Mac), where
      only display-equation blocks are OCR'd.  Matters with --no-force-ocr.
    * ``disable_ocr_math``  (1.x OcrBuilder/TableProcessor): keep surya's maths
      mode on (False is the default; explicit so nothing upstream flips it).
    * ``use_llm`` False: never call a paid LLM API (``redo_inline_math``
      needs it, so it is not used).
    """
    config: dict = {
        "output_format": "markdown",
        "force_ocr": bool(force_ocr),
        "ocr_inline_math": True,
        "disable_ocr_math": False,
        "use_llm": False,
    }
    pages = parse_page_range(page_range)
    if pages:
        config["page_range"] = pages
    return config


def marker_cli_args(*, force_ocr: bool = True, page_range: str | None = None) -> list[str]:
    """``marker_single`` flags equivalent to :func:`marker_config` on marker 2.x."""
    args = ["--output_format", "markdown"]
    if force_ocr:
        args.append("--force_ocr")
    args += ["--ocr_inline_math", "True"]
    if page_range:
        args += ["--page_range", str(page_range)]
    return args


def run_marker(pdf: Path, out_dir: Path, *, device: str | None = None, force_ocr: bool = True,
               page_range: str | None = None) -> tuple[str, dict[str, Path], dict]:
    """Run marker on a PDF.  Returns (markdown, {image name: file}, metadata).

    Uses the Python API (marker >= 1.0: PdfConverter + create_model_dict); if
    that import fails, falls back to the ``marker_single`` CLI.  First use
    downloads several GB of models from Hugging Face.  ``force_ocr`` (the
    default) OCRs every page instead of trusting the PDF's text layer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dev = device or detect_device()
    problem = llama_server_problem(dev)
    if problem:
        raise Math2KindleError(problem)
    os.environ.setdefault("TORCH_DEVICE", dev)
    if dev == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    config = marker_config(force_ocr=force_ocr, page_range=page_range)
    log.info("Running marker-pdf %s on %s (device=%s, force_ocr=%s); first run downloads its models...",
             marker_version() or "?", pdf.name, dev, force_ocr)
    log.debug("marker config: %s", config)
    try:
        from marker.converters.pdf import PdfConverter  # type: ignore
        from marker.models import create_model_dict  # type: ignore
        from marker.output import text_from_rendered  # type: ignore
    except ImportError as e:
        if which("marker_single"):
            before = surya_servers()
            try:
                return _run_marker_cli(pdf, out_dir, force_ocr=force_ocr, page_range=page_range)
            finally:
                stop_new_surya_servers(before)
        raise Math2KindleError(f"marker-pdf is not installed ({e}). `pip install marker-pdf`") from e

    before = surya_servers()
    models = None
    try:
        models = create_model_dict()
        converter = PdfConverter(artifact_dict=models, config=config)
        rendered = converter(str(pdf))
        text, _ext, images = text_from_rendered(rendered)
    except Math2KindleError:
        raise
    except Exception as e:  # network/model/torch problems -> one clear message
        hint = (f"\n{LLAMA_CPP_FIX}" if "llama-server" in str(e) else
                "\nOn first use marker downloads its models and fonts (several GB) from huggingface.co / "
                "models.datalab.to, so internet access is required once. Try again, or set TORCH_DEVICE=cpu "
                "if the GPU/MPS backend misbehaves.")
        raise Math2KindleError(f"marker failed on {pdf.name}: {type(e).__name__}: {str(e)[:400]}{hint}") from e
    finally:
        _shutdown_models(models)
        stop_new_surya_servers(before)
    files: dict[str, Path] = {}
    for name, img in (images or {}).items():
        target = out_dir / Path(name).name
        try:
            img.save(target)
            files[name] = target
        except Exception as ex:  # pragma: no cover
            log.warning("could not save marker image %s: %s", name, ex)
    meta = {}
    try:
        meta = dict(getattr(rendered, "metadata", {}) or {})
    except Exception:
        meta = {}
    (out_dir / "marker.md").write_text(text, encoding="utf-8")
    return text, files, meta


def _shutdown_models(models) -> None:
    """marker >= 2: stop the inference manager this process started (no-op otherwise)."""
    if not models:
        return
    try:
        from marker.models import shutdown_models  # type: ignore
    except ImportError:
        return
    try:
        shutdown_models(models)
    except Exception as e:  # pragma: no cover - best effort
        log.debug("marker shutdown_models failed: %s", e)


def _run_marker_cli(pdf: Path, out_dir: Path, *, force_ocr: bool, page_range: str | None):
    import json

    # Pass the whole config as JSON: it carries keys that exist in only one
    # marker release line (a CLI flag unknown to the installed version would
    # be a hard error, an unknown JSON key is ignored).
    cfg = marker_config(force_ocr=force_ocr, page_range=page_range)
    cfg.pop("output_format", None)
    cfg_file = out_dir / "m2k_marker_config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    cmd = [which("marker_single"), str(pdf), "--output_dir", str(out_dir), "--output_format", "markdown",
           "--config_json", str(cfg_file)]
    if force_ocr:
        cmd.append("--force_ocr")
    log.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    if proc.returncode != 0:
        raise Math2KindleError("marker_single failed")
    mds = sorted(p for p in out_dir.rglob("*.md") if p.name != "marker.md")
    if not mds:
        raise Math2KindleError("marker produced no markdown")
    md = mds[0]
    files = {p.name: p for p in md.parent.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")}
    return md.read_text("utf-8"), files, {}


# ---------------------------------------------------------------------------- markdown -> doc

def _tighten_inline_dollars(md: str) -> str:
    out = []
    for line in md.split("\n"):
        parts = re.split(r"(\$\$|\\\$|\$)", line)
        buf = []
        inside = False
        cur: list[str] = []
        for tok in parts:
            if tok == "$":
                if inside:
                    buf.append("$" + "".join(cur).strip() + "$")
                    cur = []
                inside = not inside
            elif inside:
                cur.append(tok)
            else:
                buf.append(tok)
        if inside:                       # unpaired: leave the rest untouched
            buf.append("$" + "".join(cur))
        out.append("".join(buf))
    return "\n".join(out)


def normalize_marker_markdown(md: str) -> str:
    """Fix marker-isms that confuse pandoc's maths parser."""
    # $ x $ -> $x$ (pandoc requires no space inside inline delimiters).  Dollars
    # must be paired left to right, otherwise "$a$ text $b$" would pair the
    # closing and opening dollars around " text ".
    md = _tighten_inline_dollars(md)
    # display maths: make sure $$ blocks stand on their own lines
    md = re.sub(r"\$\$\s*\n?(.*?)\n?\s*\$\$", lambda m: "\n\n$$\n" + m.group(1).strip() + "\n$$\n\n", md, flags=re.S)
    # \tag{...} lines like "(1.2)" after an equation are common in marker output; leave them.
    # marker's <math> tags (if HTML snuck in)
    md = re.sub(r'<math display="block">(.*?)</math>', lambda m: "\n\n$$\n" + m.group(1) + "\n$$\n\n", md, flags=re.S)
    md = re.sub(r"<math>(.*?)</math>", lambda m: "$" + m.group(1).strip() + "$", md, flags=re.S)
    return md


def markdown_to_doc(md: str, images: dict[str, Path], work: Path, *, title: str | None = None,
                    authors: list[str] | None = None, backend: str = "marker") -> ConvertedDoc:
    md = normalize_marker_markdown(md)
    html = markdown_to_html(md)
    root = parse_fragment(html)
    fig_dir = work / "figures"
    used: dict[str, Path] = {}
    n = 0
    for img in list(root.iter("img")):
        src = img.get("src") or ""
        f = images.get(src) or images.get(Path(src).name)
        if f is None or not Path(f).exists():
            log.warning("marker image missing: %s", src)
            remove_keep_tail(img)
            continue
        n += 1
        res = convert_graphic(Path(f), fig_dir, f"mfig{n:03d}")
        if res is None:
            continue
        out, _ = res
        img.set("src", str(out))
        used[str(out)] = out
        img.set("class", "figure-img")
    # arXiv's margin stamp ("arXiv:1406.7309v1 [math.MG] 27 Jun 2014") is not
    # content and must never become the title.
    for el in list(root.iter("h1", "h2", "h3", "h4", "h5", "h6", "p")):
        if is_arxiv_stamp(el.text_content()):
            remove_keep_tail(el)
    if is_arxiv_stamp(title):
        title = None
    # Title: the first heading, if it looks like the paper title, becomes the
    # title block (its maths is rendered like everywhere else).
    first_h = next(root.iter("h1", "h2"), None)
    doc_title = title
    if first_h is not None:
        t = _plain_heading(first_h)
        if not doc_title:
            doc_title = t
        if doc_title and t and t.lower()[:40] == doc_title.lower()[:40]:
            block = E("div", {"class": "m2k-title-source"})
            p = E("p")
            p.text = first_h.text
            for c in list(first_h):
                p.append(c)
            block.append(p)
            first_h.addprevious(block)
            first_h.getparent().remove(first_h)
    # marker page anchors: <span id="page-3-0"></span> - fine as anchors
    return ConvertedDoc(title=doc_title or "Untitled", authors=list(authors or []), body=root, images=used,
                        backend=backend)


def _plain_heading(h) -> str:
    """Heading text with pandoc maths spans turned into readable text."""
    from ..util import tex_to_plain

    parts = [h.text or ""]
    for c in h:
        if "math" in (c.get("class") or ""):
            t = (c.text_content() or "").strip()
            t = re.sub(r"^\\[(\[]|\\[)\]]$", "", t)
            parts.append(tex_to_plain("$" + t + "$"))
        else:
            parts.append(c.text_content() or "")
        parts.append(c.tail or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class MarkerBackend:
    name = "marker"

    def __init__(self, device: str | None = None, force_ocr: bool = True):
        self.device = device
        self.force_ocr = force_ocr

    def convert(self, pdf: Path, work: Path, *, title: str | None = None, authors=None) -> ConvertedDoc:
        layer = precheck_text_layer(pdf, force_ocr=self.force_ocr)
        md, images, meta = run_marker(pdf, work / "marker", device=self.device, force_ocr=self.force_ocr)
        if not title:
            title = _pdf_title(pdf)
        doc = markdown_to_doc(md, images, work, title=title, authors=authors)
        doc.backend_note = describe_marker_run(self.force_ocr)
        doc.warnings += postcheck_marker_output(md, layer=layer, force_ocr=self.force_ocr)
        return doc


def describe_marker_run(force_ocr: bool) -> str:
    v = marker_version()
    vs = f" {v}" if v else ""
    return (f"marker{vs} (force_ocr: every page OCR'd, inline maths as LaTeX)" if force_ocr else
            f"marker{vs} (no force_ocr: text reused from the PDF's text layer)")


def precheck_text_layer(pdf: Path, *, force_ocr: bool):
    """Inspect the PDF's text layer before marker runs; log what was found."""
    from ..textlayer import inspect_text_layer

    rep = inspect_text_layer(pdf)
    log.info("Text layer check: %s", rep.summary())
    for ex in rep.examples[:3]:
        log.info("  e.g. %r", ex)
    if force_ocr:
        if rep.broken:
            log.info("The text layer's maths is garbled; force_ocr is on, so marker OCRs the page images instead.")
        return rep
    if rep.broken:
        log.warning("=" * 70)
        log.warning("This PDF's text layer has GARBLED MATHS (%s).", "; ".join(rep.reasons()))
        log.warning("With --no-force-ocr marker reuses that text, so inline maths will come out as junk")
        log.warning("(e.g. \"A!\" for A', curly quotes for Greek letters). Drop --no-force-ocr.")
        log.warning("=" * 70)
    elif rep.suspicious:
        log.warning("Maths fonts without a ToUnicode map (%s): inline maths from the text layer may be garbled; "
                    "drop --no-force-ocr if it is.", ", ".join(rep.math_fonts_no_tounicode[:6]))
    return rep


def postcheck_marker_output(md: str, *, layer=None, force_ocr: bool = True) -> list[str]:
    """Warn loudly if marker's Markdown has few formulas but maths-like junk in its prose."""
    from ..textlayer import check_marker_output

    chk = check_marker_output(md)
    log.info("marker output: %d words, %d inline + %d display formulas", chk.words, chk.inline, chk.display)
    if not chk.garbled:
        return []
    src = (" The PDF's text layer showed the same symptoms." if layer is not None and layer.broken else "")
    hint = ("marker ran WITHOUT force_ocr; convert again without --no-force-ocr." if not force_ocr else
            "force_ocr was on, so the OCR itself went wrong; check marker.md in the working directory "
            "(--keep-work) and try `--device cpu`.")
    log.warning("=" * 70)
    log.warning("THE MATHS IN THIS EPUB IS PROBABLY GARBLED: only %d formula(s) for %d words, and the text "
                "contains %s.%s", chk.formulas, chk.words, "; ".join(chk.text.reasons()), src)
    for ex in chk.text.examples[:3]:
        log.warning("  e.g. %r", ex)
    log.warning(hint)
    log.warning("=" * 70)
    return [f"maths probably garbled ({chk.formulas} formulas, {'; '.join(chk.text.reasons())})"]


def _pdf_title(pdf: Path) -> str | None:
    try:
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            t = ((d.metadata or {}).get("title") or "").strip()
            return None if (not t or is_arxiv_stamp(t)) else t
    except Exception:
        return None


# ---------------------------------------------------------------------------- last resort

def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


JUNK_TITLES = re.compile(r"^(untitled|microsoft word|main|paper|document|arxiv|\S+\.(tex|dvi|pdf|docx?))\b", re.I)


SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "via", "with"}


def _smart_title(t: str) -> str:
    """'A TRUNCATED PERRON FORMULA OF Λ(n)' -> 'A Truncated Perron Formula of Λ(n)'."""
    letters = [c for c in t if c.isalpha() and c.isascii()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return t
    words = t.split()
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) > 1 and w.isascii():
            lw = w.lower()
            w = lw if (i and lw in SMALL_WORDS) else lw.capitalize()
        out.append(w)
    return " ".join(out)


class PdfTextBackend:
    """Plain text extraction with PyMuPDF.  NO maths reconstruction.

    Used when marker is missing or its models cannot be loaded.  Still gives a
    usable book: title from the PDF metadata (or the first page), chapters and
    a table of contents from the PDF outline (or from numbered, bold/large
    section headings), running heads and page numbers removed."""

    name = "pdftext"

    def convert(self, pdf: Path, work: Path, *, title: str | None = None, authors=None,
                reason: str = "marker-pdf is NOT installed") -> ConvertedDoc:
        import html as H

        import pymupdf

        log.warning("=" * 70)
        log.warning("%s: falling back to plain PyMuPDF text extraction.", reason)
        log.warning("Formulas will be garbled. Install marker (`pip install marker-pdf`) and let it download")
        log.warning("its models (needs internet once) for real maths.")
        log.warning("=" * 70)
        with pymupdf.open(str(pdf)) as d:
            meta = d.metadata or {}
            toc = d.get_toc(simple=True) or []
            pages = []
            sizes: dict[float, int] = {}
            for page in d:
                lines = []
                height = page.rect.height
                width = page.rect.width or 1.0
                for blk in page.get_text("dict")["blocks"]:
                    if blk.get("type") != 0:
                        continue
                    for ln in blk["lines"]:
                        spans = [sp for sp in ln["spans"] if sp["text"].strip()]
                        if not spans:
                            continue
                        text = "".join(sp["text"] for sp in ln["spans"]).strip()
                        size = max(sp["size"] for sp in spans)
                        bold = any((sp["flags"] & 16) or re.search(r"Bold|BX|Black|Heavy", sp["font"]) for sp in spans)
                        caps = any(re.search(r"CSC|SmallCaps|SC\b", sp["font"]) for sp in spans)
                        for sp in spans:
                            sizes[round(sp["size"], 1)] = sizes.get(round(sp["size"], 1), 0) + len(sp["text"])
                        d0 = ln.get("dir", (1.0, 0.0))[0]
                        lines.append({"text": text, "size": size, "bold": bold, "caps": caps,
                                      "y": ln["bbox"][1] / height, "block": id(blk),
                                      "rot": abs(d0 - 1.0) > 0.05,
                                      "margin": ln["bbox"][2] < 0.15 * width or ln["bbox"][0] > 0.85 * width})
                pages.append(lines)
        body = max(sizes, key=sizes.get) if sizes else 10.0

        # running heads / page numbers: short lines at the page edges that are
        # numbers or repeat on several pages
        edge_texts: dict[str, int] = {}
        for lines in pages:
            for ln in lines:
                if ln["y"] < 0.08 or ln["y"] > 0.92:
                    edge_texts[_norm(re.sub(r"\d+", "", ln["text"]))] = edge_texts.get(
                        _norm(re.sub(r"\d+", "", ln["text"])), 0) + 1

        def is_furniture(ln) -> bool:
            # arXiv's rotated margin stamp, whole or one glyph per line
            if is_arxiv_stamp(ln["text"]) or (ln["rot"] and ln["margin"]):
                return True
            if not (ln["y"] < 0.08 or ln["y"] > 0.92):
                return False
            if re.fullmatch(r"[\divxlc\s.\-–]+", ln["text"], re.I):
                return True
            return edge_texts.get(_norm(re.sub(r"\d+", "", ln["text"])), 0) >= 2 and len(ln["text"]) < 90

        # title: metadata, else the most prominent text near the top of page 1
        meta_title = (meta.get("title") or "").strip()
        if meta_title and (JUNK_TITLES.match(meta_title) or is_arxiv_stamp(meta_title)):
            meta_title = ""
        first_title = None
        if pages and pages[0]:
            top = [ln for ln in pages[0] if ln["y"] < 0.45 and not is_furniture(ln) and not ln["rot"]
                   and len(ln["text"]) > 3]
            if top:
                def prom(ln):
                    return ln["size"] * (1.25 if ln["bold"] else 1.0)
                best = max(prom(ln) for ln in top)
                if best > body * 1.05:
                    run = []
                    started = False
                    for ln in pages[0]:
                        if ln in top and abs(prom(ln) - best) < 0.3:
                            run.append(ln["text"])
                            started = True
                        elif started:
                            break
                    first_title = _smart_title(re.sub(r"\s+", " ", " ".join(run)).strip())
        if is_arxiv_stamp(title):
            title = None
        doc_title = title or meta_title or first_title or pdf.stem
        meta_author = (meta.get("author") or "").strip()
        doc_authors = list(authors or ([a.strip() for a in re.split(r",| and |;", meta_author) if a.strip()]
                                       if meta_author else []))

        # headings: PDF outline, else numbered bold/small-caps/large lines
        toc_by_page: dict[int, list[tuple[int, str]]] = {}
        for level, t, pno in toc:
            if pno >= 1 and level <= 3:
                toc_by_page.setdefault(pno - 1, []).append((level, t))

        def heading_level(pidx: int, ln) -> int | None:
            n = _norm(ln["text"])
            if not n:
                return None
            for level, t in toc_by_page.get(pidx, []):
                tn = _norm(t)
                if tn and (n.startswith(tn) or (len(n) >= 0.8 * len(tn) and tn.startswith(n))):
                    if ln["text"].startswith(t) and len(ln["text"]) > len(t) + 3:
                        ln["runin"] = (t, ln["text"][len(t):].strip())    # run-in heading + text
                    return level
            if toc:
                return None
            m = re.match(r"^(\d+(?:\.\d+){0,2})\.?\s+\S", ln["text"])
            prominent = ln["bold"] or ln["caps"] or ln["size"] > body * 1.1
            if m and prominent and len(ln["text"]) < 90 and not ln["text"].rstrip().endswith((",", ";")):
                return m.group(1).count(".") + 1
            if ln["size"] > body * 1.25 and len(ln["text"]) < 90:
                return 1
            if prominent and re.fullmatch(r"(References|Bibliography|Acknowledge?ments?|Notation|Appendix\b.*|"
                                          r"Introduction|Conclusions?)\.?", ln["text"].strip(), re.I):
                return 1
            return None

        parts = []
        para: list[str] = []
        last_block = None

        def flush():
            if para:
                t = " ".join(para)
                t = re.sub(r"(\w)- (\w)", r"\1\2", t)          # re-join hyphenated words
                parts.append("<p>%s</p>" % H.escape(re.sub(r"\s+", " ", t)))
                para.clear()

        for pidx, lines in enumerate(pages):
            for ln in lines:
                if is_furniture(ln):
                    continue
                if pidx == 0 and first_title and ln["text"].lower() in first_title.lower() and len(ln["text"]) > 3:
                    continue
                lvl = heading_level(pidx, ln)
                if lvl:
                    flush()
                    head, rest = ln.get("runin") or (ln["text"], "")
                    parts.append("<h%d>%s</h%d>" % (lvl, H.escape(head), lvl))
                    last_block = ln["block"] if rest else None
                    if rest:
                        para.append(rest)
                    continue
                if ln["block"] != last_block:
                    # a new text block starts a paragraph only after a sentence end
                    if para and re.search(r"[.:!?]\)?$", para[-1]):
                        flush()
                    last_block = ln["block"]
                para.append(ln["text"])
            flush()
        warn = (f"<p class=\"warning {TEXT_FALLBACK_CLASS}\"><strong>Warning:</strong> this book was produced without marker-pdf; "
                "formulas are raw extracted text and may be unreadable.</p>")
        root = parse_fragment(warn + "\n".join(parts))
        return ConvertedDoc(title=doc_title, authors=doc_authors, body=root,
                            backend="pdftext", backend_note="pdftext fallback (plain PyMuPDF text, NO maths)",
                            warnings=["plain-text fallback (no maths)"])


def _main(argv: list[str] | None = None) -> int:
    """Smoke-test marker on this machine:

        python -m math2kindle.backends.marker_backend paper.pdf [out_dir]

    Prints the torch device, how much maths marker found, and where the
    Markdown went.  Use it once after installing marker-pdf."""
    import argparse
    import logging
    import tempfile

    ap = argparse.ArgumentParser(prog="python -m math2kindle.backends.marker_backend")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--device", default=None)
    ap.add_argument("--pages", default=None, help="page range, e.g. 0-2")
    ap.add_argument("--no-force-ocr", dest="force_ocr", action="store_false",
                    help="reuse the PDF's text layer (faster; garbles maths in many publisher PDFs)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    out = a.out or Path(tempfile.mkdtemp(prefix="marker-check-"))
    print("torch device:", a.device or detect_device())
    try:
        precheck_text_layer(a.pdf, force_ocr=a.force_ocr)
        md, images, _ = run_marker(a.pdf, out, device=a.device, page_range=a.pages, force_ocr=a.force_ocr)
    except Math2KindleError as e:
        print("ERROR:", e)
        return 1
    from ..textlayer import count_formulas

    postcheck_marker_output(md, force_ocr=a.force_ocr)
    n_inl, n_disp = count_formulas(md)
    print("backend:", describe_marker_run(a.force_ocr))
    print(f"markdown: {len(md)} chars, ~{n_inl} inline / {n_disp} display formulas, {len(images)} images")
    print("written to:", out / "marker.md")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())


__all__ = ["MarkerBackend", "PdfTextBackend", "markdown_to_doc", "run_marker", "detect_device", "marker_available",
           "marker_config", "marker_cli_args", "marker_version", "precheck_text_layer", "postcheck_marker_output",
           "shutil"]
