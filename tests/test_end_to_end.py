"""End-to-end: LaTeX tarball / arXiv (mocked) / marker-style Markdown / PDF -> valid EPUB 3."""

from __future__ import annotations

import re
import sys
import types
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from math2kindle import cli, inputs
from math2kindle.backends import marker_backend
from math2kindle.backends.marker_backend import PdfTextBackend, markdown_to_doc
from math2kindle.pipeline import Builder

from .conftest import read_epub, requires_node, run_epubcheck
from .test_inputs import ATOM, FakeFetcher

XHTML_NS = "{http://www.w3.org/1999/xhtml}"


def _xhtml_docs(files: dict[str, bytes]) -> dict[str, etree._Element]:
    return {n: etree.fromstring(b) for n, b in files.items() if n.endswith(".xhtml")}


# ---------------------------------------------------------------- the fixture paper (LaTeX backend)

@requires_node
def test_paper_epub_is_valid(paper_epub):
    ok, out = run_epubcheck(paper_epub)
    assert ok, out


@requires_node
def test_paper_epub_structure(paper_epub):
    assert paper_epub.name == "A truncated Perron formula and short sums of Lambda(n) in arithmetic progressions.epub"
    with zipfile.ZipFile(paper_epub) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    files = read_epub(paper_epub)
    docs = _xhtml_docs(files)
    opf = files["OEBPS/content.opf"].decode()
    assert "<dc:title>A truncated Perron formula and short sums of" in opf
    assert "Ada N. Example" in opf and "Ben Q. Placeholder" in opf
    nav = files["OEBPS/nav.xhtml"].decode()
    for t in ("1 Introduction", "2 Preliminaries", "2.1 A discontinuous integral", "3 Primes in short",
              "References"):
        assert t in nav
    # one chapter per top-level section (+ title page)
    chapters = sorted(n for n in files if re.match(r"OEBPS/text/ch\d+\.xhtml", n))
    assert len(chapters) == 5
    alltext = "".join(b.decode() for n, b in files.items() if n.endswith(".xhtml"))
    assert "M2K" not in alltext                       # no placeholder tokens leak
    assert "\\begin{" not in re.sub(r'alt="[^"]*"', "", alltext)   # no raw TeX in visible text
    # every image referenced exists, every maths image has its TeX as alt text
    ids, hrefs = set(), []
    for name, root in docs.items():
        for el in root.iter():
            if el.get("id"):
                ids.add((name.rsplit("/", 1)[-1], el.get("id")))
            if el.tag == XHTML_NS + "img":
                src = (Path(name).parent / el.get("src")).as_posix()
                src = re.sub(r"[^/]+/\.\./", "", src)
                assert src in files, src
                if "m" in (el.get("class") or "").split()[0][:2]:
                    assert el.get("alt") is not None
            if el.tag == XHTML_NS + "a" and el.get("href", "").startswith(("ch", "#", "title")):
                hrefs.append((name.rsplit("/", 1)[-1], el.get("href")))
    for here, href in hrefs:                          # internal links resolve
        f, _, frag = href.partition("#")
        if frag:
            assert ((f or here), frag) in ids, href


@requires_node
def test_paper_semantics(paper_epub):
    files = read_epub(paper_epub)
    ch1 = files["OEBPS/text/ch001.xhtml"].decode()
    ch2 = files["OEBPS/text/ch002.xhtml"].decode()
    ch3 = files["OEBPS/text/ch003.xhtml"].decode()
    title = files["OEBPS/text/title.xhtml"].decode()
    assert 'class="abstract"' in title and "Abstract" in title
    assert '<span class="secnum">1</span> Introduction' in ch1
    # theorem-like environments with numbering and styles
    assert re.search(r'class="thm thm-plain"[^>]*>\s*<p[^>]*><strong class="thm-name">Lemma 2.1</strong>', ch2)
    assert '<strong class="thm-name">Definition 2.2</strong>' in ch2 and "thm-definition" in ch2
    assert '<strong class="thm-name">Theorem 2.3</strong>' in ch2
    assert '<strong class="thm-name">Remark 2.5</strong>' in ch2 and "thm-remark" in ch2
    assert '<em class="proof-name">Proof.</em>' in ch2 and "∎" in ch2
    # cross references, equation numbers, citations, footnotes, figures, tables
    assert re.search(r'<a href="ch002.xhtml#eq-\d+">\(2.4\)</a>', ch3)
    assert re.search(r'<a href="ch002.xhtml#thm-\d+">Theorem 2.3</a>', ch3)
    assert re.search(r'\[<a href="ch005.xhtml#bib-\d+" class="cite">3</a>, Ch. 12\]', ch3)
    assert 'epub:type="noteref"' in ch1 and 'epub:type="footnote"' in ch1
    assert "<figure" in ch3 and "Figure 1." in ch3 and "images/fig/" in ch3
    assert "<table" in ch3 and "Table 1." in ch3
    assert re.search(r'<span [^>]*class="itemlabel"[^>]*>\(ii\)<', ch2)
    # equation numbers are real text next to (or below) the formula image
    assert '<span class="eqno">(2.4)</span>' in ch2 or re.search(r'<span class="eqno"[^>]*>\(2\.4\)</span>', ch2)
    assert not re.search(r'alt="[^"]*\\tag\{2\.4\}[^"]*"[^>]*class="md full"', ch2)
    # display images never much wider than the 16em line (no shrinking at large fonts)
    for w in re.findall(r'class="md"[^>]*style="width:([\d.]+)em', "".join(v.decode() for k, v in files.items()
                                                                          if k.endswith(".xhtml"))):
        assert float(w) <= 16 * 1.5, w
    # fractions in table cells use display size
    assert re.search(r'<td[^>]*><img [^>]*alt="\\dfrac\{3\}', ch3)
    # every display equation is a block, never inside a <p>
    for doc in _xhtml_docs(files).values():
        for div in doc.iter(XHTML_NS + "div"):
            if "eq" in (div.get("class") or "").split():
                assert div.getparent().tag != XHTML_NS + "p"


@requires_node
def test_maths_images_are_deduplicated(paper_epub):
    files = read_epub(paper_epub)
    imgs = re.findall(r'<img src="\.\./(images/m/[^"]+)"', "".join(
        b.decode() for n, b in files.items() if n.endswith(".xhtml")))
    assert len(set(imgs)) < len(imgs)                 # e.g. $x$ appears many times, one file
    assert all("OEBPS/" + i in files for i in set(imgs))


# ---------------------------------------------------------------- arXiv input (network mocked)

@requires_node
def test_cli_arxiv_id_with_source(tmp_path, monkeypatch, fixtures):
    fake = FakeFetcher({
        "https://export.arxiv.org/api/query": ATOM,
        "https://arxiv.org/e-print/2101.00001v2": (fixtures / "paper_src.tar.gz").read_bytes(),
    })
    monkeypatch.setattr(inputs, "Fetcher", lambda *a, **k: fake)
    rc = cli.main(["arXiv:2101.00001v2", "--out", str(tmp_path), "-q"])
    assert rc == 0
    epub = next(tmp_path.glob("*.epub"))
    assert epub.name == "A truncated Perron formula and short sums.epub"     # title from the arXiv API
    files = read_epub(epub)
    assert "arXiv:2101.00001v2" in files["OEBPS/text/title.xhtml"].decode()
    assert "<dc:source>arXiv:2101.00001v2</dc:source>" in files["OEBPS/content.opf"].decode()


@requires_node
def test_cli_arxiv_pdf_only_uses_marker(tmp_path, monkeypatch, fixtures):
    """No LaTeX source on arXiv -> marker backend (marker itself stubbed: models can't download here)."""
    fake = FakeFetcher({
        "https://export.arxiv.org/api/query": ATOM,
        "https://arxiv.org/e-print/": (fixtures / "paper.pdf").read_bytes(),
    })
    monkeypatch.setattr(inputs, "Fetcher", lambda *a, **k: fake)
    calls = []

    def fake_run_marker(pdf, out_dir, **kw):
        calls.append(pdf.read_bytes()[:5])
        md = (fixtures / "marker_sample" / "paper.md").read_text()
        return md, {"_page_4_Figure_1.jpeg": fixtures / "marker_sample" / "_page_4_Figure_1.jpeg"}, {}

    monkeypatch.setattr(marker_backend, "run_marker", fake_run_marker)
    monkeypatch.setattr(marker_backend, "marker_available", lambda: True)
    rc = cli.main(["https://arxiv.org/abs/2101.00001", "--out", str(tmp_path), "-q"])
    assert rc == 0 and calls == [b"%PDF-"]
    epub = next(tmp_path.glob("*.epub"))
    ok, out = run_epubcheck(epub)
    assert ok, out


# ---------------------------------------------------------------- marker-style markdown & PDF fallback

@requires_node
def test_marker_markdown_to_epub(tmp_path, fixtures):
    md = (fixtures / "marker_sample" / "paper.md").read_text()
    doc = markdown_to_doc(md, {"_page_4_Figure_1.jpeg": fixtures / "marker_sample" / "_page_4_Figure_1.jpeg"},
                          tmp_path)
    assert doc.title == "A Truncated Perron Formula and Short Sums of Λ(n) in Arithmetic Progressions"
    b = Builder(doc, tmp_path)
    out = b.build(tmp_path / "m.epub")
    assert b.stats["mathjax"] >= 40 and b.stats["source"] == 1      # \badmacro shown as TeX source
    ok, log = run_epubcheck(out.epub)
    assert ok, log
    files = read_epub(out.epub)
    text = "".join(v.decode() for k, v in files.items() if k.endswith(".xhtml"))
    assert '<code class="tex">' in text
    assert "images/fig/" in text and "<table" in text
    assert re.search(r'<img [^>]*class="mi"[^>]*/> denote', text)    # spacing around inline maths kept


def test_marker_markdown_normalisation():
    md = marker_backend.normalize_marker_markdown("a $ x+1 $ b $$y$$ c <math>z</math>")
    assert "$x+1$" in md and "\n$$\ny\n$$\n" in md and "$z$" in md


def test_detect_device_cpu_or_better():
    assert marker_backend.detect_device() in ("cuda", "mps", "cpu")


@requires_node
def test_pdftext_fallback(tmp_path, fixtures, caplog):
    doc = PdfTextBackend().convert(fixtures / "paper.pdf", tmp_path)
    assert "marker-pdf is NOT installed" in caplog.text
    out = Builder(doc, tmp_path).build(tmp_path / "p.epub")
    ok, log = run_epubcheck(out.epub)
    assert ok, log


# ---------------------------------------------------------------- sending (send.py is stubbed)

@requires_node
def test_send_is_called_lazily_with_dry_run(tmp_path, monkeypatch, fixtures):
    calls = []
    fake = types.ModuleType("math2kindle.send")
    fake.send_epub = lambda epub_path, *, config_path=None, dry_run=False: calls.append(
        (Path(epub_path), config_path, dry_run))
    monkeypatch.setitem(sys.modules, "math2kindle.send", fake)
    cfg = tmp_path / "cfg.toml"
    rc = cli.main([str(fixtures / "torture" / "main.tex"), "--out", str(tmp_path), "--send", "--dry-run",
                   "--config", str(cfg), "-q"])
    assert rc == 0
    assert len(calls) == 1
    epub, config, dry = calls[0]
    assert epub.suffix == ".epub" and epub.exists() and config == cfg and dry is True


@requires_node
def test_torture_document_degrades_gracefully(tmp_path):
    from .conftest import FIXTURES

    rc = cli.main([str(FIXTURES / "torture" / "main.tex"), "--out", str(tmp_path), "-q"])
    assert rc == 0
    epub = next(tmp_path.glob("*.epub"))
    ok, log = run_epubcheck(epub)
    assert ok, log
    text = "".join(v.decode() for k, v in read_epub(epub).items() if k.startswith("OEBPS/text/"))
    assert "This must vanish" not in text and "hidden" not in text.replace("hidden=", "")
    assert "$not math$" in text                         # \verb kept verbatim
    assert "Theorem 1" in text and "(1)" in text and "A An appendix" not in text
    assert "<code class=\"tex\">" in text or '<pre class="tex">' in text
    assert re.search(r'<span class="nobr"><img [^>]*/>-forms</span>', text)
    assert '<span class="nobr">2-dimensional</span>' in text


@requires_node
def test_long_multline_is_broken_and_numbered(tmp_path):
    from .test_mathrender import HEAT

    tex = tmp_path / "heat.tex"
    tex.write_text("\\documentclass{article}\\usepackage{amsmath}\\DeclareMathOperator{\\vol}{vol}"
                   "\\newcommand{\\Ric}{\\operatorname{Ric}}\\newcommand{\\abs}[1]{\\lvert #1\\rvert}"
                   "\\begin{document}\\section{Heat}The expansion\n" +
                   HEAT.replace("multline*", "multline").replace("\\tag{1.4}", "\\label{eq:heat}") +
                   "\nand \\eqref{eq:heat}.\\end{document}\n")
    rc = cli.main([str(tex), "--out", str(tmp_path / "o"), "-q"])
    assert rc == 0
    epub = next((tmp_path / "o").glob("*.epub"))
    ok, log = run_epubcheck(epub)
    assert ok, log
    text = read_epub(epub)["OEBPS/text/ch001.xhtml"].decode()
    assert re.search(r'<span class="eqno"[^>]*>\(1\)</span>', text)
    # one group of several line images; the group (and so every line) is at most
    # 1.5x the line width and is scaled as a whole on narrower pages
    groups = re.findall(r'<div class="eqgroup" style="width:([\d.]+)em"', text)
    assert len(groups) == 1 and float(groups[0]) <= 16 * 1.5 * 1.1
    assert len(re.findall(r'class="md"[^>]*style="width:[\d.]+%"', text)) >= 4
    assert text.count('class="eqno"') == 1


def test_pdftext_title_and_chapters(tmp_path, fixtures):
    doc = PdfTextBackend().convert(fixtures / "paper.pdf", tmp_path)
    assert doc.title == "A Truncated Perron Formula and Short Sums of \u039b(n) in Arithmetic Progressions"
    heads = [(h.tag, h.text) for h in doc.body.iter("h1", "h2")]
    assert ("h1", "1. Introduction") in heads and ("h2", "2.2. The truncated formula.") in heads
    assert ("h1", "References") in heads


@requires_node
def test_cli_pdf_falls_back_when_marker_models_fail(tmp_path, monkeypatch, fixtures, caplog):
    from math2kindle.util import Math2KindleError

    def broken(*a, **k):
        raise Math2KindleError("marker failed on paper.pdf: ProxyError: models.datalab.to 403")

    monkeypatch.setattr(marker_backend, "marker_available", lambda: True)
    monkeypatch.setattr(marker_backend, "run_marker", broken)
    rc = cli.main([str(fixtures / "paper.pdf"), "--out", str(tmp_path), "--allow-text-fallback"])
    assert rc == 0
    assert "falling back to plain PyMuPDF" in caplog.text
    epub = next(tmp_path.glob("*.epub"))
    assert epub.name.startswith("A Truncated Perron Formula")
    nav = read_epub(epub)["OEBPS/nav.xhtml"].decode()
    assert "1. Introduction" in nav and "3. Primes in short intervals" in nav
    ok, log = run_epubcheck(epub)
    assert ok, log
