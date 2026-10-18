"""Input handling: local files, arXiv IDs/URLs, downloading and safe extraction.

The network layer is a tiny :class:`Fetcher` object so tests (and users behind
proxies) can substitute their own implementation.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from ._version import __version__
from .util import Math2KindleError, log

USER_AGENT = f"math2kindle/{__version__} (personal e-reader converter; +https://arxiv.org/help/api)"

NEW_ID = r"(?P<new>\d{4}\.\d{4,5})(?P<newv>v\d+)?"
OLD_ID = r"(?P<old>[a-z]+(?:-[a-z]+)?(?:\.[A-Z]{2})?/\d{7})(?P<oldv>v\d+)?"
ARXIV_ID_RE = re.compile(rf"^(?:arxiv:)?\s*(?:{NEW_ID}|{OLD_ID})$", re.I)
ARXIV_URL_RE = re.compile(
    rf"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|e-print|src|html|format)/(?:{NEW_ID}|{OLD_ID})(?:\.pdf)?/?(?:[?#].*)?$",
    re.I)


@dataclass(frozen=True)
class ArxivId:
    id: str            # without version, e.g. "2101.00001" or "math/0601001"
    version: str = ""  # "v2" or ""

    @property
    def full(self) -> str:
        return self.id + self.version

    @property
    def safe(self) -> str:
        return self.full.replace("/", "_")


def parse_arxiv_id(s: str) -> ArxivId | None:
    """Recognise an arXiv identifier or arxiv.org URL; None if it isn't one."""
    s = (s or "").strip()
    m = ARXIV_ID_RE.match(s) or ARXIV_URL_RE.match(s)
    if not m:
        return None
    if m.group("new"):
        return ArxivId(m.group("new"), (m.group("newv") or "").lower())
    old = m.group("old")
    # "math.NT/0601001" -> "math/0601001" (subject class is not part of the id)
    arch, num = old.split("/")
    arch = arch.split(".")[0].lower()
    return ArxivId(f"{arch}/{num}", (m.group("oldv") or "").lower())


# ---------------------------------------------------------------------------- arXiv stamps in PDFs
#
# arXiv prints "arXiv:1406.7309v1  [math.MG]  27 Jun 2014" rotated in the left
# margin of page 1.  Text extraction may drop the colon/brackets or space it
# oddly ("a rXiv: 1406 .7309v1"), so every pattern tolerates whitespace.

_OLD_ARCHIVES = ("math|math-ph|hep-th|hep-ph|hep-lat|hep-ex|gr-qc|quant-ph|astro-ph|cond-mat|nucl-th|nucl-ex|"
                 "physics|nlin|cs|q-bio|q-fin|stat|alg-geom|dg-ga|funct-an|q-alg|solv-int|chao-dyn|patt-sol|"
                 "adap-org|comp-gas|cmp-lg|mtrl-th|supr-con|chem-ph|atom-ph|acc-phys|plasm-ph|ao-sci|bayes-an")
_S = r"\s*"
_STAMP_NEW = rf"(?P<new>\d{{4}}{_S}\.{_S}\d{{4,5}}(?!\d))"
_STAMP_OLD = rf"(?P<old>(?:{_OLD_ARCHIVES})(?:{_S}\.{_S}[A-Z]{{2}})?{_S}/{_S}\d{{7}})"
_STAMP_VER = rf"(?:{_S}(?P<ver>v{_S}\d+))?"
_STAMP_CAT = rf"(?:\[{_S})?[a-z][a-z\-]*(?:{_S}\.{_S}[A-Za-z\-]+)?(?:{_S}\])?"
_STAMP_DATE = rf"\d{{1,2}}{_S}(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?{_S}(?:19|20)\d\d"
_STAMP_PREFIX = rf"a{_S}r{_S}X{_S}i{_S}v{_S}:?{_S}"
# "arXiv:<id>[vN] [cat] [date]"
ARXIV_STAMP_RE = re.compile(
    rf"{_STAMP_PREFIX}(?:{_STAMP_NEW}|{_STAMP_OLD}){_STAMP_VER}(?:\s+{_STAMP_CAT})?(?:\s+{_STAMP_DATE})?", re.I)
# "<id>vN [cat] date" without the arXiv prefix (only the full stamp shape counts)
ARXIV_BARE_STAMP_RE = re.compile(
    rf"(?<![\w.])(?:{_STAMP_NEW}|{_STAMP_OLD}){_STAMP_VER}\s+\[{_S}[a-z][a-z\-]*(?:\.[A-Za-z\-]+)?{_S}\]\s+{_STAMP_DATE}",
    re.I)


def _stamp_to_id(m: re.Match) -> ArxivId | None:
    ver = re.sub(r"\s+", "", m.group("ver") or "").lower()
    if m.group("new"):
        num = re.sub(r"\s+", "", m.group("new"))
        if not 1 <= int(num[2:4]) <= 12:          # YYMM
            return None
        return ArxivId(num, ver)
    return parse_arxiv_id(re.sub(r"\s+", "", m.group("old")) + ver)


def is_arxiv_stamp(text: str | None) -> bool:
    """True if ``text`` is (essentially) nothing but an arXiv identifier stamp,
    e.g. "arXiv:1406.7309v1 [math.MG] 27 Jun 2014" or its sanitised filename
    form "arXiv 1406.7309v1 math.MG 27 Jun 2014".  Such text is never a title."""
    t = (text or "").strip().strip(".,;:()[]{}")
    if not t:
        return False
    for rx in (ARXIV_STAMP_RE, ARXIV_BARE_STAMP_RE):
        m = rx.match(t)
        if m and not t[m.end():].strip(" .,;:()[]{}"):
            return True
    return False


def find_arxiv_stamp(text: str) -> ArxivId | None:
    """The arXiv id in a stamp-like string (first match), or None."""
    for rx in (ARXIV_STAMP_RE, ARXIV_BARE_STAMP_RE):
        for m in rx.finditer(text or ""):
            aid = _stamp_to_id(m)
            if aid:
                return aid
    return None


def detect_arxiv_id_in_pdf(pdf: Path) -> ArxivId | None:
    """Find arXiv's identifier stamp on the first page of a PDF.

    Accepted: the full margin stamp (id + [category] + date), or "arXiv:<id>"
    in a rotated line or in the left/right margin.  A bare "arXiv:<id>" in the
    running text (a citation, say) is ignored."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - a core dependency
        return None
    try:
        with pymupdf.open(str(pdf)) as d:
            if d.page_count == 0:
                return None
            page = d[0]
            width = page.rect.width or 1.0
            lines = []
            for blk in page.get_text("dict")["blocks"]:
                if blk.get("type") != 0:
                    continue
                for ln in blk["lines"]:
                    text = "".join(sp["text"] for sp in ln["spans"])
                    if text.strip():
                        lines.append((text, ln.get("dir", (1.0, 0.0)), ln["bbox"]))
    except Exception as e:  # unreadable/encrypted PDF: just don't detect
        log.debug("arXiv stamp scan failed: %s", e)
        return None
    full = "\n".join(t for t, _, _ in lines)
    # 1. the complete stamp shape anywhere on the page (also across line breaks)
    for rx in (ARXIV_STAMP_RE, ARXIV_BARE_STAMP_RE):
        for m in rx.finditer(full):
            if m.group(0).rstrip()[-4:].isdigit() and re.search(_STAMP_DATE, m.group(0), re.I):
                aid = _stamp_to_id(m)
                if aid:
                    return aid
    # 2. "arXiv:<id>" in a rotated line or in the page margins
    for text, direction, bbox in lines:
        rotated = abs(direction[0] - 1.0) > 0.05
        margin = bbox[2] < 0.15 * width or bbox[0] > 0.85 * width
        if rotated or margin:
            aid = find_arxiv_stamp(text)
            if aid:
                return aid
    # 3. rotated text extracted one glyph per line: try the margin text joined up
    side = "".join(t for t, d, b in lines if abs(d[0] - 1.0) > 0.05 or b[2] < 0.15 * width)
    return find_arxiv_stamp(side) if side else None


@dataclass
class InputSpec:
    kind: str                       # arxiv | pdf | tex | archive | dir
    path: Path | None = None
    arxiv: ArxivId | None = None


def classify_input(s: str) -> InputSpec:
    p = Path(os.path.expanduser(s))
    if p.exists():
        if p.is_dir():
            return InputSpec("dir", p)
        name = p.name.lower()
        if name.endswith(".pdf"):
            return InputSpec("pdf", p)
        if name.endswith(".tex") or name.endswith(".ltx"):
            return InputSpec("tex", p)
        if name.endswith((".tar.gz", ".tgz", ".tar", ".gz", ".zip", ".tar.bz2", ".tar.xz")):
            return InputSpec("archive", p)
        kind = sniff_bytes(p.read_bytes()[:4096])
        if kind == "pdf":
            return InputSpec("pdf", p)
        if kind in ("gzip", "tar", "zip"):
            return InputSpec("archive", p)
        return InputSpec("tex", p)
    a = parse_arxiv_id(s)
    if a:
        return InputSpec("arxiv", arxiv=a)
    raise Math2KindleError(f"Input not found and not an arXiv id/URL: {s!r}")


# ---------------------------------------------------------------------------- network

class Fetcher:
    """HTTP GET with a proper User-Agent, retries and polite back-off."""

    def __init__(self, user_agent: str = USER_AGENT, timeout: float = 60, retries: int = 3):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries

    def get(self, url: str) -> tuple[bytes, dict]:
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read(), {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (404, 410, 403):
                    raise Math2KindleError(f"HTTP {e.code} for {url}") from e
                if e.code in (429, 503):
                    wait = float(e.headers.get("Retry-After", 5 * (attempt + 1)) or 5)
                    log.info("arXiv asked us to wait %.0fs", wait)
                    time.sleep(min(wait, 60))
                    continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise Math2KindleError(f"download failed: {url}: {last}")


# ---------------------------------------------------------------------------- arXiv

@dataclass
class ArxivMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""


def parse_atom(xml_bytes: bytes) -> ArxivMeta | None:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    entry = root.find("a:entry", ns)
    if entry is None:
        return None
    title = re.sub(r"\s+", " ", entry.findtext("a:title", "", ns)).strip()
    if not title or title.lower() == "error":
        return None
    return ArxivMeta(
        title=title,
        authors=[re.sub(r"\s+", " ", a.findtext("a:name", "", ns)).strip() for a in entry.findall("a:author", ns)],
        abstract=re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns)).strip(),
        published=(entry.findtext("a:published", "", ns) or "")[:10],
    )


def fetch_arxiv_meta(aid: ArxivId, fetcher: Fetcher) -> ArxivMeta | None:
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": aid.full, "max_results": 1})
    try:
        data, _ = fetcher.get(url)
    except Math2KindleError as e:
        log.warning("arXiv metadata unavailable: %s", e)
        return None
    return parse_atom(data)


def sniff_bytes(data: bytes) -> str:
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    head = data[:4096].lower()
    if b"<html" in head or b"<!doctype html" in head:
        return "html"
    return "text"


@dataclass
class SourceBundle:
    kind: str                 # "tex" (directory with sources) | "pdf"
    path: Path                # directory (tex) or file (pdf)
    note: str = ""


def _safe_members(tf: tarfile.TarFile, dest: Path):
    dest_r = dest.resolve()
    for m in tf.getmembers():
        if not (m.isfile() or m.isdir()):
            continue                      # no symlinks, devices, fifos
        name = m.name.lstrip("/")
        target = (dest / name).resolve()
        if target != dest_r and dest_r not in target.parents:
            log.warning("skipping unsafe archive member %s", m.name)
            continue
        if m.size > 500 * 1024 * 1024:
            continue
        m.name = name
        m.mode = 0o644 if m.isfile() else 0o755
        yield m


def extract_archive_bytes(data: bytes, dest: Path) -> SourceBundle:
    """Unpack arXiv e-print payload into dest (a fresh directory)."""
    dest.mkdir(parents=True, exist_ok=True)
    kind = sniff_bytes(data)
    if kind == "gzip":
        try:
            data = gzip.decompress(data)
        except OSError as e:
            raise Math2KindleError(f"corrupt gzip download: {e}")
        kind = sniff_bytes(data)
    if kind == "pdf":
        p = dest / "paper.pdf"
        p.write_bytes(data)
        return SourceBundle("pdf", p, "arXiv provides only a PDF")
    if kind == "tar" or _is_tar(data):
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            members = list(_safe_members(tf, dest))
            try:
                tf.extractall(dest, members=members, filter="data")
            except TypeError:  # Python < 3.12 without the filter argument
                tf.extractall(dest, members=members)
        texs = list(dest.rglob("*.tex"))
        if not texs:
            pdfs = list(dest.rglob("*.pdf"))
            if pdfs and not texs:
                return SourceBundle("pdf", max(pdfs, key=lambda q: q.stat().st_size), "tarball without .tex")
            raise Math2KindleError("source archive contains no .tex files")
        return SourceBundle("tex", dest)
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            dest_r = dest.resolve()
            for info in zf.infolist():
                target = (dest / info.filename).resolve()
                if dest_r not in target.parents or info.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
        return SourceBundle("tex", dest)
    if kind == "html":
        raise Math2KindleError("arXiv returned an HTML page instead of the source (rate-limited or withdrawn?)")
    # single (formerly gzipped) .tex file
    text = data.decode("utf-8", "replace")
    if "\\documentclass" in text or "\\documentstyle" in text or "\\begin{document}" in text:
        (dest / "main.tex").write_bytes(data)
        return SourceBundle("tex", dest)
    raise Math2KindleError("unrecognised arXiv source format")


def _is_tar(data: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            return bool(tf.getmembers())
    except (tarfile.TarError, EOFError, OSError):
        return False


def download_arxiv(aid: ArxivId, dest: Path, fetcher: Fetcher | None = None, *,
                   pdf_fallback: bool = True) -> SourceBundle:
    """Download https://arxiv.org/e-print/<id> into a fresh directory.

    If the payload is unusable, the arXiv PDF is fetched instead, unless
    ``pdf_fallback`` is False (the caller already has the PDF): then the error
    is raised."""
    fetcher = fetcher or Fetcher()
    data, headers = fetcher.get(f"https://arxiv.org/e-print/{aid.full}")
    log.info("Downloaded %d bytes of arXiv source for %s", len(data), aid.full)
    try:
        return extract_archive_bytes(data, dest)
    except Math2KindleError as e:
        if not pdf_fallback:
            raise
        log.warning("%s; falling back to the PDF", e)
        pdf, _ = fetcher.get(f"https://arxiv.org/pdf/{aid.full}")
        if sniff_bytes(pdf) != "pdf":
            raise
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "paper.pdf"
        p.write_bytes(pdf)
        return SourceBundle("pdf", p, "fallback PDF")


def download_arxiv_pdf(aid: ArxivId, dest: Path, fetcher: Fetcher | None = None) -> Path:
    fetcher = fetcher or Fetcher()
    data, _ = fetcher.get(f"https://arxiv.org/pdf/{aid.full}")
    if sniff_bytes(data) != "pdf":
        raise Math2KindleError("arXiv did not return a PDF")
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / f"{aid.safe}.pdf"
    p.write_bytes(data)
    return p


def extract_local_archive(path: Path, dest: Path) -> SourceBundle:
    return extract_archive_bytes(path.read_bytes(), dest)
