"""Harvest macro / operator / theorem definitions from a LaTeX preamble."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .texscan import CS_RE, match_brace, read_arg, read_opt, read_star, skip_ws

# Names whose redefinition we never expand ourselves in text (LaTeX internals,
# formatting hooks).  Definitions are still passed to MathJax/LaTeX.
NO_TEXT_EXPAND = {
    "section", "subsection", "subsubsection", "paragraph", "chapter", "part", "cite", "ref", "eqref",
    "label", "footnote", "emph", "textbf", "textit", "item", "maketitle", "title", "author", "date",
    "thanks", "caption", "includegraphics", "url", "href", "baselinestretch", "arraystretch",
    "familydefault", "rmdefault", "sfdefault", "abstractname", "refname", "bibname", "contentsname",
    "proofname", "qedsymbol", "theequation", "thesection", "thefigure", "thetable", "labelenumi",
    "labelenumii", "labelitemi", "footnoterule", "thefootnote", "and", "AND", "newblock", "bibitem",
    "appendix", "tableofcontents", "begin", "end", "par", "\\", "cref", "Cref", "autoref",
}


@dataclass
class Macro:
    name: str               # without backslash
    nargs: int
    default: str | None     # default for optional first argument
    body: str
    raw: str                # original definition text (for MathJax / LaTeX)
    kind: str = "newcommand"  # newcommand|def|operator|paired|let|env


@dataclass
class TheoremDef:
    env: str
    title: str
    counter: str | None      # None => unnumbered
    within: str | None
    style: str = "plain"     # plain | definition | remark


@dataclass
class MacroSet:
    macros: dict[str, Macro] = field(default_factory=dict)
    environments: dict[str, tuple[int, str | None, str, str]] = field(default_factory=dict)
    theorems: dict[str, TheoremDef] = field(default_factory=dict)
    raw_defs: list[str] = field(default_factory=list)        # in order, for MathJax
    numberwithin: dict[str, str] = field(default_factory=dict)  # counter -> parent
    graphicspath: list[str] = field(default_factory=list)
    usepackages: list[str] = field(default_factory=list)     # raw \usepackage lines
    tikz_extra: list[str] = field(default_factory=list)      # \usetikzlibrary, \tikzset ...

    def text_expandable(self, name: str) -> Macro | None:
        m = self.macros.get(name)
        if m is None or m.kind not in ("newcommand", "def", "let") or name in NO_TEXT_EXPAND:
            return None
        if "@" in name:
            return None
        return m

    def mathjax_defs(self) -> list[str]:
        """Definitions suitable for MathJax (skip @-internals and obvious text-only stuff)."""
        out = []
        for d in self.raw_defs:
            if "@" in d.split("{", 1)[0] or "\\makeatletter" in d:
                continue
            if re.match(r"\\(re)?newcommand\*?\s*\{?\\(section|subsection|cite|ref|label|footnote|"
                        r"thesection|theequation|baselinestretch|arraystretch)\b", d):
                continue
            out.append(d)
        return out


DEF_CMDS = ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand", "def", "gdef", "edef",
            "xdef", "let", "DeclareMathOperator", "DeclarePairedDelimiter", "DeclarePairedDelimiterX",
            "newenvironment", "renewenvironment", "newtheorem", "theoremstyle", "declaretheorem",
            "numberwithin", "graphicspath", "usepackage", "usetikzlibrary", "tikzset", "tikzcdset",
            "newcounter", "spnewtheorem", "DeclareMathAlphabet", "SetMathAlphabet", "DeclareSymbolFont",
            "DeclareMathSymbol", "newtheoremstyle")

DEF_RE = re.compile(r"\\(" + "|".join(DEF_CMDS) + r")(?![A-Za-z@])")


def _read_cs_name(s: str, i: int) -> tuple[str | None, int]:
    """Read ``\\foo`` or ``{\\foo}`` -> 'foo'."""
    j = skip_ws(s, i)
    if j < len(s) and s[j] == "{":
        e = match_brace(s, j)
        inner = s[j + 1:e].strip()
        m = CS_RE.fullmatch(inner)
        return (m.group(1) if m else None), e + 1
    m = CS_RE.match(s, j)
    if m:
        return m.group(1), m.end()
    return None, j


def harvest(text: str, ms: MacroSet | None = None, *, theorem_style: list[str] | None = None) -> tuple[MacroSet, list[tuple[int, int]]]:
    """Scan text for definitions.  Returns the MacroSet and the spans of the
    definitions (so a caller can delete them from a document body)."""
    ms = ms or MacroSet()
    style = theorem_style if theorem_style is not None else ["plain"]
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while True:
        m = DEF_RE.search(text, pos)
        if not m:
            break
        cmd = m.group(1)
        start = m.start()
        i = m.end()
        try:
            end = _handle(cmd, text, i, ms, style)
        except Exception:  # never let a weird definition kill the conversion
            end = None
        if end is None or end <= i:
            pos = i
            continue
        spans.append((start, end))
        pos = end
        if pos >= n:
            break
    return ms, spans


def _handle(cmd: str, s: str, i: int, ms: MacroSet, style: list[str]) -> int | None:
    start = s.rfind("\\" + cmd, 0, i)
    if cmd in ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"):
        _, i = read_star(s, skip_ws(s, i))
        name, i = _read_cs_name(s, i)
        if not name:
            return None
        nargs_s, i = read_opt(s, i)
        default, i = read_opt(s, i)
        body, i = read_arg(s, i)
        if body is None:
            return None
        try:
            nargs = int((nargs_s or "0").strip() or 0)
        except ValueError:
            nargs = 0
        raw = s[start:i]
        if cmd == "providecommand" and name in ms.macros:
            return i
        ms.macros[name] = Macro(name, nargs, default, body, raw, "newcommand")
        ms.raw_defs.append(raw)
        return i
    if cmd in ("def", "gdef", "edef", "xdef"):
        m = CS_RE.match(s, skip_ws(s, i))
        if not m:
            return None
        name = m.group(1)
        j = m.end()
        k = s.find("{", j)
        if k < 0:
            return None
        params = s[j:k]
        if not re.fullmatch(r"\s*(#\d\s*)*", params):
            # delimited parameters: keep for LaTeX, too exotic for us/MathJax
            e = match_brace(s, k)
            ms.raw_defs.append("%" + s[start:e + 1])  # commented: LaTeX-only marker
            return e + 1
        nargs = len(re.findall(r"#\d", params))
        e = match_brace(s, k)
        body = s[k + 1:e]
        raw = "\\def\\%s%s{%s}" % (name, params.strip(), body)
        ms.macros[name] = Macro(name, nargs, None, body, raw, "def")
        ms.raw_defs.append(raw)
        return e + 1
    if cmd == "let":
        m = re.match(r"\s*\\([A-Za-z@]+)\s*=?\s*(\\[A-Za-z@]+|\S)", s[i:])
        if not m:
            return None
        name, target = m.group(1), m.group(2)
        raw = "\\let\\%s%s" % (name, target)
        ms.macros[name] = Macro(name, 0, None, target, raw, "let")
        ms.raw_defs.append(raw)
        return i + m.end()
    if cmd == "DeclareMathOperator":
        star, i = read_star(s, skip_ws(s, i))
        name, i = _read_cs_name(s, i)
        body, i = read_arg(s, i)
        if not name or body is None:
            return None
        raw = "\\DeclareMathOperator%s{\\%s}{%s}" % ("*" if star else "", name, body)
        ms.macros[name] = Macro(name, 0, None, body, raw, "operator")
        ms.raw_defs.append(raw)
        return i
    if cmd in ("DeclarePairedDelimiter", "DeclarePairedDelimiterX"):
        name, j = _read_cs_name(s, i)
        if not name:
            return None
        if cmd.endswith("X"):
            _, j = read_opt(s, j)
        args = []
        for _ in range(3 if cmd.endswith("X") else 2):
            a, j = read_arg(s, j)
            args.append(a)
        raw = s[start:j]
        ms.macros[name] = Macro(name, 1, None, "", raw, "paired")
        ms.raw_defs.append(raw)
        return j
    if cmd in ("newenvironment", "renewenvironment"):
        _, i = read_star(s, skip_ws(s, i))
        name, i = read_arg(s, i)
        nargs_s, i = read_opt(s, i)
        default, i = read_opt(s, i)
        begin, i = read_arg(s, i)
        end_, i = read_arg(s, i)
        if not name or begin is None or end_ is None:
            return None
        try:
            nargs = int((nargs_s or "0").strip() or 0)
        except ValueError:
            nargs = 0
        ms.environments[name.strip()] = (nargs, default, begin, end_)
        ms.raw_defs.append(s[start:i])
        return i
    if cmd == "theoremstyle":
        a, j = read_arg(s, i)
        if a:
            style[0] = a.strip()
        return j
    if cmd in ("newtheorem", "spnewtheorem"):
        star, i = read_star(s, skip_ws(s, i))
        env, i = read_arg(s, i)
        shared, i = read_opt(s, i)
        title, i = read_arg(s, i)
        within, i = read_opt(s, i)
        if cmd == "spnewtheorem":   # llncs: \spnewtheorem{env}[shared]{Title}[within]{headfont}{bodyfont}
            hf, i2 = read_arg(s, i)
            bf, i2 = read_arg(s, i2)
            if hf is not None and bf is not None:
                i = i2
        if not env or title is None:
            return None
        env = env.strip()
        st = _norm_style(style[0])
        if star:
            ms.theorems[env] = TheoremDef(env, title.strip(), None, None, st)
        elif shared:
            base = ms.theorems.get(shared.strip())
            counter = base.counter if base and base.counter else shared.strip()
            w = base.within if base else ms.numberwithin.get(counter)
            ms.theorems[env] = TheoremDef(env, title.strip(), counter, w, st)
        else:
            ms.theorems[env] = TheoremDef(env, title.strip(), env, within.strip() if within else None, st)
        return i
    if cmd == "declaretheorem":
        opts, i = read_opt(s, i)
        env, i = read_arg(s, i)
        opts2, i2 = read_opt(s, i)     # thmtools also accepts options after
        if opts2 is not None:
            opts = (opts or "") + "," + opts2
            i = i2
        if not env:
            return None
        kv = dict(re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|[^,]+)", opts or ""))
        kv = {k: v.strip().strip("{}") for k, v in kv.items()}
        title = kv.get("name", env.strip().capitalize())
        st = _norm_style(kv.get("style", "plain"))
        if kv.get("numbered", "yes") == "no":
            ms.theorems[env.strip()] = TheoremDef(env.strip(), title, None, None, st)
        elif "sibling" in kv or "sharenumber" in kv:
            sib = kv.get("sibling") or kv.get("sharenumber")
            base = ms.theorems.get(sib)
            ms.theorems[env.strip()] = TheoremDef(env.strip(), title, base.counter if base else sib,
                                                  base.within if base else None, st)
        else:
            ms.theorems[env.strip()] = TheoremDef(env.strip(), title, env.strip(),
                                                  kv.get("numberwithin") or kv.get("parent"), st)
        return i
    if cmd == "numberwithin":
        _, i = read_opt(s, i)
        a, i = read_arg(s, i)
        b, i = read_arg(s, i)
        if a and b:
            ms.numberwithin[a.strip()] = b.strip()
        return i
    if cmd == "graphicspath":
        a, j = read_arg(s, i)
        if a:
            ms.graphicspath.extend(re.findall(r"\{([^}]*)\}", a) or [a])
        return j
    if cmd == "usepackage":
        opt, j = read_opt(s, i)
        a, j = read_arg(s, j)
        if a:
            ms.usepackages.append(s[start:j])
        return j
    if cmd in ("usetikzlibrary", "tikzset", "tikzcdset"):
        a, j = read_arg(s, i)
        ms.tikz_extra.append(s[start:j])
        return j
    if cmd in ("DeclareMathAlphabet", "SetMathAlphabet", "DeclareSymbolFont", "DeclareMathSymbol",
               "newcounter", "newtheoremstyle"):
        # consume arguments so they don't leak into text; LaTeX-only
        j = i
        nargs = {"DeclareMathAlphabet": 5, "SetMathAlphabet": 6, "DeclareSymbolFont": 5,
                 "DeclareMathSymbol": 4, "newcounter": 1, "newtheoremstyle": 9}[cmd]
        for _ in range(nargs):
            _, j = read_arg(s, j)
        if cmd == "newcounter":
            _, j = read_opt(s, j)
        ms.raw_defs.append("%" + s[start:j])
        return j
    return None


def _norm_style(st: str) -> str:
    st = (st or "plain").strip().lower()
    if st in ("definition", "defn", "def"):
        return "definition"
    if st in ("remark", "note"):
        return "remark"
    return "plain"


def expand_macro(m: Macro, s: str, i: int) -> tuple[str, int] | None:
    """Expand macro m whose arguments start at s[i].  Returns (expansion, end)."""
    if m.kind == "let":
        return m.body, i
    args: list[str] = []
    j = i
    nargs = m.nargs
    if m.default is not None and nargs > 0:
        opt, j = read_opt(s, j)
        args.append(m.default if opt is None else opt)
        nargs -= 1
    for _ in range(nargs):
        a, j2 = read_arg(s, j)
        if a is None:
            return None
        args.append(a)
        j = j2
    body = m.body
    if args:
        body = re.sub(r"#(\d)", lambda mm: args[int(mm.group(1)) - 1] if 0 < int(mm.group(1)) <= len(args) else "",
                      body.replace("##", "\x00")).replace("\x00", "#")
    # A control word followed by a letter must stay separated after expansion.
    if m.nargs == 0 and j < len(s) and s[j].isalpha() and re.search(r"\\[A-Za-z@]+$", body):
        body += " "
    return body, j


DEFAULT_THEOREMS = {
    # name: (title, style)
    "theorem": ("Theorem", "plain"), "thm": ("Theorem", "plain"), "lemma": ("Lemma", "plain"),
    "lem": ("Lemma", "plain"), "proposition": ("Proposition", "plain"), "prop": ("Proposition", "plain"),
    "corollary": ("Corollary", "plain"), "cor": ("Corollary", "plain"), "conjecture": ("Conjecture", "plain"),
    "conj": ("Conjecture", "plain"), "claim": ("Claim", "plain"), "hypothesis": ("Hypothesis", "plain"),
    "assumption": ("Assumption", "plain"), "fact": ("Fact", "plain"), "observation": ("Observation", "plain"),
    "definition": ("Definition", "definition"), "defn": ("Definition", "definition"),
    "defi": ("Definition", "definition"), "dfn": ("Definition", "definition"),
    "example": ("Example", "definition"), "exmp": ("Example", "definition"), "ex": ("Example", "definition"),
    "exercise": ("Exercise", "definition"), "problem": ("Problem", "definition"),
    "question": ("Question", "definition"), "notation": ("Notation", "definition"),
    "remark": ("Remark", "remark"), "rem": ("Remark", "remark"), "rmk": ("Remark", "remark"),
    "note": ("Note", "remark"), "case": ("Case", "remark"),
}
