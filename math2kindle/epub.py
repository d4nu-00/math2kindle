"""Minimal, standards-conforming EPUB 3 writer (with an EPUB 2 NCX for older readers)."""

from __future__ import annotations

import datetime as _dt
import html
import mimetypes
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .htmltree import to_xhtml

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPS_NS = "http://www.idpf.org/2007/ops"


@dataclass
class Chapter:
    filename: str
    title: str
    elements: list
    linear: bool = True


@dataclass
class TocEntry:
    title: str
    href: str
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class EpubBook:
    title: str
    authors: list[str]
    language: str
    chapters: list[Chapter]
    toc: list[TocEntry]
    assets: dict[str, Path]               # path inside OEBPS -> local file
    source: str | None = None
    description: str | None = None
    identifier_seed: str | None = None
    css: str | None = None


def default_css() -> str:
    # __file__, not importlib.resources: robust when the cwd contains the repo checkout
    return (Path(__file__).resolve().parent / "data" / "kindle.css").read_text("utf-8")


def _x(s: str) -> str:
    return html.escape(s or "", quote=True)


def _page(title: str, body_xml: str, lang: str, css_href: str = "../css/kindle.css", body_type: str | None = None) -> str:
    bt = f' epub:type="{body_type}"' if body_type else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="{XHTML_NS}" xmlns:epub="{OPS_NS}" lang="{lang}" xml:lang="{lang}">\n'
        "<head>\n"
        '<meta charset="utf-8"/>\n'
        f"<title>{_x(title)}</title>\n"
        f'<link rel="stylesheet" type="text/css" href="{css_href}"/>\n'
        "</head>\n"
        f"<body{bt}>\n{body_xml}\n</body>\n</html>\n"
    )


def _fix_xml(s: str) -> str:
    s = s.replace(' data-epub-type="', ' epub:type="')
    s = re.sub(r' data-plain="[^"]*"', "", s)
    return s


def _chapter_xhtml(ch: Chapter, lang: str) -> str:
    body = "\n".join(_fix_xml(to_xhtml(el)) for el in ch.elements)
    # images live in OEBPS/images, chapters in OEBPS/text
    body = re.sub(r'src="(images/[^"]+)"', r'src="../\1"', body)
    return _page(ch.title, body, lang, body_type="bodymatter" if ch.filename != "title.xhtml" else "frontmatter")


def _nav_ol(entries: list[TocEntry], rel: str = "") -> str:
    out = ["<ol>"]
    for e in entries:
        out.append(f'<li><a href="{_x(rel + e.href)}">{_x(e.title)}</a>')
        if e.children:
            out.append(_nav_ol(e.children, rel))
        out.append("</li>")
    out.append("</ol>")
    return "\n".join(out)


def _nav_xhtml(book: EpubBook) -> str:
    first = next((c.filename for c in book.chapters if c.filename != "title.xhtml"), book.chapters[0].filename)
    body = (
        '<nav epub:type="toc" id="toc" role="doc-toc">\n<h1>Contents</h1>\n'
        f"{_nav_ol(book.toc)}\n</nav>\n"
        '<nav epub:type="landmarks" id="landmarks" hidden="hidden">\n<ol>\n'
        '<li><a epub:type="toc" href="nav.xhtml">Contents</a></li>\n'
        f'<li><a epub:type="bodymatter" href="text/{first}">Start</a></li>\n'
        "</ol>\n</nav>"
    )
    return _page("Contents", body, book.language, css_href="css/kindle.css")


def _ncx(book: EpubBook, uid: str) -> str:
    counter = [0]

    def points(entries: list[TocEntry]) -> str:
        out = []
        for e in entries:
            counter[0] += 1
            n = counter[0]
            out.append(f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{_x(e.title)}</text></navLabel>'
                       f'<content src="{_x(e.href)}"/>')
            if e.children:
                out.append(points(e.children))
            out.append("</navPoint>")
        return "\n".join(out)

    pts = points(book.toc)

    def depth(entries, d=1):
        return max([d] + [depth(e.children, d + 1) for e in entries if e.children])

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{_x(uid)}"/><meta name="dtb:depth" content="{depth(book.toc)}"/>'
        '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
        f"<docTitle><text>{_x(book.title)}</text></docTitle>\n"
        f"<navMap>\n{pts}\n</navMap>\n</ncx>\n"
    )


def _media_type(p: str) -> str:
    ext = p.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
            "svg": "image/svg+xml", "webp": "image/webp", "css": "text/css"}.get(ext) or \
        mimetypes.guess_type(p)[0] or "application/octet-stream"


def write_epub(book: EpubBook, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    uid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "math2kindle:" + (book.identifier_seed or book.title)))
    modified = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="css/kindle.css" media-type="text/css"/>',
    ]
    spine = []
    for i, ch in enumerate(book.chapters):
        cid = f"c{i:03d}"
        manifest.append(f'<item id="{cid}" href="text/{ch.filename}" media-type="application/xhtml+xml"/>')
        lin = "" if ch.linear else ' linear="no"'
        spine.append(f'<itemref idref="{cid}"{lin}/>')
        if i == 0:
            spine.append('<itemref idref="nav"/>')
    for i, path in enumerate(sorted(book.assets)):
        manifest.append(f'<item id="a{i:05d}" href="{_x(path)}" media-type="{_media_type(path)}"/>')
    creators = "\n".join(f'<dc:creator id="cr{i}">{_x(a)}</dc:creator>' for i, a in enumerate(book.authors)) or \
        '<dc:creator id="cr0">Unknown</dc:creator>'
    desc = f"<dc:description>{_x(book.description[:2000])}</dc:description>" if book.description else ""
    src = f"<dc:source>{_x(book.source)}</dc:source>" if book.source else ""
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{book.language}">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="bookid">{uid}</dc:identifier>\n'
        f"<dc:title>{_x(book.title)}</dc:title>\n{creators}\n"
        f"<dc:language>{book.language}</dc:language>\n{desc}\n{src}\n"
        '<dc:publisher>math2kindle</dc:publisher>\n'
        f'<meta property="dcterms:modified">{modified}</meta>\n'
        "</metadata>\n"
        f"<manifest>\n{chr(10).join(manifest)}\n</manifest>\n"
        f'<spine toc="ncx">\n{chr(10).join(spine)}\n</spine>\n'
        "</package>\n"
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>\n"
    )
    tmp = out_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", _nav_xhtml(book), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", _ncx(book, uid), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/css/kindle.css", book.css or default_css(), compress_type=zipfile.ZIP_DEFLATED)
        for ch in book.chapters:
            z.writestr(f"OEBPS/text/{ch.filename}", _chapter_xhtml(ch, book.language),
                       compress_type=zipfile.ZIP_DEFLATED)
        for path, f in sorted(book.assets.items()):
            z.write(f, f"OEBPS/{path}", compress_type=zipfile.ZIP_STORED if path.endswith((".png", ".jpg", ".jpeg"))
                    else zipfile.ZIP_DEFLATED)
    tmp.replace(out_path)
    return out_path
