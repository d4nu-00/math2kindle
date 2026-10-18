"""arXiv id/URL parsing, source-type detection and (mocked) downloading."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from math2kindle import inputs
from math2kindle.inputs import (ArxivId, Fetcher, classify_input, download_arxiv, extract_archive_bytes,
                                fetch_arxiv_meta, parse_arxiv_id, parse_atom, sniff_bytes)
from math2kindle.util import Math2KindleError


@pytest.mark.parametrize("s,expected", [
    ("2101.00001", ArxivId("2101.00001", "")),
    ("2101.00001v2", ArxivId("2101.00001", "v2")),
    ("arXiv:2101.00001", ArxivId("2101.00001", "")),
    ("arxiv:2101.00001V3", ArxivId("2101.00001", "v3")),
    ("0704.0001", ArxivId("0704.0001", "")),
    ("math/0601001", ArxivId("math/0601001", "")),
    ("math/0601001v3", ArxivId("math/0601001", "v3")),
    ("math.NT/0601001", ArxivId("math/0601001", "")),
    ("hep-th/9901001v1", ArxivId("hep-th/9901001", "v1")),
    ("https://arxiv.org/abs/2101.00001", ArxivId("2101.00001", "")),
    ("https://arxiv.org/abs/2101.00001v2", ArxivId("2101.00001", "v2")),
    ("https://arxiv.org/pdf/2101.00001v2", ArxivId("2101.00001", "v2")),
    ("https://arxiv.org/pdf/2101.00001v2.pdf", ArxivId("2101.00001", "v2")),
    ("http://export.arxiv.org/abs/math/0601001", ArxivId("math/0601001", "")),
    ("https://arxiv.org/abs/math/0601001v2", ArxivId("math/0601001", "v2")),
    ("https://www.arxiv.org/e-print/2101.00001", ArxivId("2101.00001", "")),
    ("https://arxiv.org/abs/2101.00001?context=math", ArxivId("2101.00001", "")),
])
def test_parse_arxiv_id(s, expected):
    assert parse_arxiv_id(s) == expected


@pytest.mark.parametrize("s", ["hello", "2101.0001x", "21010.00001", "https://example.com/abs/2101.00001",
                               "math/060100", "", "paper.pdf"])
def test_parse_arxiv_id_rejects(s):
    assert parse_arxiv_id(s) is None


def test_arxiv_id_helpers():
    a = ArxivId("math/0601001", "v2")
    assert a.full == "math/0601001v2"
    assert a.safe == "math_0601001v2"


def test_classify_input(tmp_path, fixtures):
    assert classify_input(str(fixtures / "paper.pdf")).kind == "pdf"
    assert classify_input(str(fixtures / "paper_src.tar.gz")).kind == "archive"
    assert classify_input(str(fixtures / "paper_src" / "main.tex")).kind == "tex"
    assert classify_input(str(fixtures / "paper_src")).kind == "dir"
    spec = classify_input("2101.00001v2")
    assert spec.kind == "arxiv" and spec.arxiv.version == "v2"
    noext = tmp_path / "download"
    noext.write_bytes((fixtures / "paper.pdf").read_bytes())
    assert classify_input(str(noext)).kind == "pdf"
    with pytest.raises(Math2KindleError):
        classify_input("definitely-not-a-file-or-id")


def test_sniff_bytes(fixtures):
    assert sniff_bytes((fixtures / "paper_src.tar.gz").read_bytes()) == "gzip"
    assert sniff_bytes((fixtures / "paper.pdf").read_bytes()) == "pdf"
    assert sniff_bytes(b"<!DOCTYPE html><html>") == "html"
    assert sniff_bytes(b"\\documentclass{article}") == "text"


# ---------------------------------------------------------------- extraction

def _tar_bytes(members: dict[str, bytes], gz=True, extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tf.addfile(info)
    return buf.getvalue()


def test_extract_tarball(tmp_path, fixtures):
    b = extract_archive_bytes((fixtures / "paper_src.tar.gz").read_bytes(), tmp_path / "src")
    assert b.kind == "tex"
    assert (b.path / "main.tex").exists()
    assert (b.path / "sections" / "perron.tex").exists()
    assert (b.path / "figures" / "contour.pdf").exists()


def test_extract_gzipped_single_tex(tmp_path):
    tex = b"\\documentclass{article}\\begin{document}Hi $x$\\end{document}"
    b = extract_archive_bytes(gzip.compress(tex), tmp_path / "src")
    assert b.kind == "tex"
    assert (b.path / "main.tex").read_bytes() == tex


def test_extract_pdf_and_gzipped_pdf(tmp_path, fixtures):
    pdf = (fixtures / "paper.pdf").read_bytes()
    b = extract_archive_bytes(pdf, tmp_path / "a")
    assert b.kind == "pdf" and b.path.read_bytes()[:5] == b"%PDF-"
    b = extract_archive_bytes(gzip.compress(pdf), tmp_path / "b")
    assert b.kind == "pdf"


def test_extract_uncompressed_tar(tmp_path):
    data = _tar_bytes({"paper.tex": b"\\documentclass{article}\\begin{document}x\\end{document}"}, gz=False)
    b = extract_archive_bytes(data, tmp_path / "s")
    assert b.kind == "tex" and (b.path / "paper.tex").exists()


def test_extract_rejects_html(tmp_path):
    with pytest.raises(Math2KindleError):
        extract_archive_bytes(b"<!DOCTYPE html><html><body>rate limited</body></html>", tmp_path / "x")


def test_extract_is_safe(tmp_path):
    link = tarfile.TarInfo("evil-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    data = _tar_bytes({
        "main.tex": b"\\documentclass{article}\\begin{document}x\\end{document}",
        "../escape.tex": b"bad",
        "/abs/escape2.tex": b"bad",
    }, extra=[link])
    dest = tmp_path / "inner" / "src"
    b = extract_archive_bytes(data, dest)
    assert b.kind == "tex"
    assert not (tmp_path / "inner" / "escape.tex").exists()
    assert not (dest / "evil-link").exists()
    for p in dest.rglob("*"):
        assert dest.resolve() in p.resolve().parents


# ---------------------------------------------------------------- network (mocked)

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2101.00001v2</id>
    <published>2021-01-01T00:00:00Z</published>
    <title>A truncated Perron formula and
      short sums</title>
    <summary>  We prove   things.  </summary>
    <author><name>Ada N. Example</name></author>
    <author><name>Ben Q. Placeholder</name></author>
  </entry>
</feed>"""


class FakeFetcher(Fetcher):
    def __init__(self, routes: dict[str, bytes]):
        super().__init__()
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        for prefix, data in self.routes.items():
            if url.startswith(prefix):
                if isinstance(data, Exception):
                    raise data
                return data, {}
        raise Math2KindleError(f"HTTP 404 for {url}")


def test_parse_atom():
    m = parse_atom(ATOM)
    assert m.title == "A truncated Perron formula and short sums"
    assert m.authors == ["Ada N. Example", "Ben Q. Placeholder"]
    assert m.abstract == "We prove things."
    assert m.published == "2021-01-01"
    assert parse_atom(b"not xml") is None


def test_fetch_meta_uses_export_api():
    f = FakeFetcher({"https://export.arxiv.org/api/query": ATOM})
    m = fetch_arxiv_meta(ArxivId("2101.00001", "v2"), f)
    assert m.title.startswith("A truncated")
    assert "id_list=2101.00001v2" in f.calls[0]


def test_fetch_meta_failure_is_not_fatal():
    f = FakeFetcher({})
    assert fetch_arxiv_meta(ArxivId("2101.00001"), f) is None


@pytest.mark.parametrize("payload_kind", ["tarball", "gztex", "pdf"])
def test_download_arxiv_source_types(tmp_path, fixtures, payload_kind):
    tex = b"\\documentclass{article}\\begin{document}x\\end{document}"
    payload = {
        "tarball": (fixtures / "paper_src.tar.gz").read_bytes(),
        "gztex": gzip.compress(tex),
        "pdf": (fixtures / "paper.pdf").read_bytes(),
    }[payload_kind]
    f = FakeFetcher({"https://arxiv.org/e-print/2101.00001": payload})
    b = download_arxiv(ArxivId("2101.00001"), tmp_path / "dl", f)
    assert b.kind == ("pdf" if payload_kind == "pdf" else "tex")
    assert f.calls == ["https://arxiv.org/e-print/2101.00001"]


def test_download_arxiv_html_falls_back_to_pdf(tmp_path, fixtures):
    f = FakeFetcher({"https://arxiv.org/e-print/": b"<html>withdrawn</html>",
                     "https://arxiv.org/pdf/": (fixtures / "paper.pdf").read_bytes()})
    b = download_arxiv(ArxivId("2101.00001", "v1"), tmp_path / "dl", f)
    assert b.kind == "pdf"
    assert f.calls[-1] == "https://arxiv.org/pdf/2101.00001v1"


def test_fetcher_sends_user_agent(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        headers = {"Content-Type": "application/gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return Resp(b"data")

    monkeypatch.setattr(inputs.urllib.request, "urlopen", fake_urlopen)
    data, headers = Fetcher().get("https://arxiv.org/e-print/2101.00001")
    assert data == b"data"
    assert seen["ua"].startswith("math2kindle/")
