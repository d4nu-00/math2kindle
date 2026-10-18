"""Tiny hand-written PDFs with controlled fonts / text layers (no font files needed)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

TOUNICODE_PUA = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /M2K-Garble def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
3 beginbfchar
<41> <E001>
<42> <E002>
<43> <F021>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""

# name -> font dictionary (without the stream objects)
FONTS = {
    # WinAnsi Helvetica: the text layer decodes exactly as written (cp1252)
    "Helv": "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    # a TeX maths font with no ToUnicode map (decoded through its built-in encoding)
    "CMMI": "<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+CMMI10 >>",
    # a custom maths font whose ToUnicode map sends glyphs to the private use area / U+FFFD
    "Garble": "<< /Type /Font /Subtype /Type1 /BaseFont /XYZMathPi /Encoding /WinAnsiEncoding /ToUnicode {tu} 0 R >>",
}


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(path: Path, pages: list[list[tuple[str, str | bytes]]]) -> Path:
    """pages: per page a list of (font key, text) runs, one line each.
    str text is encoded cp1252; bytes are used as raw character codes."""
    objs: list[bytes] = []

    def add(b: bytes | str) -> int:
        objs.append(b.encode("latin-1") if isinstance(b, str) else b)
        return len(objs)

    tu = add(b"<< /Length %d >>\nstream\n" % len(TOUNICODE_PUA) + TOUNICODE_PUA + b"\nendstream")
    font_refs = {k: add(v.replace("{tu}", str(tu))) for k, v in FONTS.items()}
    pages_id = len(objs) + 1 + 2 * len(pages)     # reserve: content + page per input page, then /Pages
    kids = []
    for runs in pages:
        stream = b"BT\n"
        y = 760
        for font, text in runs:
            raw = text if isinstance(text, bytes) else text.encode("cp1252")
            stream += b"/%s 10 Tf 1 0 0 1 50 %d Tm (%s) Tj\n" % (font.encode(), y, _esc(raw))
            y -= 14
        stream += b"ET"
        c = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        fres = " ".join(f"/{k} {font_refs[k]} 0 R" for k in dict.fromkeys(f for f, _ in runs))
        kids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                        f"/Resources << /Font << {fres} >> >> /Contents {c} 0 R >>"))
    assert add(f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>") == pages_id
    cat = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
    body = b"%PDF-1.4\n"
    for i, o in enumerate(objs, 1):
        body += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    body += b"trailer\n<< /Root %d 0 R /Size %d >>\n%%%%EOF\n" % (cat, len(objs) + 1)
    # no xref table on purpose: MuPDF rebuilds it, and saving writes a clean file
    with pymupdf.open("pdf", body) as d:
        d.save(str(path))
    return path


GARBLED_PROSE = [
    "Let the lines ” and “! meet at O, and let the lines “ and ”! be given.",
    "Let A be represented by the vector a and A! by a! . Then B is represented by",
    "λa and C by μa. The point P is represented by a vector of the form",
    "ªˆ + (1 − ˜) ªa! , which says that it is on BC! and similarly on CB!.",
    "Hence the lines AA!, BB! and CC! meet at a point, as ”! claimed.",
]
GARBLED_PROSE = [ln.replace("λ", "l").replace("μ", "m").replace("−", "-") for ln in GARBLED_PROSE]

CLEAN_PROSE = [
    "Let the lines l and m meet at O. Let A be represented by the vector a.",
    "Then B is represented by a multiple of a, and C by another multiple of a.",
    "The point P lies on the line BC, which proves the claim. This is remarkable!",
    "Hence the three lines meet at a point, as Desargues claimed in 1639.",
    "We call such a configuration “perspective” from the point O.",
]
