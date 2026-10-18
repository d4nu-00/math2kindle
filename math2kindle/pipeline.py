"""Backend-independent part: maths -> images, structure, chapters, EPUB."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .document import ConvertedDoc
from .epub import Chapter, EpubBook, TocEntry, default_css, write_epub
from .htmltree import E, INLINE_TAGS, add_class, has_class, remove_keep_tail, replace_tokens, text_of
from .latex.converter import TOKEN_RE
from .mathrender import MathRenderer, RenderedMath, RenderOptions
from .util import log, tex_to_plain

MATH_TOKEN_RE = re.compile(r"M2K(MI|MD)\d{5}X")
BLOCK_PARENTS = {"div", "li", "td", "th", "dd", "blockquote", "figure", "figcaption", "aside", "section", "body"}


@dataclass
class BuildResult:
    epub: Path
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _mid(tex: str, display: bool) -> str:
    return "m" + hashlib.sha1(("D" if display else "I").encode() + tex.encode("utf-8")).hexdigest()[:14]


def _fmt(x: float) -> str:
    return ("%.3f" % x).rstrip("0").rstrip(".")


class Builder:
    def __init__(self, doc: ConvertedDoc, work: Path, render_opts: RenderOptions | None = None, *,
                 justify: bool = False):
        self.justify = justify
        self.doc = doc
        self.work = Path(work)
        self.opt = render_opts or RenderOptions()
        self.math_dir = self.work / "math"
        self.assets: dict[str, Path] = {}       # epub path -> file
        self.rendered: dict[str, RenderedMath] = {}

    # ------------------------------------------------------------------ maths
    def collect_math(self) -> dict[str, tuple[str, bool]]:
        items: dict[str, tuple[str, bool]] = {}
        for tok, m in self.doc.math.items():
            items[_mid(m.tex, m.display)] = (m.tex, m.display)
        for span in self.doc.body.iter("span"):
            if has_class(span, "math"):
                tex, display = self._span_tex(span)
                if tex:
                    items[_mid(tex, display)] = (tex, display)
        return items

    @staticmethod
    def _span_tex(span) -> tuple[str, bool]:
        display = has_class(span, "display")
        t = span.text_content() or ""
        t = t.strip()
        if t.startswith("\\(") and t.endswith("\\)"):
            t = t[2:-2]
        elif t.startswith("\\[") and t.endswith("\\]"):
            t = t[2:-2]
        return t.strip(), display

    def prepare_tables(self) -> None:
        """Inline maths in table cells: use display-size fractions (\\dfrac) so
        numerators/denominators are not tiny."""
        def dfrac(t: str) -> str:
            return re.sub(r"\\(frac|binom)(?![A-Za-z])", r"\\d\1", t)

        for cell in self.doc.body.iter("td", "th"):
            for el in cell.iter():
                if not isinstance(el.tag, str):
                    continue
                for txt in (el.text, el.tail if el is not cell else None):
                    for m in MATH_TOKEN_RE.finditer(txt or ""):
                        ref = self.doc.math.get(m.group(0))
                        if ref is not None and not ref.display:
                            ref.tex = dfrac(ref.tex)
                if el.tag == "span" and has_class(el, "math") and not has_class(el, "display"):
                    if el.text:
                        el.text = dfrac(el.text)

    def render_math(self) -> None:
        self.prepare_tables()
        items = self.collect_math()
        log.info("Rendering %d distinct formulas with MathJax...", len(items))
        renderer = MathRenderer(self.opt, macros=self.doc.mathjax_macros, latex_preamble=self.doc.latex_preamble,
                                out_dir=self.math_dir)
        self.rendered = renderer.render_all(items)
        self.stats = dict(renderer.stats)
        log.info("Maths: %s", ", ".join(f"{k}={v}" for k, v in renderer.stats.items()))

    def _img(self, piece, tex: str, cls: str, display: bool):
        path = f"images/m/{piece.file.name}"
        self.assets[path] = piece.file
        style = []
        if display:
            style.append(f"width:{_fmt(piece.w_em)}em")
        else:
            style += [f"width:{_fmt(piece.w_em)}em", f"height:{_fmt(piece.h_em)}em",
                      f"vertical-align:{_fmt(piece.va_em)}em"]
        return E("img", {"src": path, "alt": tex, "class": cls, "style": ";".join(style),
                         "data-plain": tex_to_plain("$" + tex + "$")[:200]})

    def _display_block(self, b, tex: str, group_em: float | None = None):
        """One display line/row.  Inside a group (group_em = width of the whole
        display in em) sizes are percentages of the group, so when the page is
        narrower than the group every piece is scaled by the same factor."""
        cls = "eq" + {"left": " eq-left", "right": " eq-right"}.get(b.align, "")
        div = E("div", {"class": cls})
        img = self._img(b.piece, b.alt or tex, "md", True)
        if group_em:
            # margin-left is a % of the group's width; the image's % width is
            # relative to what remains of the line after that margin
            room = max(group_em - b.indent, 1e-6)
            img.set("style", "width:%s%%" % _fmt(min(100.0, 100.0 * b.piece.w_em / room)))
            if b.indent:
                div.set("style", "margin-left:%s%%" % _fmt(100.0 * b.indent / group_em))
        elif b.indent:
            div.set("style", "margin-left:%sem" % _fmt(b.indent))
        num = None
        if b.tag:
            mt = max(0.0, min(0.6, (b.piece.h_em - 1.45) / 2))
            num = E("span", {"class": "eqno"}, b.tag)
            if mt > 0.05:
                num.set("style", "margin-top:%sem" % _fmt(mt))
        div.append(img)
        if num is not None:
            div.append(num)
            div.append(E("span", {"class": "eqclear"}))
        return div

    def _math_nodes(self, tex: str, display: bool, anchor: str | None = None):
        r = self.rendered.get(_mid(tex, display))
        if display:
            if r is None or not (r.blocks or r.pieces):
                div = E("div", {"class": "eq"})
                div.append(E("pre", {"class": "tex"}, tex))
                blocks_html = [div]
            else:
                from .mathrender import Block

                blocks = r.blocks or [Block(p, None, "center", 0.0, tex) for p in r.pieces]
                if len(blocks) == 1:
                    blocks_html = [self._display_block(blocks[0], tex)]
                else:
                    group_em = max(b.indent + b.piece.w_em for b in blocks)
                    blocks_html = [self._display_block(b, tex, group_em) for b in blocks]
            if len(blocks_html) == 1:
                out = blocks_html[0]
            else:
                out = E("div", {"class": "eqgroup", "style": "width:%sem" % _fmt(group_em)})
                for d in blocks_html:
                    out.append(d)
            if anchor:
                out.set("id", anchor)
            return out
        if r is None or not r.pieces:
            return E("code", {"class": "tex"}, "$%s$" % tex)
        if len(r.pieces) == 1:
            return self._img(r.pieces[0], tex, "mi", False)
        span = E("span", {"class": "mi-group"})
        for i, p in enumerate(r.pieces):
            img = self._img(p, tex if i == 0 else "", "mi", False)
            if i > 0:
                br = r.breaks[i - 1] if i - 1 < len(r.breaks) else "3"
                gap = 0.0 if br in ("0", "newline") else (int(br) + 1) / 18.0
                if gap:
                    img.set("style", img.get("style") + ";margin-left:%sem" % _fmt(gap))
            if i < len(r.pieces) - 1:
                img.tail = "\u200b"      # break opportunity that justification cannot stretch
            span.append(img)
        return span

    def replace_math(self) -> None:
        body = self.doc.body
        # 1. tokens from the LaTeX backend
        def make(m):
            ref = self.doc.math.get(m.group(0))
            if ref is None:
                return None
            node = self._math_nodes(ref.tex, ref.display, ref.anchor)
            if ref.display:
                node.set("data-display-placeholder", "1")
            return node

        replace_tokens(body, MATH_TOKEN_RE, make)
        # 2. pandoc maths spans (marker backend, or macros pandoc expanded)
        for span in list(body.iter("span")):
            if not has_class(span, "math"):
                continue
            tex, display = self._span_tex(span)
            node = self._math_nodes(tex, display) if tex else None
            parent = span.getparent()
            if node is None:
                remove_keep_tail(span)
                continue
            if display:
                node.set("data-display-placeholder", "1")
            tail = span.tail
            parent.insert(parent.index(span), node)
            parent.remove(span)     # lxml moves span.tail away with it ...
            node.tail = tail        # ... so re-attach the following text
        # 3. display blocks must not sit inside <p>: split paragraphs around them
        for div in list(body.iter("div")):
            if div.get("data-display-placeholder"):
                del div.attrib["data-display-placeholder"]
                self._lift_display(div)

    def _lift_display(self, div) -> None:
        parent = div.getparent()
        while parent is not None and parent.tag in INLINE_TAGS and parent.tag != "span":
            # display inside <em>/<strong>/...: hoist out of the inline element
            parent = self._split_inline(parent, div)
        if parent is None:
            return
        if parent.tag in INLINE_TAGS or parent.tag in ("h1", "h2", "h3", "h4", "h5", "h6", "figcaption"):
            # can't split: keep it as an inline-block image line
            div.tag = "span"
            add_class(div, "eq-inline")
            return
        if parent.tag != "p":
            return
        self._split_paragraph(parent, div)

    def _split_inline(self, inline_el, div):
        """Move div out of inline_el (e.g. <em>), splitting inline_el in two."""
        outer = inline_el.getparent()
        idx = list(inline_el).index(div)
        after = copy.copy(inline_el)
        for c in list(after):
            after.remove(c)
        after.text = div.tail
        after.tail = inline_el.tail
        for c in list(inline_el)[idx + 1:]:
            after.append(c)
        inline_el.remove(div)
        div.tail = None
        inline_el.tail = None
        pos = outer.index(inline_el)
        outer.insert(pos + 1, div)
        if (after.text and after.text.strip()) or len(after):
            outer.insert(pos + 2, after)
        else:
            div.tail = after.tail
        return outer

    def _split_paragraph(self, p, div) -> None:
        container = p.getparent()
        idx = list(p).index(div)
        after = E("p")
        for k, v in p.attrib.items():
            after.set(k, v)
        after.text = div.tail
        for c in list(p)[idx + 1:]:
            after.append(c)
        p.remove(div)
        div.tail = None
        pos = container.index(p)
        container.insert(pos + 1, div)
        has_after = (after.text and after.text.strip()) or len(after)
        if has_after:
            add_class(after, "cont")
            if after.text:
                after.text = after.text.lstrip()
            container.insert(pos + 2, after)
            after.tail = p.tail
            p.tail = None
        if not ((p.text and p.text.strip()) or len(p)):
            container.remove(p)

    # ------------------------------------------------------------------ structure
    def keep_hyphenated_together(self) -> None:
        """"$1$-form", "2-dimensional": never break the line after the hyphen."""
        body = self.doc.body
        word = re.compile(r"-[A-Za-z][\w-]*")
        for img in list(body.iter("img", "span")):
            if not (has_class(img, "mi") or has_class(img, "mi-group")):
                continue
            if img.tag == "img" and img.getparent() is not None and has_class(img.getparent(), "mi-group"):
                continue
            m = word.match(img.tail or "")
            if not m:
                continue
            parent = img.getparent()
            span = E("span", {"class": "nobr"})
            rest = img.tail[m.end():]
            parent.insert(parent.index(img), span)
            span.append(img)            # moves img (and its tail) into the span
            img.tail = m.group(0)
            span.tail = rest
        replace_tokens(body, re.compile(r"(?<![\w-])\d+-[A-Za-z][\w-]*"),
                       lambda m: E("span", {"class": "nobr"}, m.group(0)))

    def finish_structure(self) -> None:
        self.keep_hyphenated_together()
        body = self.doc.body
        for k, h in enumerate(body.iter("h1", "h2", "h3"), 1):   # every heading reachable from the TOC
            if not h.get("id"):
                h.set("id", f"h-{k}")
        # proofs end with a tombstone
        for div in body.iter("div"):
            if div.get("data-qed"):
                del div.attrib["data-qed"]
                last = div[-1] if len(div) else None
                qed = E("span", {"class": "qed"}, "∎")
                if last is not None and last.tag == "p":
                    qed.tail = None
                    if len(last):
                        lastc = last[-1]
                        lastc.tail = (lastc.tail or "").rstrip() + "  "
                    else:
                        last.text = (last.text or "").rstrip() + "  "
                    last.append(qed)
                else:
                    div.append(E("p", {"class": "qed-line"}, None, qed))
        # paragraph indentation classes
        for container in body.iter():
            if not isinstance(container.tag, str):
                continue
            prev = None
            for child in container:
                if child.tag == "p" and not has_class(child, "cont"):
                    if prev is None or prev.tag != "p":
                        add_class(child, "noindent")
                prev = child
        # images of figures
        for img in body.iter("img"):
            if not (has_class(img, "mi") or has_class(img, "md")):
                add_class(img, "figure-img")

    def assets_for_figures(self) -> None:
        n = 0
        for img in self.doc.body.iter("img"):
            if has_class(img, "mi") or has_class(img, "md"):
                continue
            src = img.get("src") or ""
            f = self.doc.images.get(src)
            if f is None and Path(src).exists():
                f = Path(src)
            if f is None:
                log.warning("Image not found, dropped: %s", src)
                img.set("src", "")
                continue
            n += 1
            ep = f"images/fig/{Path(f).name}"
            self.assets[ep] = Path(f)
            img.set("src", ep)
            if not img.get("alt"):
                img.set("alt", "Figure")
            st = img.get("style") or ""
            m = re.search(r"width:\s*([\d.]+)%", st)
            if m:
                img.set("style", "width:%s%%" % m.group(1))
            elif img.get("width") and img.get("width").endswith("%"):
                img.set("style", "width:" + img.get("width"))
            for a in ("width", "height"):
                if a in img.attrib:
                    del img.attrib[a]
        for img in list(self.doc.body.iter("img")):
            if img.get("src") == "":
                remove_keep_tail(img)

    # ------------------------------------------------------------------ chapters & footnotes
    def split_chapters(self) -> tuple[list, list]:
        body = self.doc.body
        # footnotes from pandoc
        notes = {}
        for sec in list(body.iter("section", "aside")):
            if "footnotes" in (sec.get("class") or "") or sec.get("id") == "footnotes":
                for li in sec.iter("li"):
                    if li.get("id"):
                        notes[li.get("id")] = li
                sec.getparent().remove(sec)
        # title source block (LaTeX title with maths)
        title_block = None
        for div in list(body.iter("div")):
            if has_class(div, "m2k-title-source"):
                title_block = div
                div.getparent().remove(div)
                break
        # choose split level: h1 if present more than once, else h2
        levels = {}
        for h in body:
            if isinstance(h.tag, str) and re.fullmatch(r"h[1-6]", h.tag):
                levels[h.tag] = levels.get(h.tag, 0) + 1
        split_tag = next((t for t in ("h1", "h2", "h3") if levels.get(t, 0) >= 1), None)
        if split_tag == "h1" and levels.get("h1", 0) == 1 and levels.get("h2", 0) >= 2:
            split_tag = "h2"
        front: list = []
        chapters: list[list] = []
        for child in list(body):
            if isinstance(child.tag, str) and child.tag == split_tag:
                chapters.append([child])
            elif chapters:
                chapters[-1].append(child)
            else:
                front.append(child)
        self.title_block = title_block
        self.split_tag = split_tag
        return front, chapters, notes  # type: ignore[return-value]

    def build(self, out_path: Path, *, toc_depth: int = 2) -> BuildResult:
        self.render_math()
        self.replace_math()
        self.assets_for_figures()
        self.finish_structure()
        front, chapters, notes = self.split_chapters()
        doc = self.doc

        # ---- title page
        tp = E("div", {"class": "title-page"})
        h = E("h1", {"class": "title", "id": "title"})
        if self.title_block is not None and len(self.title_block) and self.title_block[0].tag == "p":
            src_p = self.title_block[0]
            h.text = src_p.text
            for c in list(src_p):
                h.append(c)
        else:
            h.text = doc.title
        tp.append(h)
        if doc.authors:
            tp.append(E("p", {"class": "authors"}, ", ".join(doc.authors)))
        if doc.source:
            p = E("p", {"class": "source"})
            if doc.source_url:
                p.append(E("a", {"href": doc.source_url}, doc.source))
            else:
                p.text = doc.source
            tp.append(p)
        has_abstract = any(has_class(el, "abstract") for el in front for el in el.iter())
        for el in front:
            tp.append(el)
        if not has_abstract and doc.abstract:
            ab = E("div", {"class": "abstract"})
            ab.append(E("p", {"class": "abstract-head"}, None, E("strong", None, "Abstract")))
            ab.append(E("p", {"class": "noindent"}, doc.abstract))
            tp.append(ab)

        book_chapters: list[Chapter] = [Chapter("title.xhtml", doc.title, [tp], linear=True)]
        for i, els in enumerate(chapters, 1):
            head = els[0]
            book_chapters.append(Chapter(f"ch{i:03d}.xhtml", text_of(head) or f"Part {i}", els))

        # ---- footnotes: move each note into the chapter that references it
        for ch in book_chapters:
            refs = []
            for el in ch.elements:
                for a in el.iter("a"):
                    if has_class(a, "footnote-ref") or a.get("role") == "doc-noteref":
                        refs.append(a)
            if not refs:
                continue
            fsec = E("section", {"class": "footnotes"})
            fsec.append(E("hr", {"class": "fn-rule"}))
            for a in refs:
                target = (a.get("href") or "").lstrip("#")
                li = notes.get(target)
                a.set("data-epub-type", "noteref")
                a.set("class", "noteref")
                if "role" in a.attrib:
                    del a.attrib["role"]
                if li is None:
                    continue
                num = (a.text_content() or "").strip() or "*"
                aside = E("aside", {"id": target, "class": "footnote", "data-epub-type": "footnote"})
                for back in list(li.iter("a")):
                    if has_class(back, "footnote-back") or back.get("role") == "doc-backlink":
                        par = back.getparent()
                        if back.tail:
                            prev = back.getprevious()
                            if prev is not None:
                                prev.tail = (prev.tail or "") + back.tail
                            else:
                                par.text = (par.text or "") + back.tail
                        par.remove(back)
                kids = [c for c in li]
                if not kids or kids[0].tag != "p":
                    p = E("p")
                    p.text = li.text
                    for c in kids:
                        p.append(c)
                    kids = [p]
                first = kids[0]
                lab = E("a", {"href": "#" + (a.get("id") or ""), "class": "fn-num"}, num)
                lab.tail = ". " + (first.text or "")
                first.text = None
                first.insert(0, lab)
                for c in kids:
                    aside.append(c)
                fsec.append(aside)
            ch.elements.append(fsec)

        # ---- ids -> files, rewrite internal links
        where: dict[str, str] = {}
        for ch in book_chapters:
            for el in ch.elements:
                for x in el.iter():
                    if isinstance(x.tag, str) and x.get("id"):
                        where.setdefault(x.get("id"), ch.filename)
        for ch in book_chapters:
            for el in ch.elements:
                for a in el.iter("a"):
                    href = a.get("href") or ""
                    if href.startswith("#"):
                        target = href[1:]
                        f = where.get(target)
                        if f is None:
                            a.attrib.pop("href", None)
                        elif f != ch.filename:
                            a.set("href", f"{f}#{target}")

        # ---- TOC
        toc: list[TocEntry] = []
        for ch in book_chapters[1:]:
            top = TocEntry(ch.title, f"text/{ch.filename}")
            toc.append(top)
            if toc_depth >= 2:
                sub_tag = "h%d" % (int(self.split_tag[1]) + 1) if self.split_tag else None
                for el in ch.elements[1:]:
                    if el.tag == sub_tag and el.get("id"):
                        top.children.append(TocEntry(text_of(el), f"text/{ch.filename}#{el.get('id')}"))
        if not toc:
            toc.append(TocEntry(doc.title, "text/title.xhtml"))

        css = default_css()
        if self.justify:
            css = css.replace("text-align: left; /* body */", "text-align: justify; /* body */")
        book = EpubBook(css=css, title=doc.title, authors=doc.authors, language=doc.language, chapters=book_chapters,
                        toc=toc, assets=self.assets, source=doc.source, description=doc.abstract,
                        identifier_seed=(doc.source or doc.title))
        write_epub(book, out_path)
        return BuildResult(epub=out_path, stats=getattr(self, "stats", {}), warnings=list(doc.warnings))
