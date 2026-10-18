"""PDF route safety: arXiv stamps in PDFs, llama.cpp preflight, surya server cleanup,
and failing closed instead of shipping garbled maths.  All network access is mocked."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest

from math2kindle import cli, inputs
from math2kindle.backends import marker_backend
from math2kindle.backends.marker_backend import PdfTextBackend, markdown_to_doc
from math2kindle.inputs import ArxivId, detect_arxiv_id_in_pdf, find_arxiv_stamp, is_arxiv_stamp
from math2kindle.util import Math2KindleError

from .conftest import FIXTURES, read_epub, requires_node
from .test_inputs import ATOM, FakeFetcher

STAMP = "arXiv:1406.7309v1  [math.MG]  27 Jun 2014"


def stamped_pdf(path: Path, stamp: str = STAMP, base: Path | None = None, rotate: int = 90) -> Path:
    """A PDF with arXiv's rotated margin stamp on page 1 (as arXiv prints it: 20pt, grey)."""
    if base is not None:
        d = pymupdf.open(str(base))
    else:
        d = pymupdf.open()
        p = d.new_page(width=612, height=792)
        p.insert_text((150, 120), "ON KLEIN'S SO-CALLED NON-EUCLIDEAN GEOMETRY", fontsize=12, fontname="tibo")
        p.insert_text((200, 150), "NORBERT A'CAMPO AND ATHANASE PAPADOPOULOS", fontsize=9, fontname="tiro")
        for i in range(30):
            p.insert_text((100, 200 + 14 * i), f"Body text of the paper with words and more words {i}.",
                          fontsize=10, fontname="tiro")
    d[0].insert_text((32, 560), stamp, fontsize=20, fontname="tiro", rotate=rotate, color=(0.5, 0.5, 0.5))
    d.save(str(path))
    d.close()
    return path


# ---------------------------------------------------------------- stamp recognition

@pytest.mark.parametrize("s,expected", [
    (STAMP, ArxivId("1406.7309", "v1")),
    ("arXiv 1406.7309v1 math.MG 27 Jun 2014", ArxivId("1406.7309", "v1")),        # sanitised filename form
    ("a r X i v : 1406 .7309v1 [ math.MG ] 27 Jun 2014", ArxivId("1406.7309", "v1")),
    ("arXiv:2101.00001v12 [math.NT] 3 Jan 2021", ArxivId("2101.00001", "v12")),
    ("arXiv:math/0601001v1 [math.NT] 1 Jan 2006", ArxivId("math/0601001", "v1")),
    ("arXiv:math.NT/0601001v2  4 Jan 2006", ArxivId("math/0601001", "v2")),
    ("arXiv:hep-th/9901001v1  2 Jan 1999", ArxivId("hep-th/9901001", "v1")),
    ("1406.7309v1 [math.MG] 27 Jun 2014", ArxivId("1406.7309", "v1")),
])
def test_stamp_patterns(s, expected):
    assert find_arxiv_stamp(s) == expected
    assert is_arxiv_stamp(s)


@pytest.mark.parametrize("s", ["On Klein's so-called Non-Euclidean geometry", "arXiv", "", None,
                               "See arXiv:1406.7309 for the proof of the main theorem",
                               "Received 27 Jun 2014", "1406.7309"])
def test_not_a_stamp(s):
    assert not is_arxiv_stamp(s)


def test_invalid_month_is_rejected():
    assert find_arxiv_stamp("arXiv:1413.7309v1 [math.MG] 27 Jun 2014") is None


def test_detect_rotated_margin_stamp(tmp_path):
    assert detect_arxiv_id_in_pdf(stamped_pdf(tmp_path / "s.pdf")) == ArxivId("1406.7309", "v1")


def test_detect_old_style_stamp(tmp_path):
    pdf = stamped_pdf(tmp_path / "o.pdf", "arXiv:math/0601001v1  [math.NT]  1 Jan 2006")
    assert detect_arxiv_id_in_pdf(pdf) == ArxivId("math/0601001", "v1")


def test_detect_ignores_citations_and_plain_pdfs(tmp_path, fixtures):
    assert detect_arxiv_id_in_pdf(fixtures / "paper.pdf") is None
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((100, 300), "This answers a question raised in arXiv:2101.00001 by the authors.", fontsize=10)
    d.save(str(tmp_path / "cite.pdf"))
    assert detect_arxiv_id_in_pdf(tmp_path / "cite.pdf") is None
    (tmp_path / "junk.pdf").write_bytes(b"%PDF-1.4 not really")
    assert detect_arxiv_id_in_pdf(tmp_path / "junk.pdf") is None


# ---------------------------------------------------------------- stamps never become titles

def test_pdftext_title_skips_stamp(tmp_path):
    doc = PdfTextBackend().convert(stamped_pdf(tmp_path / "s.pdf"), tmp_path)
    assert doc.title == "On Klein's So-called Non-euclidean Geometry"
    text = "".join(doc.body.itertext())
    assert "1406.7309" not in text


def test_pdftext_ignores_stamp_as_given_title(tmp_path, fixtures):
    doc = PdfTextBackend().convert(fixtures / "paper.pdf", tmp_path, title="arXiv 1406.7309v1 math.MG 27 Jun 2014")
    assert doc.title.startswith("A Truncated Perron Formula")


def test_marker_markdown_drops_stamp_heading(tmp_path):
    md = "# arXiv:1406.7309v1 [math.MG] 27 Jun 2014\n\n# On Klein geometry\n\nText $x$.\n"
    doc = markdown_to_doc(md, {}, tmp_path)
    assert doc.title == "On Klein geometry"
    assert "1406.7309" not in "".join(doc.body.itertext())


# ---------------------------------------------------------------- CLI: arXiv source first

def _no_marker(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("marker must not run")

    monkeypatch.setattr(marker_backend, "run_marker", boom)


def _fake_marker(monkeypatch, calls):
    def fake_run_marker(pdf, out_dir, **kw):
        calls.append(pdf)
        md = (FIXTURES / "marker_sample" / "paper.md").read_text()
        return md, {"_page_4_Figure_1.jpeg": FIXTURES / "marker_sample" / "_page_4_Figure_1.jpeg"}, {}

    monkeypatch.setattr(marker_backend, "run_marker", fake_run_marker)
    monkeypatch.setattr(marker_backend, "marker_available", lambda: True)


@requires_node
def test_stamped_pdf_uses_arxiv_latex_source(tmp_path, monkeypatch, fixtures):
    pdf = stamped_pdf(tmp_path / "on_kleins_so_called_non.pdf", base=fixtures / "paper.pdf")
    fake = FakeFetcher({"https://export.arxiv.org/api/query": ATOM,
                        "https://arxiv.org/e-print/1406.7309v1": (fixtures / "paper_src.tar.gz").read_bytes()})
    monkeypatch.setattr(inputs, "Fetcher", lambda *a, **k: fake)
    _no_marker(monkeypatch)
    out = tmp_path / "out"
    rc = cli.main([str(pdf), "--out", str(out), "-q"])
    assert rc == 0
    assert "https://arxiv.org/e-print/1406.7309v1" in fake.calls
    assert not any("/pdf/" in u for u in fake.calls)            # we already have the PDF
    epub = next(out.glob("*.epub"))
    assert epub.name == "A truncated Perron formula and short sums.epub"      # arXiv metadata title
    files = read_epub(epub)
    assert "<dc:source>arXiv:1406.7309v1</dc:source>" in files["OEBPS/content.opf"].decode()
    assert '<strong class="thm-name">Lemma 2.1</strong>' in files["OEBPS/text/ch002.xhtml"].decode()


@requires_node
@pytest.mark.parametrize("payload", ["pdf-only", "network-error"])
def test_stamped_pdf_falls_back_to_local_pdf(tmp_path, monkeypatch, fixtures, payload):
    pdf = stamped_pdf(tmp_path / "s.pdf", base=fixtures / "paper.pdf")
    routes = {"https://export.arxiv.org/api/query": ATOM,
              "https://arxiv.org/e-print/": (fixtures / "paper.pdf").read_bytes()}
    if payload == "network-error":
        routes = {"https://": Math2KindleError("download failed: <urlopen error [Errno 8] nodename>")}
    fake = FakeFetcher(routes)
    monkeypatch.setattr(inputs, "Fetcher", lambda *a, **k: fake)
    calls: list[Path] = []
    _fake_marker(monkeypatch, calls)
    out = tmp_path / "out"
    rc = cli.main([str(pdf), "--out", str(out), "-q"])
    assert rc == 0
    assert calls == [pdf]                                        # marker ran on the local PDF
    assert "https://arxiv.org/e-print/1406.7309v1" in fake.calls
    assert not any("/pdf/" in u for u in fake.calls)
    epub = next(out.glob("*.epub"))
    assert "1406" not in epub.name and not is_arxiv_stamp(epub.stem)
    if payload == "pdf-only":
        assert epub.name == "A truncated Perron formula and short sums.epub"


@requires_node
def test_no_arxiv_source_flag_stays_offline(tmp_path, monkeypatch, fixtures):
    pdf = stamped_pdf(tmp_path / "s.pdf", base=fixtures / "paper.pdf")
    fake = FakeFetcher({})
    monkeypatch.setattr(inputs, "Fetcher", lambda *a, **k: fake)
    calls: list[Path] = []
    _fake_marker(monkeypatch, calls)
    rc = cli.main([str(pdf), "--out", str(tmp_path / "out"), "--no-arxiv-source", "-q"])
    assert rc == 0 and calls == [pdf] and fake.calls == []
    epub = next((tmp_path / "out").glob("*.epub"))
    assert not is_arxiv_stamp(epub.stem) and "1406" not in epub.name


def test_download_arxiv_without_pdf_fallback_raises(tmp_path):
    f = FakeFetcher({"https://arxiv.org/e-print/": b"<html>withdrawn</html>"})
    with pytest.raises(Math2KindleError):
        inputs.download_arxiv(ArxivId("1406.7309", "v1"), tmp_path / "dl", f, pdf_fallback=False)
    assert f.calls == ["https://arxiv.org/e-print/1406.7309v1"]


# ---------------------------------------------------------------- fail closed on garbled maths

def _broken_marker(monkeypatch, msg="marker failed on paper.pdf: ProxyError: models.datalab.to 403"):
    def broken(*a, **k):
        raise Math2KindleError(msg)

    monkeypatch.setattr(marker_backend, "marker_available", lambda: True)
    monkeypatch.setattr(marker_backend, "run_marker", broken)


def test_text_fallback_fails_closed(tmp_path, monkeypatch, fixtures, caplog):
    _broken_marker(monkeypatch, "marker-pdf needs llama.cpp's `llama-server` binary\n  Fix:  brew install llama.cpp")
    rc = cli.main([str(fixtures / "paper.pdf"), "--out", str(tmp_path)])
    assert rc == 2
    assert not list(tmp_path.glob("*.epub"))
    assert "brew install llama.cpp" in caplog.text and "--allow-text-fallback" in caplog.text
    assert "garbles" in caplog.text


def test_text_fallback_without_marker_names_the_install(tmp_path, monkeypatch, fixtures, caplog):
    monkeypatch.setattr(marker_backend, "marker_available", lambda: False)
    rc = cli.main([str(fixtures / "paper.pdf"), "--out", str(tmp_path)])
    assert rc == 2 and not list(tmp_path.glob("*.epub"))
    assert "install_mac.sh --with-marker" in caplog.text


@requires_node
def test_text_fallback_epub_is_never_sent(tmp_path, monkeypatch, fixtures, caplog):
    import types

    sent = []
    fake = types.ModuleType("math2kindle.send")
    fake.send_epub = lambda *a, **k: sent.append(a)
    _broken_marker(monkeypatch)
    from math2kindle import send as real_send

    epub_dir = tmp_path / "out"
    monkeypatch.setitem(sys.modules, "math2kindle.send", fake)
    rc = cli.main([str(fixtures / "paper.pdf"), "--out", str(epub_dir), "--allow-text-fallback", "--send"])
    assert rc == 2 and sent == []
    assert "NOT e-mailed" in caplog.text
    epub = next(epub_dir.glob("*.epub"))
    monkeypatch.setitem(sys.modules, "math2kindle.send", real_send)
    assert real_send.is_text_fallback_epub(epub)
    with pytest.raises(real_send.SendError, match="plain-text fallback"):
        real_send.send_epub(epub, config_path=tmp_path / "missing.toml")


def test_is_text_fallback_epub_false_for_normal_and_junk(tmp_path):
    from math2kindle.send import is_text_fallback_epub

    p = tmp_path / "a.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("OEBPS/text/ch001.xhtml", '<p class="warning">hi</p>')
    assert not is_text_fallback_epub(p)
    (tmp_path / "b.epub").write_bytes(b"not a zip")
    assert not is_text_fallback_epub(tmp_path / "b.epub")


# ---------------------------------------------------------------- llama.cpp preflight

@pytest.fixture
def new_surya(tmp_path, monkeypatch):
    """Pretend the installed surya is a llama.cpp-era one, with no llama-server anywhere."""
    sd = tmp_path / "surya"
    (sd / "inference" / "backends").mkdir(parents=True)
    (sd / "inference" / "backends" / "llamacpp.py").write_text("")
    monkeypatch.setattr(marker_backend, "_surya_dir", lambda: sd)
    monkeypatch.setattr(marker_backend, "_LLAMA_DIRS", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    for v in ("LLAMA_CPP_BINARY", "SURYA_INFERENCE_URL", "SURYA_INFERENCE_BACKEND"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def test_llama_server_missing_fails_fast(new_surya, tmp_path, fixtures):
    msg = marker_backend.llama_server_problem("mps")
    assert msg and "brew install llama.cpp" in msg and "LLAMA_CPP_BINARY" in msg
    with pytest.raises(Math2KindleError, match="brew install llama.cpp"):
        marker_backend.run_marker(fixtures / "paper.pdf", tmp_path / "m", device="mps")
    assert marker_backend.llama_server_problem("cuda") is None           # vLLM on NVIDIA


def test_llama_server_found(new_surya, tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "llama-server"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("LLAMA_CPP_BINARY", str(exe))
    assert marker_backend.llama_server_problem("mps") is None
    monkeypatch.setenv("LLAMA_CPP_BINARY", str(tmp_path / "nope"))
    assert "LLAMA_CPP_BINARY=" in marker_backend.llama_server_problem("mps")
    monkeypatch.delenv("LLAMA_CPP_BINARY")
    monkeypatch.setenv("PATH", str(exe.parent))
    assert marker_backend.llama_server_problem("cpu") is None


def test_llama_server_found_in_homebrew_dir(new_surya, tmp_path, monkeypatch):
    exe = tmp_path / "brew" / "llama-server"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(marker_backend, "_LLAMA_DIRS", (str(exe.parent),))
    assert marker_backend.llama_server_problem("mps") is None
    assert os.environ["LLAMA_CPP_BINARY"] == str(exe)


def test_old_marker_needs_no_llama(monkeypatch, tmp_path):
    monkeypatch.setattr(marker_backend, "_surya_dir", lambda: tmp_path)
    assert marker_backend.llama_server_problem("mps") is None


# ---------------------------------------------------------------- keep-alive server cleanup

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")
def test_new_surya_servers_are_stopped(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(marker_backend, "SURYA_CACHE", tmp_path)
    monkeypatch.delenv("M2K_KEEP_MARKER_SERVERS", raising=False)
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)", "-m", "surya.fast_layout.server"]
    old = subprocess.Popen(sleeper)
    new = subprocess.Popen(sleeper)
    try:
        (tmp_path / "ocr_error_server.json").write_text(json.dumps({"pid": old.pid, "cleanup_kind": "process"}))
        before = marker_backend.surya_servers()
        (tmp_path / "fast_layout_server.json").write_text(json.dumps({"pid": new.pid, "cleanup_kind": "process"}))
        (tmp_path / "llamacpp_server.json").write_text(json.dumps({"pid": 999999, "cleanup_kind": "process"}))
        stopped = marker_backend.stop_new_surya_servers(before)
        assert stopped == ["fast_layout"]
        assert new.wait(timeout=15) is not None
        assert old.poll() is None                                   # was running before: not ours
        assert not (tmp_path / "fast_layout_server.json").exists()
        assert (tmp_path / "ocr_error_server.json").exists()
        assert (tmp_path / "llamacpp_server.json").exists()          # surya's atexit owns llama-server
    finally:
        for p in (old, new):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_keep_servers_env_and_foreign_pids(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(marker_backend, "SURYA_CACHE", tmp_path)
    (tmp_path / "detection_server.json").write_text(json.dumps({"pid": os.getpid(), "cleanup_kind": "process"}))
    monkeypatch.setenv("M2K_KEEP_MARKER_SERVERS", "1")
    assert marker_backend.stop_new_surya_servers({}) == []
    monkeypatch.delenv("M2K_KEEP_MARKER_SERVERS")
    # our own pid is not a surya server (pytest's cmdline has no "surya"): never killed
    monkeypatch.setattr(marker_backend, "_is_surya_process", lambda pid: False)
    assert marker_backend.stop_new_surya_servers({}) == []
