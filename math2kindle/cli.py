"""Command line interface: math2kindle INPUT [options]."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ._version import __version__
from .util import Math2KindleError, log, run, sanitize_filename, which


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="math2kindle",
        description="Convert a maths paper (arXiv id/URL, LaTeX source or PDF) into a Kindle-friendly EPUB "
                    "with every formula pre-rendered as a crisp image.")
    p.add_argument("input", help="arXiv id (2101.00001, math/0601001v2), arxiv.org URL, .pdf, .tex, "
                                 "source archive (.tar.gz/.zip) or source directory")
    p.add_argument("--out", "-o", type=Path, default=Path("."), help="output directory (default: .)")
    p.add_argument("--backend", choices=("auto", "latex", "marker"), default="auto",
                   help="auto: LaTeX source if available, else marker (ML PDF conversion)")
    p.add_argument("--send", action="store_true", help="e-mail the EPUB to your Send-to-Kindle address")
    p.add_argument("--dry-run", action="store_true", help="with --send: build the e-mail but do not send it")
    p.add_argument("--config", type=Path, default=None, help="send-to-kindle config file (see docs)")
    p.add_argument("--keep-work", action="store_true", help="keep the temporary working directory")
    p.add_argument("--work-dir", type=Path, default=None, help="use this working directory (implies --keep-work)")
    p.add_argument("--azw3", action="store_true", help="also produce an AZW3 with calibre's ebook-convert")
    p.add_argument("--width-em", type=float, default=16.0,
                   help="line width (in em) for display maths (default 16). Formulas that overrun it by more "
                        "than 25%% are broken at relations and top-level +/-; on narrower pages (large "
                        "fonts) a whole display is scaled by one factor")
    p.add_argument("--em-px", type=int, default=48, help="raster resolution of maths: pixels per em (default 48)")
    p.add_argument("--math-weight", type=float, default=12.0,
                   help="embolden maths strokes for e-ink, in 1/1000 em (default 12; 0 = plain MathJax)")
    p.add_argument("--math-scale", type=float, default=1.06,
                   help="size of maths relative to the body text (default 1.06)")
    p.add_argument("--justify", action="store_true", help="justify paragraphs (default: ragged right)")
    p.add_argument("--device", default=None, help="marker torch device: cuda|mps|cpu (default: auto)")
    p.add_argument("--force-ocr", action=argparse.BooleanOptionalAction, default=True,
                   help="marker: OCR every page and ignore the PDF's text layer (default: on). Publisher PDFs "
                        "often have a text layer whose maths decodes to junk; OCR yields real LaTeX, inline "
                        "maths included. --no-force-ocr reuses the text layer: faster, but only display "
                        "equations become LaTeX")
    p.add_argument("--title", default=None, help="override the book title")
    p.add_argument("--no-arxiv-source", action="store_true",
                   help="for a PDF that carries an arXiv stamp/id: do not fetch its LaTeX source from arXiv "
                        "(no network); convert the PDF itself")
    p.add_argument("--allow-text-fallback", action="store_true",
                   help="if marker cannot run, still write an EPUB from plain PyMuPDF text (formulas garbled). "
                        "Without it such a run fails. Text-only EPUBs are never e-mailed")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"math2kindle {__version__}")
    return p


def _setup_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose > 1 else logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)
    log.setLevel(level)


def convert(args, fetcher=None) -> Path:
    from .backends.latex_backend import LatexBackend
    from .backends.marker_backend import MarkerBackend, PdfTextBackend, marker_available
    from .inputs import (Fetcher, classify_input, detect_arxiv_id_in_pdf, download_arxiv, download_arxiv_pdf,
                         extract_local_archive, fetch_arxiv_meta, is_arxiv_stamp)
    from .mathrender import RenderOptions
    from .pipeline import Builder

    spec = classify_input(args.input)
    if args.work_dir:
        work = Path(args.work_dir).resolve()
        work.mkdir(parents=True, exist_ok=True)
        keep = True
    else:
        work = Path(tempfile.mkdtemp(prefix="math2kindle-"))
        keep = args.keep_work
    t0 = time.time()
    try:
        meta = None
        tex_src: Path | None = None
        pdf: Path | None = None
        source_label = source_url = None
        if spec.kind == "arxiv":
            fetcher = fetcher or Fetcher()
            aid = spec.arxiv
            source_label = f"arXiv:{aid.full}"
            source_url = f"https://arxiv.org/abs/{aid.full}"
            meta = fetch_arxiv_meta(aid, fetcher)
            if args.backend == "marker":
                pdf = download_arxiv_pdf(aid, work / "download", fetcher)
            else:
                bundle = download_arxiv(aid, work / "download", fetcher)
                if bundle.kind == "tex":
                    tex_src = bundle.path
                else:
                    pdf = bundle.path
                    if args.backend == "latex":
                        raise Math2KindleError(f"arXiv has no LaTeX source for {aid.full} ({bundle.note})")
        elif spec.kind == "pdf":
            pdf = spec.path
            aid = None if args.no_arxiv_source else detect_arxiv_id_in_pdf(pdf)
            if aid:
                fetcher = fetcher or Fetcher()
                source_label = f"arXiv:{aid.full}"
                source_url = f"https://arxiv.org/abs/{aid.full}"
                meta = fetch_arxiv_meta(aid, fetcher)
                if args.backend == "marker":
                    log.info("%s carries the arXiv stamp %s; using its arXiv metadata", pdf.name, source_label)
                else:
                    log.info("%s carries the arXiv stamp %s; trying its LaTeX source from arXiv first "
                             "(--no-arxiv-source converts the PDF directly)", pdf.name, source_label)
                    try:
                        bundle = download_arxiv(aid, work / "download", fetcher, pdf_fallback=False)
                    except Math2KindleError as e:
                        bundle = None
                        log.warning("arXiv source for %s unavailable (%s); converting the PDF instead", aid.full, e)
                    if bundle is not None and bundle.kind == "tex":
                        tex_src = bundle.path
                    elif bundle is not None:
                        log.info("arXiv has no LaTeX source for %s (%s); converting the PDF", aid.full, bundle.note)
        elif spec.kind in ("tex", "dir"):
            tex_src = spec.path
        elif spec.kind == "archive":
            bundle = extract_local_archive(spec.path, work / "source")
            if bundle.kind == "tex":
                tex_src = bundle.path
            else:
                pdf = bundle.path

        if args.backend == "marker" and pdf is None:
            raise Math2KindleError("--backend marker needs a PDF (or an arXiv id)")
        if args.backend == "latex" and tex_src is None:
            raise Math2KindleError("--backend latex needs LaTeX source (.tex, archive, directory or arXiv id)")

        doc = None
        if tex_src is not None:
            try:
                doc = LatexBackend(em_px=args.em_px, width_em=args.width_em).convert(tex_src, work)
            except Math2KindleError:
                raise
            except Exception as e:  # unexpected bug/odd source: try the PDF route for arXiv input
                log.exception("LaTeX backend failed: %s", e)
                if spec.kind == "pdf" and args.backend == "auto":
                    log.warning("Falling back to converting the PDF")
                elif spec.kind == "arxiv" and args.backend == "auto":
                    log.warning("Falling back to the PDF + marker route")
                    pdf = download_arxiv_pdf(spec.arxiv, work / "download-pdf", fetcher)
                else:
                    raise Math2KindleError(f"LaTeX conversion failed: {e}") from e
        if doc is None:
            title = meta.title if meta else None
            authors = meta.authors if meta else None
            reason = "marker-pdf is NOT installed"
            cause = "marker-pdf is not installed in this Python environment.\n" \
                    "  Fix: bash install_mac.sh --with-marker   (or: pip install marker-pdf)"
            if marker_available():
                try:
                    doc = MarkerBackend(device=args.device, force_ocr=args.force_ocr).convert(
                        pdf, work, title=title, authors=authors)
                except Math2KindleError as e:
                    # typically: llama-server missing, or models could not be downloaded/loaded
                    if args.allow_text_fallback:
                        log.warning("marker failed: %s", e)
                    reason = "marker-pdf could not run"
                    cause = str(e)
            if doc is None:
                if not args.allow_text_fallback:
                    raise Math2KindleError(
                        f"{reason}, and the only other PDF route is plain-text extraction, which garbles "
                        f"every formula. No EPUB was written.\n  Cause: {cause}\n"
                        "  To get a text-only EPUB anyway (unreadable maths; never e-mailed), re-run with "
                        "--allow-text-fallback.")
                doc = PdfTextBackend().convert(pdf, work, title=title, authors=authors, reason=reason)

        if meta:
            doc.title = meta.title or doc.title
            doc.authors = meta.authors or doc.authors
            doc.abstract = meta.abstract or doc.abstract
            doc.date = meta.published or doc.date
        if args.title:
            doc.title = args.title
        if not doc.title or is_arxiv_stamp(doc.title):     # an arXiv margin stamp is never a title
            doc.title = (pdf.stem if pdf is not None and not is_arxiv_stamp(pdf.stem) else "Untitled")
        doc.source = doc.source or source_label
        doc.source_url = doc.source_url or source_url

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (sanitize_filename(doc.title) + ".epub")
        opts = RenderOptions(em_px=args.em_px, width_em=args.width_em, stroke=args.math_weight,
                             scale=args.math_scale)
        note = doc.backend_note or {"latex": "latex (from LaTeX source)", "pdftext": "pdftext fallback"}.get(
            doc.backend, doc.backend)
        (log.warning if doc.backend == "pdftext" else log.info)("backend: %s", note)
        result = Builder(doc, work, opts, justify=args.justify).build(out_path)
        log.info("Wrote %s (%.1f MB) in %.1fs [backend: %s]", out_path, out_path.stat().st_size / 1e6,
                 time.time() - t0, note)
        if doc.warnings:
            log.info("%d warning(s) during conversion (see above)", len(doc.warnings))

        if args.azw3:
            make_azw3(out_path)
        if args.send and doc.backend == "pdftext":
            raise Math2KindleError(f"{out_path} is a plain-text fallback (formulas garbled); it was NOT e-mailed. "
                                   "Fix marker (see above) and convert again.")
        if args.send:
            from .send import send_epub  # lazy: only needed when e-mailing

            send_epub(out_path, config_path=args.config, dry_run=args.dry_run)
        return result.epub
    finally:
        if keep:
            log.info("Working directory kept at %s", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def make_azw3(epub: Path) -> Path | None:
    conv = which("ebook-convert")
    if not conv and sys.platform == "darwin":
        cand = Path("/Applications/calibre.app/Contents/MacOS/ebook-convert")
        conv = str(cand) if cand.exists() else None
    if not conv:
        log.warning("calibre's ebook-convert not found; skipping AZW3 (EPUB is what Send-to-Kindle wants anyway)")
        return None
    out = epub.with_suffix(".azw3")
    proc = run([conv, str(epub), str(out)], timeout=900)
    if proc.returncode != 0 or not out.exists():
        log.warning("ebook-convert failed: %s", proc.stderr.decode("utf-8", "replace")[-1000:])
        return None
    log.info("Wrote %s", out)
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.work_dir:
        args.keep_work = True
    previous_level = log.level
    _setup_logging(args.verbose, args.quiet)
    try:
        path = convert(args)
    except Math2KindleError as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        log.setLevel(previous_level)     # don't leak our verbosity into embedding code/tests
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
