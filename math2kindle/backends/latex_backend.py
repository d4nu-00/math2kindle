"""LaTeX source -> ConvertedDoc (best quality: exact TeX, no OCR)."""

from __future__ import annotations

import re
from pathlib import Path

from ..document import ConvertedDoc, MathRef
from ..htmltree import E, add_class, has_class, parse_fragment, replace_tokens, unwrap
from ..latex.bbl import parse_bbl, parse_thebibliography
from ..latex.converter import TOKEN_RE, LatexConverter
from ..latex.macros import MacroSet, harvest
from ..latex.project import document_class, find_main_tex, flatten, read_bbl_for, split_preamble
from ..latex.texscan import match_brace, read_arg, read_opt
from ..mathrender import PACKAGE_DENYLIST, LatexPreamble, LatexSnippetRenderer
from ..pandoc import latex_to_html
from ..util import log, run, tex_to_plain, which


def _find_cmd_args(text: str, cmd: str) -> list[str]:
    out = []
    for m in re.finditer(r"\\" + cmd + r"(?![A-Za-z@])\s*\*?", text):
        _, j = read_opt(text, m.end())
        a, _ = read_arg(text, j)
        if a is not None:
            out.append(a)
    return out


def extract_title_authors(preamble: str, body: str) -> tuple[str | None, list[str]]:
    text = preamble + "\n" + body
    titles = _find_cmd_args(text, "title")
    title = titles[0] if titles else None
    if title is not None:
        title = re.sub(r"\\\\(\[[^\]]*\])?", " ", title)
        title = re.sub(r"\\(thanks|footnote|tnoteref|tnotetext)\s*\{", "\\\\m2kdrop{", title)
        title = _drop_m2k(title)
    authors: list[str] = []
    for a in _find_cmd_args(text, "author"):
        a = re.sub(r"\\(thanks|footnote|inst|IEEEauthorrefmark|orcidlink|orcid|email|address|affil|fnref|"
                   r"corref|textsuperscript)\s*\{", "\\\\m2kdrop{", a)
        a = _drop_m2k(a)
        a = re.sub(r"\$\^\{?[^$]*\}?\$", "", a)
        for part in re.split(r"\\and\b|\\And\b|\\AND\b|,\s*and\s+|\s+and\s+|\\\\", a):
            p = tex_to_plain(part).strip(" ,;")
            if p and len(p) < 80 and p not in authors:
                authors.append(p)
    return title, authors


def _drop_m2k(s: str) -> str:
    out = []
    pos = 0
    for m in re.finditer(r"\\m2kdrop\{", s):
        if m.start() < pos:
            continue
        out.append(s[pos:m.start()])
        pos = match_brace(s, m.end() - 1) + 1
    out.append(s[pos:])
    return "".join(out)


def _available_packages(names: list[str], root: Path) -> set[str]:
    kpse = which("kpsewhich")
    found = {n for n in names if (root / f"{n}.sty").exists()}
    if not kpse:
        return found | set(names)
    todo = [n for n in names if n not in found]
    if todo:
        proc = run([kpse] + [f"{n}.sty" for n in todo], timeout=60)
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            found.add(Path(line.strip()).stem)
    return found


def build_snippet_preamble(ms: MacroSet, root: Path, local_styles: list[Path]) -> LatexPreamble:
    pkgs: list[str] = []
    names_needed = []
    parsed = []
    for line in ms.usepackages:
        m = re.match(r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}", line, re.S)
        if not m:
            continue
        names = [n.strip() for n in m.group(2).split(",") if n.strip()]
        parsed.append((m.group(1) or "", names))
        names_needed += names
    avail = _available_packages(sorted(set(names_needed)), root)
    for opt, names in parsed:
        keep = [n for n in names if n in avail and n not in PACKAGE_DENYLIST]
        if keep:
            pkgs.append("\\usepackage%s{%s}" % (opt if len(keep) == len(names) else "", ",".join(keep)))
    defs = []
    for d in ms.raw_defs:
        d = d[1:] if d.startswith("%") else d
        if "@" in d:
            d = "\\makeatletter\n" + d + "\n\\makeatother"
        defs.append(d)
    defs = ["\\providecommand{\\qedhere}{}"] + defs
    return LatexPreamble(packages=pkgs, definitions=defs, extra=list(ms.tikz_extra),
                         support_files=list(local_styles))


class LatexBackend:
    name = "latex"

    def __init__(self, *, em_px: int = 48, width_em: float = 16.0):
        self.em_px = em_px
        self.width_em = width_em

    def convert(self, src: Path, work: Path) -> ConvertedDoc:
        src = Path(src)
        if src.is_dir():
            root = src
            main = find_main_tex(root)
        else:
            root = src.parent
            main = src
        flat = flatten(main, root)
        preamble, body = split_preamble(flat.text)
        cls, cls_opts = document_class(preamble)
        ms = MacroSet()
        style = ["plain"]
        for sty in flat.local_styles:
            try:
                harvest(sty.read_text("utf-8", "replace"), ms, theorem_style=style)
            except OSError:
                pass
        harvest(preamble, ms, theorem_style=style)
        if cls in ("amsart", "amsproc", "amsbook") and "equation" not in ms.numberwithin:
            pass  # AMS classes number equations globally unless \numberwithin is used
        has_chapters = bool(re.search(r"\\chapter\*?\s*[\[{]", body)) and cls in (
            "book", "report", "amsbook", "memoir", "scrbook", "scrreprt")
        title_tex, authors = extract_title_authors(preamble, body)

        # bibliography: inline thebibliography wins, else .bbl next to the main file
        bbl_text = read_bbl_for(main, root)
        bib = []
        if "\\begin{thebibliography}" in body:
            bib = parse_thebibliography(body)
        elif bbl_text:
            bib = parse_bbl(bbl_text)
            log.info("Bibliography: %d entries from .bbl", len(bib))
        snippet_pre = build_snippet_preamble(ms, root, flat.local_styles)
        snippets = LatexSnippetRenderer(snippet_pre, em_px=self.em_px, width_em=self.width_em)
        conv = LatexConverter(ms, root=root, work_dir=work, bib=bib if "\\begin{thebibliography}" not in body else [],
                              has_chapters=has_chapters, snippet_renderer=snippets, bbl_text=bbl_text)
        title_marked = ""
        if title_tex:
            title_marked = "\\begin{m2ktitle}\n%s\n\\end{m2ktitle}\n" % conv._process(title_tex)
        marked = conv.convert_body(body)
        html = latex_to_html(title_marked + marked)
        root_el = parse_fragment(html)
        self._postprocess(root_el, conv)

        doc = ConvertedDoc(
            title=tex_to_plain(title_tex) if title_tex else main.stem,
            authors=authors, body=root_el, backend="latex",
            math={k: MathRef(v.tex, v.display, v.anchor) for k, v in conv.st.math.items()},
            images=dict(conv.st.images), mathjax_macros=ms.mathjax_defs(), latex_preamble=snippet_pre,
            warnings=list(conv.st.warnings),
        )
        return doc

    # ------------------------------------------------------------------ HTML fix-ups
    def _postprocess(self, root, conv: LatexConverter) -> None:
        st = conv.st

        # section numbers + heading ids
        for h in root.iter("h1", "h2", "h3", "h4", "h5", "h6"):
            txt = h.text or ""
            m = TOKEN_RE.search(txt)
            if m and m.group(1) == "SN":
                number, ident = st.secnums.get(m.group(0), ("", None))
                h.text = txt[:m.start()] + txt[m.end():].lstrip()
                if ident:
                    h.set("id", ident)
                if h.get("class"):
                    del h.attrib["class"]
                if number:
                    span = E("span", {"class": "secnum"}, number)
                    span.tail = " " + (h.text or "")
                    h.text = None
                    h.insert(0, span)

        def make(m):
            kind, tok = m.group(1), m.group(0)
            if kind == "ANC":
                ident = st.anchors.get(tok, "a" + tok)
                if ident in st.item_labels:
                    sp = E("span", {"id": ident, "class": "itemlabel"}, st.item_labels[ident])
                    sp.tail = "\u00a0"
                    return sp
                return E("span", {"id": ident})
            if kind == "SN":
                return ""
            if kind == "REF":
                return self._ref(conv, tok)
            if kind == "CIT":
                return self._cite(conv, tok)
            if kind == "BL":
                return E("span", {"class": "biblabel"}, "[%s]" % st.biblabels.get(tok, "?"))
            return None   # maths tokens: handled by the common pipeline

        replace_tokens(root, TOKEN_RE, make)

        # environments -> semantic classes
        for div in list(root.iter("div")):
            cls = div.get("class") or ""
            if cls.startswith("m2kthm"):
                style = cls[len("m2kthm"):] or "plain"
                div.set("class", "thm thm-%s" % style)
                strong = next(iter(div.iter("strong")), None)
                if strong is not None:
                    add_class(strong, "thm-name")
                # anchor span -> id on the div
                sp = next((s for s in div.iter("span") if s.get("id") and not len(s) and not s.text), None)
                if sp is not None and div.get("id") is None:
                    div.set("id", sp.get("id"))
                    unwrap_empty(sp)
            elif cls == "m2kproof":
                div.set("class", "proof")
                em = next(iter(div.iter("em")), None)
                if em is not None:
                    add_class(em, "proof-name")
                div.set("data-qed", "1")
            elif cls == "m2kabstract":
                div.set("class", "abstract")
                head = E("p", {"class": "abstract-head"}, None, E("strong", None, "Abstract"))
                div.insert(0, head)
            elif cls == "m2kbib":
                div.set("class", "bibliography")
            elif cls == "m2kbibitem":
                ps = [c for c in div if c.tag == "p"]
                if ps:
                    for p in ps:
                        add_class(p, "bibitem")
                    sp = next((s for s in ps[0].iter("span") if s.get("id") and not s.text and not len(s)), None)
                    if sp is not None:
                        ps[0].set("id", sp.get("id"))
                        unwrap_empty(sp)
                unwrap(div)
            elif cls == "m2ktitle":
                div.set("class", "m2k-title-source")

        # enumerate lists with explicit labels such as (i), (ii): print the labels ourselves
        for sp in list(root.iter("span")):
            if has_class(sp, "itemlabel"):
                li = next((a for a in sp.iterancestors() if a.tag == "li"), None)
                if li is not None:
                    ol = li.getparent()
                    if ol is not None and ol.tag == "ol":
                        add_class(ol, "custom-labels")
                    par = sp.getparent()
                    if par is not None and par.tag == "p":
                        add_class(par, "itemfirst")
                    elif par is li:
                        add_class(li, "itemfirst-li")

        # figure / table anchors -> ids
        for fig in root.iter("figure", "table"):
            sp = next((s for s in fig.iter("span") if s.get("id") and not s.text and not len(s)), None)
            if sp is not None and fig.get("id") is None:
                fig.set("id", sp.get("id"))
                unwrap_empty(sp)

    def _ref(self, conv: LatexConverter, tok: str):
        cmd, keys, text = conv.st.refs[tok]
        labels = conv.st.labels
        out = []
        for idx, key in enumerate(keys):
            t = labels.get(key)
            if t is None:
                conv.warn(f"Undefined reference: {key}")
                out.append("??")
                continue
            num = t.number or "?"
            if text is not None:
                label_text = None
            elif cmd == "eqref" or (t.kind == "equation" and cmd in ("cref", "Cref", "autoref", "Autoref")):
                label_text = "(%s)" % num
                if cmd in ("cref",):
                    label_text = ("eq. " if idx == 0 else "") + label_text
                elif cmd in ("Cref", "autoref", "Autoref"):
                    label_text = ("Equation " if idx == 0 else "") + label_text
            elif cmd in ("cref", "Cref", "autoref", "Autoref", "vref", "Vref"):
                name = t.name or t.kind.capitalize()
                if cmd == "cref" and t.kind in ("section", "figure", "table", "equation"):
                    name = {"section": "Section", "figure": "Figure", "table": "Table"}.get(t.kind, name)
                label_text = ("%s %s" % (name, num)) if (idx == 0 or labels.get(keys[idx - 1]) is None or
                                                          labels[keys[idx - 1]].name != t.name) else num
            elif cmd in ("pageref", "cpageref"):
                label_text = "p.\u00a0" + num
            elif cmd in ("nameref",):
                label_text = num
            else:
                label_text = num
            a = E("a", {"href": "#" + t.anchor} if t.anchor else {}, label_text)
            if text is not None:
                frag = parse_fragment("<span>%s</span>" % text)
                a.text = (frag.text_content() or "").strip() or num
            out.append(a)
        if not out:
            return ""
        seq = []
        for i, piece in enumerate(out):
            if i:
                seq.append(" and " if i == len(out) - 1 and cmd in ("cref", "Cref") else ", ")
            seq.append(piece)
        return seq

    def _cite(self, conv: LatexConverter, tok: str):
        cmd, keys, pre, post = conv.st.cites[tok]
        labels = conv.st.labels
        textual = cmd in ("citet", "Citet", "textcite", "Textcite", "citealt")
        if cmd in ("citeauthor", "citeyear", "citeyearpar"):
            vals = []
            for key in keys:
                b = conv.bib_info.get(key)
                if b is None:
                    vals.append(key)
                else:
                    vals.append(b.author if cmd == "citeauthor" else b.year or b.label)
            return ", ".join(v for v in vals if v)
        seq: list = []
        for i, key in enumerate(keys):
            t = labels.get("bib::" + key)
            b = conv.bib_info.get(key)
            if t is None:
                conv.warn(f"Citation key not found in bibliography: {key}")
                link = key
            else:
                link = E("a", {"href": "#" + t.anchor, "class": "cite"}, t.number)
            if textual:
                if i:
                    seq.append(", ")
                if b is not None and b.author:
                    seq.append(tex_to_plain(b.author) + " ")
                seq += ["[", link, "]"]
            else:
                if i:
                    seq.append(", ")
                seq.append(link)
        pre = _note_text(pre) if pre else pre
        post = _note_text(post) if post else post
        if not textual:
            seq.insert(0, "[" + ((pre + " ") if pre else ""))
            seq.append((", " + post if post else "") + "]")
        elif post:
            seq.append(" (" + post + ")")
        return seq


def _note_text(s: str) -> str:
    """Citation pre/post notes ("Thm.~3.12", "\\S 17") -> text, keeping maths tokens."""
    s = s.replace("~", "\u00a0").replace("---", "\u2014").replace("--", "\u2013")
    s = re.sub(r"\\S\s*", "\u00a7\u00a0", s)
    s = re.sub(r"\\P\s*", "\u00b6\u00a0", s)
    s = re.sub(r"\\(?:emph|textit|textbf|textrm|text)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\[,;:! ]", " ", s)
    s = re.sub(r"\\[A-Za-z]+\*?", "", s)
    return s.replace("{", "").replace("}", "").strip()


def unwrap_empty(sp) -> None:
    """Remove an empty <span>, keeping its tail."""
    parent = sp.getparent()
    if parent is None:
        return
    tail = sp.tail or ""
    prev = sp.getprevious()
    if prev is not None:
        prev.tail = (prev.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(sp)


__all__ = ["LatexBackend", "extract_title_authors", "has_class"]
