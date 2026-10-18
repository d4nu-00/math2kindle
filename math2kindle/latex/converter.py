"""LaTeX -> (pandoc-friendly LaTeX with placeholder tokens).

A single left-to-right scan over the flattened document body that

* pulls every maths expression out into a store and leaves an opaque token
  (``M2KMI00012X`` inline / ``M2KMD00013X`` display), so pandoc never has to
  understand the maths and MathJax gets the exact source;
* numbers sections, theorem-like environments, equations (per row for
  align/gather/...), figures and tables the way LaTeX would, and records
  ``\\label`` targets;
* replaces ``\\ref``/``\\eqref``/``\\cref``/``\\cite``... with tokens that are
  resolved to links after pandoc (forward references work);
* expands the author's text macros and simple ``\\newenvironment`` s;
* converts figures to grayscale PNG and tries to render TikZ pictures;
* rewrites theorem/proof environments into marker environments that pandoc
  turns into ``<div class="m2k...">``.

Anything unknown is passed to pandoc unchanged.  The scanner never raises on
odd input; it logs warnings and degrades.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..figures import convert_graphic, find_graphic, width_percent
from ..util import log
from .bbl import BibEntry
from .macros import DEFAULT_THEOREMS, MacroSet, TheoremDef, expand_macro, harvest
from .texscan import (CS_RE, find_env_end, find_math_end, match_brace, read_arg, read_opt, read_star,
                      skip_ws, split_top_level, strip_cmd_with_arg)

TOKEN_RE = re.compile(r"M2K(MI|MD|REF|CIT|ANC|SN|BL)(\d{5})X")

SECTION_LEVELS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3,
                  "paragraph": 4, "subparagraph": 5}
PANDOC_HEADINGS = ["section", "subsection", "subsubsection"]

NUMBERED_MATH = {"equation", "align", "flalign", "alignat", "xalignat", "xxalignat", "gather", "multline",
                 "eqnarray", "dmath", "dgroup"}
UNNUMBERED_MATH = {"displaymath", "equation*", "align*", "flalign*", "alignat*", "gather*", "multline*",
                   "eqnarray*", "dmath*", "math"}
ALIGN_LIKE = {"align", "flalign", "alignat", "xalignat", "xxalignat", "eqnarray"}

PICTURE_ENVS = {"tikzpicture", "tikzcd", "pgfpicture", "picture", "xy", "pspicture", "circuitikz"}

DROP_WITH_ARGS = {
    # command: (n_optional, n_mandatory)
    "title": (1, 1), "author": (1, 1), "date": (0, 1), "thanks": (0, 1), "address": (1, 1),
    "email": (1, 1), "curraddr": (1, 1), "urladdr": (1, 1), "keywords": (0, 1), "subjclass": (1, 1),
    "dedicatory": (0, 1), "affil": (1, 1), "affiliation": (1, 1), "institute": (0, 1), "inst": (0, 1),
    "bibliographystyle": (0, 1), "nocite": (0, 1), "setcounter": (0, 2), "addtocounter": (0, 2),
    "pagestyle": (0, 1), "thispagestyle": (0, 1), "vspace": (0, 1), "addvspace": (0, 1),
    "enlargethispage": (0, 1), "markboth": (0, 2), "markright": (0, 1), "addcontentsline": (0, 3),
    "pagenumbering": (0, 1), "hypersetup": (0, 1), "titlerunning": (0, 1), "authorrunning": (0, 1),
    "orcid": (0, 1), "orcidID": (0, 1), "shorttitle": (0, 1), "shortauthors": (0, 1), "received": (0, 1),
    "accepted": (0, 1), "arxiv": (0, 1), "MSC": (0, 1), "PACS": (0, 1), "copyrightinfo": (0, 2),
    "bibstyle": (0, 1), "renewcommand*": (0, 0), "settowidth": (0, 2), "setlength": (0, 2),
    "addtolength": (0, 2), "linespread": (0, 1), "numberwithin": (1, 2), "theoremstyle": (0, 1),
    "crefname": (0, 3), "Crefname": (0, 3), "crefformat": (0, 2), "newlength": (0, 1),
    "captionsetup": (1, 1), "setlist": (1, 1), "allowdisplaybreaks": (1, 0), "label*": (0, 1),
    "pdfbookmark": (1, 2), "phantomsection": (0, 0), "maketitle": (0, 0), "tableofcontents": (0, 0),
    "listoffigures": (0, 0), "listoftables": (0, 0), "qed": (0, 0), "qedhere": (0, 0), "newpage": (0, 0),
    "clearpage": (0, 0), "cleardoublepage": (0, 0), "pagebreak": (1, 0), "nopagebreak": (1, 0),
    "frontmatter": (0, 0), "mainmatter": (0, 0), "backmatter": (0, 0), "printindex": (0, 0),
    "makeatletter": (0, 0), "makeatother": (0, 0), "normalsize": (0, 0), "sloppy": (0, 0),
    "balance": (0, 0), "FloatBarrier": (0, 0), "IEEEpeerreviewmaketitle": (0, 0),
    "smallskip": (0, 0), "medskip": (0, 0), "bigskip": (0, 0), "vfill": (0, 0), "hfill": (0, 0),
    "linebreak": (1, 0), "index": (0, 1), "glossary": (0, 1), "todo": (1, 1), "marginpar": (1, 1),
    "graphicspath": (0, 1), "DeclareGraphicsExtensions": (0, 1), "tikzset": (0, 1),
    "usetikzlibrary": (0, 1), "pgfplotsset": (0, 1), "hyphenation": (0, 1), "bibliographystyle*": (0, 1),
}

REF_CMDS = {"ref", "eqref", "cref", "Cref", "autoref", "Autoref", "pageref", "cpageref", "nameref", "vref",
            "Vref", "crefrange", "Crefrange", "labelcref", "namecref", "nameCref", "subref", "thmref"}
CITE_CMDS = {"cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar", "Cite",
             "Citep", "Citet", "parencite", "textcite", "autocite", "footcite", "cites", "citenum",
             "smartcite", "Textcite", "Parencite", "Autocite", "citeN", "shortcite", "onlinecite"}


@dataclass
class MathItem:
    tex: str
    display: bool
    anchor: str | None = None


@dataclass
class LabelTarget:
    kind: str           # section|theorem|equation|figure|table|item|bib|other
    number: str
    anchor: str | None
    name: str = ""      # e.g. "Theorem", "Section"


@dataclass
class Frame:
    env: str
    end_text: str
    saved: object = None      # \@currentlabel to restore at \end (environments are groups)
    restore: bool = False


@dataclass
class ConvertState:
    math: dict[str, MathItem] = field(default_factory=dict)
    refs: dict[str, tuple[str, list[str], str | None]] = field(default_factory=dict)   # token -> (cmd, keys, text)
    cites: dict[str, tuple[str, list[str], str | None, str | None]] = field(default_factory=dict)
    anchors: dict[str, str] = field(default_factory=dict)        # token -> id
    secnums: dict[str, tuple[str, str]] = field(default_factory=dict)  # token -> (number, id)
    biblabels: dict[str, str] = field(default_factory=dict)      # token -> label text
    item_labels: dict[str, str] = field(default_factory=dict)    # anchor id -> "(ii)" for custom enumerate labels
    labels: dict[str, LabelTarget] = field(default_factory=dict)
    images: dict[str, Path] = field(default_factory=dict)        # src used in LaTeX -> file
    warnings: list[str] = field(default_factory=list)


def _roman(n: int) -> str:
    vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
            (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _alpha(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s or "a"


class LatexConverter:
    def __init__(self, macros: MacroSet, *, root: Path, work_dir: Path, bib: list[BibEntry] | None = None,
                 has_chapters: bool = False, snippet_renderer=None, bbl_text: str | None = None):
        self.ms = macros
        self.root = Path(root)
        self.work = Path(work_dir)
        self.fig_dir = self.work / "figures"
        self.bib = bib or []
        self.bib_by_key = {b.key: b for b in self.bib}
        self.bib_info = dict(self.bib_by_key)
        self.bbl_text = bbl_text
        self.snippets = snippet_renderer
        self.st = ConvertState()
        self.n = 0
        self.cnt: dict[str, int] = defaultdict(int)
        self.has_chapters = has_chapters
        self.top_level = 0 if has_chapters else 1
        self.appendix = False
        self.parent: dict[str, str | None] = {
            "subsection": "section", "subsubsection": "subsection",
            "section": "chapter" if has_chapters else None,
            "equation": macros.numberwithin.get("equation", "chapter" if has_chapters else None),
            "figure": macros.numberwithin.get("figure", "chapter" if has_chapters else None),
            "table": macros.numberwithin.get("table", "chapter" if has_chapters else None),
        }
        for t in macros.theorems.values():
            if t.counter and t.counter not in self.parent:
                self.parent[t.counter] = t.within or macros.numberwithin.get(t.counter)
        self.current: LabelTarget | None = None
        self.float_kind: str | None = None
        self.frames: list[Frame] = []
        self.enum_stack: list[dict] = []
        self.expansions = 0
        self.fig_n = 0
        self.title_tex: str | None = None
        self.used_bib = False

    # ------------------------------------------------------------------ tokens
    def _tok(self, kind: str) -> str:
        self.n += 1
        return f"M2K{kind}{self.n:05d}X"

    def _anchor(self, prefix: str) -> tuple[str, str]:
        tok = self._tok("ANC")
        ident = f"{prefix}-{self.n}"
        self.st.anchors[tok] = ident
        return tok, ident

    def warn(self, msg: str) -> None:
        if msg not in self.st.warnings:
            self.st.warnings.append(msg)
            log.warning(msg)

    # ------------------------------------------------------------------ counters
    def step(self, c: str) -> None:
        self.cnt[c] += 1
        stack = [c]
        while stack:
            p = stack.pop()
            for child, par in self.parent.items():
                if par == p and self.cnt[child]:
                    self.cnt[child] = 0
                    stack.append(child)

    def the(self, c: str) -> str:
        n = self.cnt[c]
        top = "chapter" if self.has_chapters else "section"
        s = (chr(64 + n) if 0 < n <= 26 else str(n)) if (self.appendix and c == top) else str(n)
        par = self.parent.get(c)
        if par and (par != "chapter" or self.has_chapters):
            return f"{self.the(par)}.{s}"
        return s

    # ------------------------------------------------------------------ public
    def convert_body(self, body: str) -> str:
        if self.bib and "\\begin{thebibliography}" not in body and not re.search(
                r"\\(bibliography|printbibliography)\b", body):
            body = body + "\n\\bibliography{auto}\n"
        out = self._process(body)
        self._finalize_math()
        return out

    # ------------------------------------------------------------------ scanner
    def _process(self, s: str) -> str:
        out: list[str] = []
        i = 0
        special = re.compile(r"[\\$]")
        while i < len(s):
            m = special.search(s, i)
            if not m:
                out.append(s[i:])
                break
            j = m.start()
            if j > i:
                out.append(s[i:j])
            i = j
            if s[i] == "$":
                if s.startswith("$$", i):
                    e = find_math_end(s, i + 2, "$$")
                    if e < 0:
                        self.warn("Unterminated $$ in source; treated as text")
                        i += 2
                        continue
                    out.append(self._display(s[i + 2:e]))
                    i = e + 2
                else:
                    e = find_math_end(s, i + 1, "$")
                    if e < 0:
                        self.warn("Unterminated $ in source; treated as text")
                        out.append("\\$")
                        i += 1
                        continue
                    out.append(self._inline(s[i + 1:e]))
                    i = e + 1
                continue
            cm = CS_RE.match(s, i)
            if not cm:
                out.append("\\")
                i += 1
                continue
            name = cm.group(1)
            j = cm.end()
            if name == "[":
                e = find_math_end(s, j, "\\]")
                if e < 0:
                    out.append(s[i:j])
                    i = j
                    continue
                out.append(self._display(s[j:e]))
                i = e + 2
                continue
            if name == "(":
                e = find_math_end(s, j, "\\)")
                if e < 0:
                    out.append(s[i:j])
                    i = j
                    continue
                out.append(self._inline(s[j:e]))
                i = e + 2
                continue
            res = self._command(name, s, i, j)
            if res is None:
                out.append(s[i:j])
                i = j
                continue
            kind, payload, new_i = res
            if kind == "emit":
                out.append(payload)
                i = new_i
            else:  # "replace": splice expansion into the input and rescan
                s = s[:i] + payload + s[new_i:]
        return "".join(out)

    # ------------------------------------------------------------------ commands
    def _command(self, name: str, s: str, i: int, j: int):
        if name == "begin":
            return self._begin(s, i, j)
        if name == "end":
            env, k = read_arg(s, j)
            env = (env or "").strip()
            for idx in range(len(self.frames) - 1, -1, -1):
                if self.frames[idx].env == env:
                    fr = self.frames.pop(idx)
                    if fr.restore:
                        self.current = fr.saved
                    if env in ("enumerate",) and self.enum_stack:
                        self.enum_stack.pop()
                    if env in ("figure", "figure*", "table", "table*", "wrapfigure", "wraptable"):
                        self.float_kind = None
                    return ("emit", fr.end_text, k)
            return ("emit", s[i:k], k)
        if name in SECTION_LEVELS:
            return self._section(name, s, j)
        if name == "label":
            key, k = read_arg(s, j)
            if key is not None:
                key = key.strip()
                if self.current is not None:
                    self.st.labels[key] = self.current
                else:
                    tok, ident = self._anchor("lbl")
                    self.st.labels[key] = LabelTarget("other", "", ident)
                    return ("emit", tok, k)
            return ("emit", "", k)
        if name in REF_CMDS:
            star, k = read_star(s, j)
            if name in ("crefrange", "Crefrange"):
                a, k = read_arg(s, k)
                b, k = read_arg(s, k)
                keys = [a or "", b or ""]
            else:
                keys_s, k = read_arg(s, k)
                keys = [x.strip() for x in (keys_s or "").split(",") if x.strip()]
            tok = self._tok("REF")
            self.st.refs[tok] = (name, keys, None)
            return ("emit", tok, k)
        if name == "hyperref":
            opt, k = read_opt(s, j)
            text, k = read_arg(s, k)
            if opt:
                tok = self._tok("REF")
                self.st.refs[tok] = ("hyperref", [opt.strip()], self._process(text or ""))
                return ("emit", tok, k)
            return ("emit", self._process(text or ""), k)
        if name in CITE_CMDS:
            return self._cite(name, s, j)
        if name in ("bibliography", "printbibliography"):
            if name == "bibliography":
                _, k = read_arg(s, j)
            else:
                _, k = read_opt(s, j)
            return ("emit", self._bibliography(), k)
        if name == "appendix":
            self.appendix = True
            top = "chapter" if self.has_chapters else "section"
            self.cnt[top] = 0
            return ("emit", "", j)
        if name in ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def", "gdef",
                    "edef", "xdef", "let", "DeclareMathOperator", "DeclarePairedDelimiter",
                    "DeclarePairedDelimiterX", "newenvironment", "renewenvironment", "newtheorem",
                    "declaretheorem", "spnewtheorem", "theoremstyle", "numberwithin"):
            _, spans = harvest(s[i:], self.ms)
            if spans and spans[0][0] == 0:
                for t in self.ms.theorems.values():
                    if t.counter and t.counter not in self.parent:
                        self.parent[t.counter] = t.within
                if "equation" in self.ms.numberwithin:
                    self.parent["equation"] = self.ms.numberwithin["equation"]
                return ("emit", "", i + spans[0][1])
            return None
        if name == "ensuremath":
            a, k = read_arg(s, j)
            return ("emit", self._inline(a or ""), k)
        if name == "texorpdfstring":
            a, k = read_arg(s, j)
            _, k = read_arg(s, k)
            return ("replace", a or "", k)
        if name == "includegraphics":
            return self._includegraphics(s, j)
        if name == "caption":
            return self._caption(s, j)
        if name == "item":
            return self._item(s, j)
        if name == "paragraph" or name == "subparagraph":
            pass  # handled by _section
        if name == "verb":
            k = j
            if k < len(s) and s[k] == "*":
                k += 1
            if k < len(s):
                e = s.find(s[k], k + 1)
                if e > 0:
                    return ("emit", s[i:e + 1], e + 1)
            return None
        if name in DROP_WITH_ARGS:
            nopt, nman = DROP_WITH_ARGS[name]
            k = j
            star, k = read_star(s, k)
            for _ in range(nopt):
                _, k = read_opt(s, k)
            for _ in range(nman):
                a, k2 = read_arg(s, k)
                if a is None:
                    break
                k = k2
            if name == "title" and self.title_tex is None:
                pass
            return ("emit", "", k)
        if name in ("intertext", "shortintertext"):
            a, k = read_arg(s, j)
            return ("emit", self._process(a or ""), k)
        # user text macros
        mac = self.ms.text_expandable(name)
        if mac is not None:
            if self.expansions > 50000:
                if self.expansions == 50001:
                    self.warn("Macro expansion limit reached (recursive macro?); remaining macros left as-is")
                self.expansions += 1
                return None
            r = expand_macro(mac, s, j)
            if r is None:
                return None
            body, k = r
            self.expansions += 1
            return ("replace", body, k)
        return None

    # ------------------------------------------------------------------ environments
    def _begin(self, s: str, i: int, j: int):
        env, k = read_arg(s, j)
        if env is None:
            return None
        env = env.strip()
        base = env.rstrip("*")
        if env in NUMBERED_MATH or env in UNNUMBERED_MATH or base in NUMBERED_MATH:
            arg = ""
            if base in ("alignat", "xalignat", "xxalignat"):
                a, k2 = read_arg(s, k)
                arg = "{%s}" % (a or "1")
                k = k2
            e0, e1 = find_env_end(s, k, env)
            content = s[k:e0]
            if env == "math":
                return ("emit", self._inline(content), e1)
            return ("emit", self._math_env(env, content, arg), e1)
        if env in PICTURE_ENVS:
            e0, e1 = find_env_end(s, k, env)
            return ("emit", self._picture(s[i:e1]), e1)
        thm = self._theorem_def(env)
        if thm is not None:
            return self._theorem(thm, s, k)
        if env == "proof":
            opt, k = read_opt(s, k)
            head = self._process(opt).strip() if opt else "Proof"
            if not re.search(r"[.:]\s*$", head):
                head += "."
            self.frames.append(Frame(env, "\n\\end{m2kproof}\n"))
            return ("emit", "\n\\begin{m2kproof}\n\\textit{%s} " % head, k)
        if env in self.ms.environments:
            nargs, default, begin_code, end_code = self.ms.environments[env]
            args = []
            kk = k
            if default is not None and nargs > 0:
                o, kk = read_opt(s, kk)
                args.append(default if o is None else o)
                nargs -= 1
            for _ in range(nargs):
                a, kk = read_arg(s, kk)
                args.append(a or "")
            e0, e1 = find_env_end(s, kk, env)
            b = re.sub(r"#(\d)", lambda mm: args[int(mm.group(1)) - 1] if 0 < int(mm.group(1)) <= len(args) else "",
                       begin_code)
            if self.expansions > 50000:
                return None
            self.expansions += 1
            return ("replace", b + s[kk:e0] + end_code, e1)
        if env == "abstract":
            self.frames.append(Frame(env, "\n\\end{m2kabstract}\n"))
            return ("emit", "\n\\begin{m2kabstract}\n", k)
        if env == "thebibliography":
            e0, e1 = find_env_end(s, k, env)
            from .bbl import parse_thebibliography

            entries = parse_thebibliography(s[i:e1])
            if entries:
                self.bib_info.update({b.key: b for b in entries})
                return ("emit", self._bib_html(entries), e1)
            return ("emit", "", e1)
        if env in ("figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "SCfigure"):
            self.float_kind = "table" if env.startswith(("table", "wraptable")) else "figure"
            if env.startswith("wrap"):
                _, k = read_opt(s, k)
                _, k = read_arg(s, k)
                _, k = read_arg(s, k)
            else:
                _, k = read_opt(s, k)
            pandoc_env = "table" if self.float_kind == "table" else "figure"
            self.frames.append(Frame(env, "\\end{%s}" % pandoc_env))
            return ("emit", "\\begin{%s}" % pandoc_env, k)
        if env == "enumerate":
            opt, k2 = read_opt(s, k)
            self.enum_stack.append({"n": 0, "fmt": self._enum_format(opt), "depth": len(self.enum_stack),
                                    "custom": opt is not None})
            self.frames.append(Frame(env, "\\end{enumerate}", self.current, True))
            return ("emit", "\\begin{enumerate}", k2)
        if env in ("itemize", "description"):
            opt, k2 = read_opt(s, k)
            self.frames.append(Frame(env, "\\end{%s}" % env))
            return ("emit", "\\begin{%s}" % env, k2)
        if env in ("subequations",):
            self.frames.append(Frame(env, ""))
            return ("emit", "", k)
        if env in ("acknowledgments", "acknowledgements", "acknowledgment", "acknowledgement", "ack"):
            sec = self._section_heading(1, "Acknowledgements", numbered=False)
            self.frames.append(Frame(env, "\n"))
            return ("emit", sec, k)
        if env in ("center", "flushleft", "flushright", "quote", "quotation", "minipage", "small", "footnotesize",
                   "scriptsize", "itshape", "bfseries", "sloppypar", "landscape", "adjustbox", "resizebox",
                   "spacing", "singlespace", "onehalfspace", "doublespace", "multicols"):
            # pass through, but drop minipage width etc.
            if env in ("minipage",):
                _, k = read_opt(s, k)
                _, k = read_arg(s, k)
            if env in ("multicols", "spacing", "adjustbox"):
                _, k = read_arg(s, k)
            self.frames.append(Frame(env, "\n"))
            return ("emit", "\n", k)
        # unknown environment: pass begin through, content is scanned normally
        self.frames.append(Frame(env, "\\end{%s}" % env))
        return ("emit", s[i:k], k)

    def _enum_format(self, opt: str | None) -> str:
        if not opt:
            return ["{n}.", "({a})", "{r}.", "{A}."][min(len(self.enum_stack), 3)]
        m = re.search(r"label\s*=\s*(\{[^}]*\}|[^,]+)", opt)
        tmpl = m.group(1).strip("{}") if m else opt
        tmpl = (tmpl.replace("\\roman*", "{r}").replace("\\Roman*", "{R}").replace("\\alph*", "{a}")
                .replace("\\Alph*", "{A}").replace("\\arabic*", "{n}"))
        if "{" not in tmpl:
            for ch, f in (("i", "{r}"), ("I", "{R}"), ("a", "{a}"), ("A", "{A}"), ("1", "{n}")):
                if ch in tmpl:
                    tmpl = tmpl.replace(ch, f, 1)
                    break
        return tmpl

    def _item(self, s: str, j: int):
        opt, k = read_opt(s, j)
        if self.enum_stack and opt is None and self.frames and self.frames[-1].env == "enumerate":
            e = self.enum_stack[-1]
            e["n"] += 1
            n = e["n"]
            lab = (e["fmt"].replace("{n}", str(n)).replace("{r}", _roman(n)).replace("{R}", _roman(n).upper())
                   .replace("{a}", _alpha(n)).replace("{A}", _alpha(n).upper()))
            lab = re.sub(r"\\[A-Za-z]+\s*|[{}]", "", lab).strip()
            tok, ident = self._anchor("item")
            self.current = LabelTarget("item", lab.rstrip("."), ident, "item")
            if e.get("custom"):
                self.st.item_labels[ident] = lab
            return ("emit", "\\item " + tok, k)
        if opt is not None:
            return ("emit", "\\item[%s] " % self._process(opt), k)
        return ("emit", "\\item ", k)

    # ------------------------------------------------------------------ theorems
    def _theorem_def(self, env: str) -> TheoremDef | None:
        if env in self.ms.theorems:
            return self.ms.theorems[env]
        base = env.rstrip("*")
        if base in DEFAULT_THEOREMS and base not in self.ms.environments:
            title, style = DEFAULT_THEOREMS[base]
            numbered = not env.endswith("*")
            t = TheoremDef(env, title, base if numbered else None, None, style)
            self.ms.theorems[env] = t
            if numbered and base not in self.parent:
                self.parent[base] = None
            return t
        return None

    def _theorem(self, t: TheoremDef, s: str, k: int):
        opt, k = read_opt(s, k)
        saved = self.current
        number = ""
        if t.counter:
            self.step(t.counter)
            number = self.the(t.counter)
        tok, ident = self._anchor("thm")
        self.current = LabelTarget("theorem", number, ident, t.title)
        head = "\\textbf{%s%s}" % (t.title, (" " + number) if number else "")
        if opt:
            head += " (%s)" % self._process(opt).strip()
        head += "."
        env_name = {"plain": "m2kthmplain", "definition": "m2kthmdefinition", "remark": "m2kthmremark"}[t.style]
        self.frames.append(Frame(t.env, "\n\\end{%s}\n" % env_name, saved, True))
        return ("emit", "\n\\begin{%s}\n%s%s " % (env_name, tok, head), k)

    # ------------------------------------------------------------------ sections
    def _section_heading(self, level: int, title_out: str, numbered: bool, number: str = "",
                         ident: str | None = None) -> str:
        tok = self._tok("SN")
        if ident is None:
            ident = f"sec-{self.n}"
        self.st.secnums[tok] = (number if numbered else "", ident)
        idx = max(0, min(level - self.top_level, len(PANDOC_HEADINGS) - 1))
        return "\n\n\\%s*{%s %s}\n\n" % (PANDOC_HEADINGS[idx], tok, title_out)

    def _section(self, name: str, s: str, j: int):
        star, k = read_star(s, j)
        short, k = read_opt(s, k)
        title, k = read_arg(s, k)
        if title is None:
            return ("emit", "", k)
        level = SECTION_LEVELS[name]
        title_out = self._process(title).strip()
        if name in ("paragraph", "subparagraph"):
            t = title_out.rstrip()
            if not re.search(r"[.:!?]$", t):
                t += "."
            return ("emit", "\n\n\\noindent\\textbf{%s} " % t, k)
        numbered = not star and level >= self.top_level and level <= 3
        number = ""
        if numbered:
            self.step(name)
            number = self.the(name)
        tok = self._tok("SN")
        ident = f"sec-{self.n}"
        self.st.secnums[tok] = (number, ident)
        kindname = {0: "Chapter", 1: "Section", 2: "Section", 3: "Section"}.get(level, "Section")
        if self.appendix and level == self.top_level:
            kindname = "Appendix"
        self.current = LabelTarget("section", number, ident, kindname)
        if level < self.top_level:
            level = self.top_level
        idx = max(0, min(level - self.top_level, len(PANDOC_HEADINGS) - 1))
        return ("emit", "\n\n\\%s*{%s %s}\n\n" % (PANDOC_HEADINGS[idx], tok, title_out), k)

    # ------------------------------------------------------------------ citations
    def _cite(self, name: str, s: str, j: int):
        star, k = read_star(s, j)
        o1, k = read_opt(s, k)
        o2, k = read_opt(s, k)
        keys_s, k = read_arg(s, k)
        if name == "cites":   # biblatex multi-cite: \cites{a}{b}...
            keys = [x.strip() for x in (keys_s or "").split(",")]
            while True:
                kk = skip_ws(s, k)
                if kk < len(s) and s[kk] == "{":
                    more, k = read_arg(s, kk)
                    keys += [x.strip() for x in (more or "").split(",")]
                else:
                    break
        else:
            keys = [x.strip() for x in (keys_s or "").split(",") if x.strip()]
        pre, post = (o1, o2) if o2 is not None else (None, o1)
        tok = self._tok("CIT")
        self.st.cites[tok] = (name, keys, self._process(pre) if pre else None, self._process(post) if post else None)
        self.used_bib = True
        if name == "footcite":
            return ("emit", "\\footnote{%s}" % tok, k)
        return ("emit", tok, k)

    def _bibliography(self) -> str:
        if not self.bib:
            self.warn("No .bbl bibliography found; citations will show their keys")
            return ""
        return self._bib_html(self.bib)

    def _bib_html(self, entries: list[BibEntry]) -> str:
        parts = [self._section_heading(self.top_level, "References", numbered=False)]
        parts.append("\n\\begin{m2kbib}\n")
        for b in entries:
            tok, ident = self._anchor("bib")
            self.st.labels.setdefault("bib::" + b.key, LabelTarget("bib", b.label, ident))
            bl = self._tok("BL")
            self.st.biblabels[bl] = b.label
            saved = self.current
            text = self._process(b.text)
            self.current = saved
            parts.append("\n\\begin{m2kbibitem}\n%s%s %s\n\\end{m2kbibitem}\n" % (tok, bl, text))
        parts.append("\n\\end{m2kbib}\n")
        self.bib = []    # emit once
        return "".join(parts)

    # ------------------------------------------------------------------ figures
    def _includegraphics(self, s: str, j: int):
        star, k = read_star(s, j)
        opts, k = read_opt(s, k)
        name, k = read_arg(s, k)
        if not name:
            return ("emit", "", k)
        bases = [self.root] + [self.root / g for g in self.ms.graphicspath]
        src = find_graphic(name, bases, self.root)
        if src is None:
            self.warn(f"Figure file not found: {name}")
            return ("emit", "[figure: %s]" % name.replace("_", "\\_"), k)
        self.fig_n += 1
        res = convert_graphic(src, self.fig_dir, f"fig{self.fig_n:03d}")
        if res is None:
            return ("emit", "[figure: %s]" % src.name.replace("_", "\\_"), k)
        out, natural = res
        pct = width_percent(opts, natural)
        self.st.images[str(out)] = out
        w = ("[width=%.2f\\textwidth]" % (pct / 100.0)) if pct else ""
        return ("emit", "\\includegraphics%s{%s}" % (w, out.as_posix()), k)

    def _caption(self, s: str, j: int):
        short, k = read_opt(s, j)
        text, k = read_arg(s, k)
        kind = self.float_kind or "figure"
        self.step(kind)
        number = self.the(kind)
        tok, ident = self._anchor("fig" if kind == "figure" else "tab")
        self.current = LabelTarget(kind, number, ident, kind.capitalize())
        body = self._process(text or "")
        return ("emit", "\\caption{%s\\textbf{%s %s.} %s}" % (tok, kind.capitalize(), number, body), k)

    def _picture(self, env_src: str) -> str:
        """TikZ & friends: compile with LaTeX (safe mode) into an image if possible."""
        if self.snippets is not None and getattr(self.snippets, "available", False):
            self.fig_n += 1
            out = self.fig_dir / f"pic{self.fig_n:03d}.png"
            self.fig_dir.mkdir(parents=True, exist_ok=True)
            piece = self.snippets.render(env_src, False, out, picture=True)
            if piece is not None:
                self.st.images[str(out)] = out
                pct = max(20, min(100, round(100 * piece.w_px / 1100)))
                img = "\\includegraphics[width=%.2f\\textwidth]{%s}" % (pct / 100.0, out.as_posix())
                if self.float_kind is None:      # free-standing picture: make it a centred figure
                    return "\n\n\\begin{figure}%s\\end{figure}\n\n" % img
                return img
        self.warn("A TikZ/picture environment could not be rendered and was omitted")
        return "\n\n\\emph{[Diagram omitted: see the PDF version.]}\n\n"

    # ------------------------------------------------------------------ maths
    def _inline(self, tex: str) -> str:
        tex = tex.strip()
        if not tex:
            return ""
        labels = []
        tex, labels = strip_cmd_with_arg(tex, "label")
        tex = re.sub(r"\s*\n\s*", " ", tex)
        tok = self._tok("MI")
        self.st.math[tok] = MathItem(tex, False)
        return tok

    def _register_display(self, tex: str, labels: list[str], number: str | None) -> str:
        tex = re.sub(r"\n\s*\n", "\n", tex)    # a blank line would end the paragraph in LaTeX
        tok = self._tok("MD")
        ident = f"eq-{self.n}"
        self.st.math[tok] = MathItem(tex, True, ident)
        for key in labels:
            self.st.labels[key.strip()] = LabelTarget("equation", number or "", ident, "Equation")
        return " %s " % tok

    def _display(self, tex: str) -> str:
        """$$..$$ or \\[..\\]: unnumbered unless \\tag is present."""
        tex, labels = strip_cmd_with_arg(tex, "label")
        tag = self._extract_tag(tex)
        tex = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", tex)
        if not tex.strip():
            return ""
        return self._register_display(tex.strip(), labels, tag)

    @staticmethod
    def _extract_tag(tex: str) -> str | None:
        m = re.search(r"\\tag\*?\s*\{", tex)
        if not m:
            return None
        e = match_brace(tex, m.end() - 1)
        return tex[m.end():e]

    def _number_row(self, row: str, numbered_env: bool) -> tuple[str, list[str], str | None]:
        row, labels = strip_cmd_with_arg(row, "label")
        tag = self._extract_tag(row)
        nonum = bool(re.search(r"\\(nonumber|notag)(?![A-Za-z])", row))
        row = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", row)
        number = None
        if tag is not None:
            number = tag
        elif numbered_env and not nonum:
            self.step("equation")
            number = self.the("equation")
            row = row.rstrip() + " \\tag{%s}" % number
        return row, labels, number

    def _math_env(self, env: str, content: str, arg: str = "") -> str:
        base = env.rstrip("*")
        numbered = not env.endswith("*") and env in NUMBERED_MATH
        if base in ("equation", "displaymath", "dmath"):
            content = re.sub(r"\\begin\{split\}", r"\\begin{aligned}", content).replace("\\end{split}", "\\end{aligned}")
            row, labels, number = self._number_row(content, numbered)
            if not row.strip():
                return ""
            return self._register_display(row.strip(), labels, number)
        if base == "multline":
            rows_tex, labels = strip_cmd_with_arg(content, "label")
            tag = self._extract_tag(rows_tex)
            nonum = bool(re.search(r"\\(nonumber|notag)(?![A-Za-z])", rows_tex))
            rows_tex = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", rows_tex)
            number = tag
            if tag is None and numbered and not nonum:
                self.step("equation")
                number = self.the("equation")
                rows_tex = rows_tex.rstrip() + " \\tag{%s}" % number
            return self._register_display("\\begin{multline*}%s\\end{multline*}" % rows_tex, labels, number)
        # row-based environments
        out_env = {"align": "align*", "flalign": "align*", "alignat": "alignat*", "xalignat": "alignat*",
                   "xxalignat": "alignat*", "gather": "gather*", "eqnarray": "eqnarray*",
                   "dgroup": "gather*"}.get(base, "align*")
        rows = split_top_level(content, "\\\\")
        # groups are separated by \intertext; each: (rows, [(label, number)], [numbers])
        groups: list[tuple[list[str], list[tuple[str, str | None]], list[str | None]]] = [([], [], [])]
        between: list[str] = []
        for raw in rows:
            r = raw
            mt = re.match(r"\s*\\(intertext|shortintertext)\s*\{", r)
            if mt:
                e = match_brace(r, mt.end() - 1)
                between.append(r[mt.end():e])
                groups.append(([], [], []))
                r = r[e + 1:]
            if not r.strip() and len(rows) > 1:
                continue
            row, labels, number = self._number_row(r, numbered)
            g = groups[-1]
            g[0].append(row.strip())
            g[1].extend((lab, number) for lab in labels)
            g[2].append(number)
        result = []
        for gi, (grows, glabels, gnums) in enumerate(groups):
            if gi > 0 and gi - 1 < len(between):
                result.append("\n\n\\noindent %s\n\n" % self._process(between[gi - 1]))
            if not grows:
                continue
            tex = "\\begin{%s}%s%s\\end{%s}" % (out_env, arg, " \\\\\n".join(grows), out_env)
            tok = self._register_display(tex, [], next((x for x in gnums if x), None))
            anchor = self.st.math[tok.strip()].anchor
            for key, num in glabels:
                self.st.labels[key.strip()] = LabelTarget("equation", num or "", anchor, "Equation")
            result.append(tok)
        return "".join(result)

    def _finalize_math(self) -> None:
        """Resolve \\ref/\\eqref/\\cite inside maths to plain text."""
        def ref_text(key: str, eq: bool) -> str:
            t = self.st.labels.get(key.strip())
            if t is None:
                return "??"
            return "(%s)" % t.number if eq else t.number

        for item in self.st.math.values():
            tex = item.tex
            if "\\" not in tex:
                continue
            tex = re.sub(r"\\eqref\s*\{([^}]*)\}", lambda m: "\\text{%s}" % ref_text(m.group(1), True), tex)
            tex = re.sub(r"\\(?:ref|cref|Cref|autoref)\s*\{([^}]*)\}",
                         lambda m: "\\text{%s}" % ref_text(m.group(1), False), tex)
            tex = re.sub(r"\\cite[pt]?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}",
                         lambda m: "\\text{[%s]}" % ", ".join(
                             self.bib_label(k.strip()) for k in m.group(1).split(",")), tex)
            item.tex = tex

    def bib_label(self, key: str) -> str:
        t = self.st.labels.get("bib::" + key)
        return t.number if t else key
