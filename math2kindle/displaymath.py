"""Structure of display maths, for e-ink layout.

A display is turned into a *spec*: rows (with alignment cells and equation
numbers) plus, for every row, the places where it may be broken, ranked like
a careful typesetter would:

  0  a top-level ``\\quad``/``\\qquad`` between separate formulas
  1  before a top-level relation (=, \\le, \\ll, ...)
  2  before a top-level binary + or -
  3+ before + or - inside an ordinary or \\bigl( ... \\bigr) bracket group
     (3 at nesting depth 1, 4 at depth 2, ...: outer groups are broken first)
  7  inside a long \\left( ... \\right) group (turned into \\biggl/\\biggr)
  8  inside a short bracket group (last resort)

Never inside braces, ``\\left...\\right`` pairs or environments, and never
between a big operator (\\sum, \\int, ...) and its operand (we only ever break
at relations and +/-).  The Node renderer measures the pieces with MathJax and
fills lines greedily; equation numbers are emitted as HTML text.
"""

from __future__ import annotations

import re

from .latex.texscan import CS_RE, match_brace, split_top_level

ROW_ENVS = ("align", "flalign", "alignat", "xalignat", "xxalignat", "gather", "multline", "eqnarray")
ALIGN_ENVS = ("align", "flalign", "alignat", "xalignat", "xxalignat", "eqnarray")

RELATION_CS = {
    "le", "leq", "ge", "geq", "ll", "gg", "lesssim", "gtrsim", "leqslant", "geqslant", "sim", "simeq",
    "asymp", "approx", "equiv", "ne", "neq", "subset", "subseteq", "supset", "supseteq", "to", "mapsto",
    "longrightarrow", "longmapsto", "iff", "implies", "impliedby", "Rightarrow", "Longrightarrow",
    "Leftrightarrow", "coloneqq", "coloneq", "eqqcolon", "propto", "cong", "prec", "succ", "preceq",
    "succeq", "lll", "ggg", "doteq", "triangleq", "stackrel", "overset", "underset", "xrightarrow",
    "xleftarrow", "in", "notin", "ni", "leqq", "geqq", "lneq", "gneq", "nless", "ngtr", "vdash", "models",
}
BINARY_CS = {"pm", "mp"}
OPEN_CS = {"lbrack", "langle", "lfloor", "lceil", "lvert", "lVert", "{"}
CLOSE_CS = {"rbrack", "rangle", "rfloor", "rceil", "rvert", "rVert", "}"}
SIZE_CS = {"big", "Big", "bigg", "Bigg", "bigl", "Bigl", "biggl", "Biggl", "bigr", "Bigr", "biggr", "Biggr",
           "bigm", "Bigm", "biggm", "Biggm"}
QUAD_RE = re.compile(r"(?:,\s*)?\\q?quad(?![A-Za-z])")


def _tags(row: str) -> tuple[str, str | None]:
    """Remove \\tag/\\notag/\\nonumber/\\label; return (row, printed label or None)."""
    label = None
    out = []
    pos = 0
    for m in re.finditer(r"\\tag(\*?)\s*\{", row):
        if m.start() < pos:
            continue
        e = match_brace(row, m.end() - 1)
        arg = row[m.end():e]
        label = arg if m.group(1) else "(%s)" % arg
        out.append(row[pos:m.start()])
        pos = e + 1
    out.append(row[pos:])
    row = "".join(out)
    row = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", row)
    row = re.sub(r"\\label\s*\{[^}]*\}", "", row)
    return row.strip(), label


def _env_body(tex: str):
    m = re.match(r"\s*\\begin\{(%s)(\*?)\}" % "|".join(ROW_ENVS), tex)
    if not m:
        return None
    env = m.group(1)
    end = r"\end{%s%s}" % (env, m.group(2))
    j = tex.rfind(end)
    if j < 0 or tex[j + len(end):].strip():
        return None
    body = tex[m.end():j]
    arg = ""
    if env in ("alignat", "xalignat", "xxalignat"):
        am = re.match(r"\s*\{(\d+)\}", body)
        if am:
            arg = "{%s}" % am.group(1)
            body = body[am.end():]
    return env, arg, body


def expand_user_macros(tex: str, ms) -> str:
    """Expand the author's \\newcommand/\\def macros (not operators) so that
    break analysis sees through e.g. \\bigO{...} = O\\left(...\\right)."""
    if ms is None or not getattr(ms, "macros", None):
        return tex
    from .latex.macros import expand_macro

    out = tex
    pos = 0
    count = 0
    while count < 400:
        m = CS_RE.search(out, pos)
        if not m:
            break
        name = m.group(1)
        mac = ms.macros.get(name)
        if mac is None or mac.kind not in ("newcommand", "def", "let") or "@" in name:
            pos = m.end()
            continue
        r = expand_macro(mac, out, m.end())
        if r is None:
            pos = m.end()
            continue
        body, end = r
        if out[:m.start()].rstrip().endswith(("^", "_")):
            body = "{" + body + "}"           # x^\foo must stay one script
        out = out[:m.start()] + body + out[end:]
        pos = m.start()
        count += 1
    return out


def soften_left_right(tex: str, min_len: int = 50) -> str:
    """Long top-level \\left( ... \\right) groups that contain a + or - become
    \\biggl( ... \\biggr) so the breaker may split them (as a TeX author would
    by hand).  Short groups keep their stretchy delimiters."""
    out = []
    i = 0
    n = len(tex)
    while i < n:
        m = re.compile(r"\\left(?![A-Za-z])").search(tex, i)
        if not m:
            out.append(tex[i:])
            break
        out.append(tex[i:m.start()])
        # find the matching \right
        depth = 0
        j = m.start()
        end = None
        for mm in re.finditer(r"\\(left|right)(?![A-Za-z])", tex[m.start():]):
            depth += 1 if mm.group(1) == "left" else -1
            if depth == 0:
                end = m.start() + mm.start()
                break
        if end is None:
            out.append(tex[m.start():])
            break
        dl = re.match(r"\s*(\\[A-Za-z]+|\\[{}|]|.)", tex[m.end():])
        dr = re.match(r"\s*(\\[A-Za-z]+|\\[{}|]|.)", tex[end + 6:])
        inner_start = m.end() + dl.end()
        inner = tex[inner_start:end]
        if len(inner) >= min_len and re.search(r"(?<![{^_(])\s*[+-]", inner):
            size = "bigg" if re.search(r"\\(frac|dfrac|sum|int|prod)", inner) else "Big"
            # \vphantom{} marks the group as "was \left...\right" for the breaker
            left = ("\\%sl." if dl.group(1) == "." else "\\%sl%s") % (size, dl.group(1)) + "\\vphantom{}"
            right = ("\\%sr." if dr.group(1) == "." else "\\%sr%s") % (size, dr.group(1))
            inner = re.sub(r"\\middle(?![A-Za-z])", r"\\%sm" % size, inner)
            out.append(left + " " + soften_left_right(inner, min_len) + " " + right)
        else:
            out.append(tex[m.start():end + 6 + dr.end()])
        i = end + 6 + dr.end()
    return "".join(out)


def display_spec(tex: str, macros=None) -> dict:
    """Describe a display formula as rows/cells/numbers (see module doc).
    ``macros`` (a MacroSet) lets the break analysis see inside user macros."""
    global _MACROS
    _MACROS = macros
    t = tex.strip()
    eb = _env_body(t)
    if eb is None:
        # equation-like; maybe the whole thing is one split/aligned block
        body, label = _tags(t)
        m = re.fullmatch(r"\s*\\begin\{(split|aligned)\}(.*)\\end\{\1\}\s*([.,;]?)\s*", body, re.S)
        if m and "\\begin{%s}" % m.group(1) not in m.group(2):
            rows = [r for r in split_top_level(m.group(2), "\\\\") if r.strip()]
            if rows:
                if m.group(3):
                    rows[-1] = rows[-1].rstrip() + m.group(3)
                return {"kind": "rows", "env": "align", "arg": "", "group_tag": label,
                        "rows": [_row(r, True) for r in rows]}
        return {"kind": "single", "env": "", "arg": "", "group_tag": label,
                "rows": [{"cells": [body], "tag": None, "segs": _segs(body)}]}
    env, arg, body = eb
    raw_rows = [r for r in split_top_level(body, "\\\\") if r.strip()]
    rows = [_row(r, env in ALIGN_ENVS) for r in raw_rows]
    group = None
    if env == "multline":
        labels = [r["tag"] for r in rows if r["tag"]]
        for r in rows:
            r["tag"] = None
        group = labels[-1] if labels else None
    return {"kind": "rows", "env": env, "arg": arg, "group_tag": group, "rows": rows}


def _row(raw: str, aligned: bool) -> dict:
    row, label = _tags(raw)
    cells = split_top_level(row, "&") if aligned else [row]
    cells = [c.strip() for c in cells]
    joined = " ".join(c for c in cells if c)
    return {"cells": cells, "tag": label, "segs": _segs(joined)}


_MACROS = None


def _segs(tex: str) -> list[dict]:
    """Break candidates for tex (used only if it is too wide as written)."""
    try:
        t = soften_left_right(expand_user_macros(tex, _MACROS))
    except Exception:  # pragma: no cover - analysis must never fail a render
        t = tex
    return break_candidates(t)


# ----------------------------------------------------------------------------- break points

def _bin_prio(paren: int, groups) -> int:
    if any(g[2] for g in groups):
        return 7          # inside a (softened) \left...\right group: avoid unless really needed
    return min(6, 2 + paren)


def break_candidates(tex: str) -> list[dict]:
    """Split tex into segments; each segment records the quality of the break
    *before* it (None for the first).  Concatenating the segments gives back
    the input (minus a dropped \\quad at a priority-0 break)."""
    cuts: list[tuple[int, int, int]] = []    # (position, priority, skip chars)
    groups: list[tuple[int, int, bool, int]] = []  # open groups: (start, cut index, soft, end of delimiter)

    def add_cut(pos: int, prio: int, skip: int = 0) -> None:
        # remember where the innermost open bracket ends, to indent continuations past it
        cuts.append((pos, prio, skip, groups[-1][3] if groups else None))

    def close_group(pos: int) -> None:
        # a break inside a short bracket group, e.g. (n s - 6 s), is a last resort
        if groups:
            start, first_cut, _soft, _end = groups.pop()
            if pos - start < 45:
                for k in range(first_cut, len(cuts)):
                    p0, pr, sk, op = cuts[k]
                    if pr >= 3:
                        cuts[k] = (p0, max(pr, 8), sk, op)
    n = len(tex)
    brace = lr = env = paren = 0
    i = 0
    prev_kind = "start"   # start | rel | bin | open | ord | punct
    while i < n:
        c = tex[i]
        if c == "\\":
            m = CS_RE.match(tex, i)
            if m is None:
                i += 1
                continue
            name = m.group(1)
            j = m.end()
            if name == "left":
                lr += 1
                i = j + 1 if j < n and tex[j] != "\\" else j
                prev_kind = "open"
                continue
            if name == "right":
                lr = max(0, lr - 1)
                i = j + 1 if j < n and tex[j] != "\\" else j
                prev_kind = "ord"
                continue
            if name == "begin":
                env += 1
            elif name == "end":
                env = max(0, env - 1)
            top = brace == 0 and lr == 0 and env == 0
            if name in SIZE_CS:
                # \bigl( ... \bigr): a bracket group we may break inside (priority 3)
                k = j
                while k < n and tex[k] == " ":
                    k += 1
                delim = tex[k:k + 1]
                if delim == "\\":
                    dm = CS_RE.match(tex, k)
                    dname = dm.group(1)
                    k2 = dm.end()
                    is_open = name.endswith("l") or dname in OPEN_CS
                    is_close = name.endswith("r") or dname in CLOSE_CS
                else:
                    k2 = k + 1
                    is_open = name.endswith("l") or delim in "(["
                    is_close = name.endswith("r") or delim in ")]"
                if top and not name.endswith("m"):
                    if is_open and not is_close:
                        paren += 1
                        soft = tex.startswith("\\vphantom{}", k2)
                        groups.append((i, len(cuts), soft, k2 + (len("\\vphantom{}") if soft else 0)))
                        prev_kind = "open"
                    elif is_close:
                        paren = max(0, paren - 1)
                        close_group(i)
                        prev_kind = "ord"
                i = k2
                continue
            if top and paren == 0 and name in ("quad", "qquad"):
                start = i
                # include a preceding comma+space in the previous segment, drop the quad
                add_cut(start, 0, j - start)
                prev_kind = "punct"
                i = j
                continue
            if top and name in RELATION_CS:
                if paren == 0 and prev_kind not in ("start", "rel", "open"):
                    add_cut(i, 1)
                prev_kind = "rel"
            elif top and name in BINARY_CS:
                if prev_kind in ("ord",):
                    add_cut(i, _bin_prio(paren, groups))
                prev_kind = "bin"
            elif name in OPEN_CS and top:
                prev_kind = "open"
            elif name in ("not",):
                pass
            else:
                prev_kind = "ord" if name not in ("," , ";", ":", "!", " ", "quad", "qquad") else prev_kind
            i = j
            continue
        if c == "{":
            brace += 1
            i += 1
            continue
        if c == "}":
            brace = max(0, brace - 1)
            i += 1
            prev_kind = "ord"
            continue
        top = brace == 0 and lr == 0 and env == 0
        if c in " \t\n":
            i += 1
            continue
        if top:
            if c in "([":
                paren += 1
                groups.append((i, len(cuts), False, i + 1))
                prev_kind = "open"
            elif c in ")]":
                paren = max(0, paren - 1)
                close_group(i)
                prev_kind = "ord"
            elif c in "=<>":
                if paren == 0 and prev_kind not in ("start", "rel", "open"):
                    add_cut(i, 1)
                prev_kind = "rel"
            elif c in "+-":
                if prev_kind == "ord":
                    add_cut(i, _bin_prio(paren, groups))
                prev_kind = "bin"
            elif c in ",;":
                prev_kind = "punct"
            elif c in "^_":
                pass
            else:
                prev_kind = "ord"
        else:
            if c not in "^_":
                pass
        i += 1
    # build segments; each knows the separator dropped before it (a \\quad
    # break) and, for breaks inside a bracket, where that bracket opened
    raw: list[list] = []          # [start, end, prio, sep, open_pos]
    last, skip, prio, open_pos, sep = 0, 0, None, None, ""
    for pos, p, sk, op in cuts + [(n, None, 0, None)]:
        a = last + skip
        while a < pos and tex[a] in " \t\n":
            a += 1
        b = pos
        while b > a and tex[b - 1] in " \t\n":
            b -= 1
        if b > a:
            raw.append([a, b, prio, sep, open_pos])
            prio, open_pos = p, op
            sep = tex[pos:pos + sk] if sk else ""
        else:
            if p is not None:
                prio = p if prio is None else min(prio, p)
                open_pos = op if op is not None else open_pos
                sep = (sep + tex[pos:pos + sk]) if sk else sep
        last, skip = pos, sk
    segs: list[dict] = []
    for k, (a, b, pr, sp, op) in enumerate(raw):
        seg = {"tex": tex[a:b], "prio": None if k == 0 else pr}
        if sp and k:
            seg["sep"] = sp
        if op is not None and k and pr is not None and pr >= 3:
            for j in range(k):
                if raw[j][0] <= op <= raw[j][1]:
                    seg["open"] = [j, op - raw[j][0]]
                    break
        segs.append(seg)
    return segs
