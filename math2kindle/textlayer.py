"""Is a PDF's embedded text layer usable for maths?

marker (without ``force_ocr``) reuses a PDF's text layer for every block it
does not classify as an equation.  Many publisher PDFs (Springer, older TeX
workflows with Type 3 / custom-encoded maths fonts) have a text layer whose
maths decodes to junk: primes become "!", Greek line names become curly
quotes, super/subscripts become modifier letters or private-use code points.
These helpers sample a few pages with PyMuPDF and look for those symptoms, so
the CLI can say up front (and again after conversion) when the maths in an
EPUB is probably garbled.

No ML here: everything is cheap string and font-dictionary inspection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Font names of TeX / AMS / common maths fonts.  A maths font WITHOUT a
# ToUnicode map is decoded through its (often custom) built-in encoding, which
# is exactly how "!" for a prime comes about.
MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|CMBSY|CMMIB|MSAM|MSBM|EUFM|EUSM|EUEX|RSFS|MTMI|MTSY|MTEX|Mathematical|MathPi|"
    r"Math|Symbol|STIX|Euclid|LMMath|LatinModernMath|TXSY|TXMI|PXSY|PXMI|Cambria\s*Math)",
    re.I)

# Code points that do not belong in decoded running text.  U+F8E5-F8FF is
# left out: Adobe maps extensible delimiter pieces (big braces/brackets from
# CMEX, Symbol) there, and those appear in perfectly healthy display maths.
_PUA = ((0xE000, 0xF8E4), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))
# Modifier letters (ˢ ᴴ ...) and ordinal indicators (ª º) - what mangled
# super/subscripts typically decode to.
_ODD = ((0x02B0, 0x02FF), (0x1D00, 0x1DBF))
_ODD_CHARS = "\u00aa\u00ba"

# "A!" / "a! ." / "\u201c!" : an exclamation mark glued to a letter, quote or
# modifier letter and followed by more of the sentence (lower case,
# punctuation, an ellipsis or another symbol), i.e. not a genuine
# sentence-ending "!".
_BANG_RE = re.compile("[A-Za-z\u00aa\u00ba\u02b0-\u02ff\u0370-\u03ff\u1d00-\u1dbf\u201c\u201d\u2018\u2019]!"
                      "(?=\\s*(?:[a-z,.;:)\\]!\u2026]|$)|[A-Za-z])", re.M)
# curly quotes in places prose never puts them: on their own between spaces,
# an opening quote straight after a letter, a closing one straight before a
# letter, or a quote followed by "!".
_QUOTE_RE = re.compile("(?:(?<=\\s)[\u201c\u201d](?=\\s)|[A-Za-z]\u201c|\u201d[A-Za-z]|[\u201c\u201d]!)")

DEFAULT_SAMPLE_PAGES = 6


def _in(o: int, ranges) -> bool:
    return any(a <= o <= b for a, b in ranges)


@dataclass
class TextLayerReport:
    pages_sampled: int = 0
    chars: int = 0                      # non-space characters sampled
    pua: int = 0                        # private-use code points
    fffd: int = 0                       # U+FFFD replacement characters
    bang: int = 0                       # "!" glued to letters / quotes
    quotes: int = 0                     # curly quotes in impossible positions
    odd_glyphs: int = 0                 # modifier letters / ordinal indicators
    math_fonts: list[str] = field(default_factory=list)
    math_fonts_no_tounicode: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    error: str | None = None

    # -- derived -----------------------------------------------------------------
    @property
    def garble_marks(self) -> int:
        return self.bang + self.quotes + self.odd_glyphs

    @property
    def garble_rate(self) -> float:
        """Garble marks per 1000 non-space characters."""
        return 1000.0 * self.garble_marks / self.chars if self.chars else 0.0

    @property
    def has_text(self) -> bool:
        return self.chars > 0

    def reasons(self) -> list[str]:
        r = []
        if self.pua >= 3:
            r.append(f"{self.pua} private-use-area glyphs")
        if self.fffd >= 3:
            r.append(f"{self.fffd} U+FFFD replacement characters")
        if self.garble_marks >= 5 and self.garble_rate >= 1.0:
            parts = []
            if self.bang:
                parts.append(f"{self.bang} '!' glued to letters (mangled primes?)")
            if self.quotes:
                parts.append(f"{self.quotes} stray curly quotes (mangled Greek?)")
            if self.odd_glyphs:
                parts.append(f"{self.odd_glyphs} modifier-letter glyphs (mangled sub/superscripts?)")
            r.append(", ".join(parts) + f" = {self.garble_rate:.1f} per 1000 chars")
        return r

    @property
    def broken(self) -> bool:
        """Strong evidence that maths in the text layer decodes to junk."""
        return bool(self.reasons())

    @property
    def suspicious(self) -> bool:
        """Weaker evidence: maths fonts that carry no ToUnicode map."""
        return bool(self.math_fonts_no_tounicode)

    def summary(self) -> str:
        if self.error:
            return f"text layer not inspected ({self.error})"
        if not self.has_text:
            return f"no text layer on the {self.pages_sampled} sampled page(s) (scanned PDF?)"
        bits = [f"{self.pages_sampled} page(s), {self.chars} chars sampled"]
        bits += self.reasons() or ["no garbled-maths symptoms"]
        if self.math_fonts_no_tounicode:
            bits.append("maths fonts without ToUnicode: " + ", ".join(self.math_fonts_no_tounicode[:6]))
        return "; ".join(bits)


def _sample_pages(n_pages: int, k: int) -> list[int]:
    if n_pages <= k:
        return list(range(n_pages))
    # spread over the document, skipping the very first page (often a title page)
    step = (n_pages - 1) / k
    return sorted({min(n_pages - 1, int(1 + i * step)) for i in range(k)})


def _font_name(basefont: str) -> str:
    return basefont.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", basefont or "") else (basefont or "")


def score_text(text: str, report: TextLayerReport | None = None) -> TextLayerReport:
    """Count garbled-maths symptoms in already-extracted text."""
    rep = report or TextLayerReport()
    for ch in text:
        if ch.isspace():
            continue
        rep.chars += 1
        o = ord(ch)
        if o == 0xFFFD:
            rep.fffd += 1
        elif _in(o, _PUA):
            rep.pua += 1
        elif _in(o, _ODD) or ch in _ODD_CHARS:
            rep.odd_glyphs += 1
    for rx, attr in ((_BANG_RE, "bang"), (_QUOTE_RE, "quotes")):
        hits = list(rx.finditer(text))
        setattr(rep, attr, getattr(rep, attr) + len(hits))
        for m in hits[:2]:
            if len(rep.examples) < 4:
                a, b = max(0, m.start() - 25), min(len(text), m.end() + 25)
                rep.examples.append(re.sub(r"\s+", " ", text[a:b]).strip())
    return rep


def inspect_text_layer(pdf: Path | str, max_pages: int = DEFAULT_SAMPLE_PAGES) -> TextLayerReport:
    """Sample up to ``max_pages`` pages of ``pdf`` and report garbled-maths symptoms."""
    rep = TextLayerReport()
    try:
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            seen_fonts: set[int] = set()
            for pno in _sample_pages(len(d), max_pages):
                page = d[pno]
                rep.pages_sampled += 1
                score_text(page.get_text("text"), rep)
                for xref, _ext, _ftype, basefont, *_rest in page.get_fonts(full=True):
                    if xref in seen_fonts:
                        continue
                    seen_fonts.add(xref)
                    name = _font_name(basefont)
                    if not MATH_FONT_RE.search(name):
                        continue
                    if name not in rep.math_fonts:
                        rep.math_fonts.append(name)
                    try:
                        kind, _val = d.xref_get_key(xref, "ToUnicode")
                    except Exception:
                        kind = "null"
                    if kind in ("null", "") and name not in rep.math_fonts_no_tounicode:
                        rep.math_fonts_no_tounicode.append(name)
    except Exception as e:  # unreadable / encrypted PDF: say so, never fail the run
        rep.error = f"{type(e).__name__}: {e}"
    return rep


# ---------------------------------------------------------------------------- after conversion

_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE_RE = re.compile(r"(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$(?!\$)")


def count_formulas(md: str) -> tuple[int, int]:
    """(inline, display) maths counts in marker Markdown."""
    n_disp = len(_DISPLAY_RE.findall(md))
    rest = _DISPLAY_RE.sub(" ", md)
    n_inl = len(_INLINE_RE.findall(rest))
    return n_inl, n_disp


@dataclass
class OutputCheck:
    inline: int
    display: int
    words: int
    text: TextLayerReport

    @property
    def formulas(self) -> int:
        return self.inline + self.display

    @property
    def garbled(self) -> bool:
        """Few formulas for the amount of text, but maths-like junk in the prose."""
        few = self.formulas < max(5, self.words // 250)   # under ~1 formula per 250 words
        return few and self.text.broken


def check_marker_output(md: str) -> OutputCheck:
    """Post-conversion sanity check on marker's Markdown (maths removed before scoring)."""
    n_inl, n_disp = count_formulas(md)
    prose = _INLINE_RE.sub(" ", _DISPLAY_RE.sub(" ", md))
    prose = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", prose)       # Markdown images: ![alt](src)
    prose = re.sub(r"<[^>]+>", " ", prose)                     # stray HTML tags
    return OutputCheck(inline=n_inl, display=n_disp, words=len(prose.split()), text=score_text(prose))


__all__ = ["TextLayerReport", "inspect_text_layer", "score_text", "check_marker_output", "count_formulas",
           "OutputCheck", "MATH_FONT_RE"]
