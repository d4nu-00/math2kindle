"""Low-level, dependency-free helpers for scanning LaTeX source.

These are deliberately forgiving: they never raise on malformed input, they
just return the best guess (e.g. an unmatched brace extends to end of text).
"""

from __future__ import annotations

import re

CS_RE = re.compile(r"\\([A-Za-z@]+|.)", re.S)
WS = " \t\r\n"

VERBATIM_ENVS = ("verbatim", "verbatim*", "lstlisting", "minted", "Verbatim", "comment", "alltt")


def strip_comments(text: str) -> str:
    """Remove TeX comments (``%`` to end of line, plus the newline and the next
    line's leading blanks, like TeX does), ``comment`` environments and
    ``\\iffalse ... \\fi`` blocks.  Verbatim environments are left intact."""
    out: list[str] = []
    i, n = 0, len(text)
    verb_end: str | None = None
    while i < n:
        if verb_end is not None:
            j = text.find(verb_end, i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j + len(verb_end)])
            i = j + len(verb_end)
            verb_end = None
            continue
        c = text[i]
        if c == "\\":
            m = re.match(r"\\begin\s*\{(verbatim\*?|lstlisting|minted|Verbatim|alltt)\}", text[i:i + 40])
            if m:
                verb_end = "\\end{%s}" % m.group(1)
                out.append(m.group(0))
                i += len(m.group(0))
                continue
            if text.startswith("\\verb", i) and i + 5 < n and not text[i + 5].isalpha():
                k = i + 5
                if k < n and text[k] == "*":
                    k += 1
                if k < n:
                    delim = text[k]
                    e = text.find(delim, k + 1)
                    nl = text.find("\n", k + 1)
                    if e > 0 and (nl < 0 or e < nl):
                        out.append(text[i:e + 1])
                        i = e + 1
                        continue
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "%":
            j = text.find("\n", i)
            if j < 0:
                break
            # TeX drops the end-of-line and leading spaces of the next line,
            # but a following blank line still ends the paragraph.
            k = j + 1
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "\n":
                out.append("\n")
            i = k
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    s = re.sub(r"\\begin\{comment\}.*?\\end\{comment\}", "", s, flags=re.S)
    return remove_iffalse(s)


_IF_RE = re.compile(r"\\(iffalse|if[a-zA-Z@]*|fi)(?![a-zA-Z@])")


def remove_iffalse(s: str) -> str:
    """Delete ``\\iffalse ... \\fi`` (nesting-aware; ``\\iff`` is not an \\if)."""
    out = []
    pos = 0
    while True:
        m = re.search(r"\\iffalse(?![a-zA-Z@])", s[pos:])
        if not m:
            out.append(s[pos:])
            break
        start = pos + m.start()
        out.append(s[pos:start])
        depth = 1
        k = pos + m.end()
        end = len(s)
        for mm in _IF_RE.finditer(s, k):
            name = mm.group(1)
            if name == "iff":
                continue
            if name == "fi":
                depth -= 1
                if depth == 0:
                    end = mm.end()
                    break
            elif name.startswith("if"):
                depth += 1
        pos = end
    return "".join(out)


def skip_ws(s: str, i: int, *, allow_par: bool = True) -> int:
    n = len(s)
    while i < n and s[i] in WS:
        if not allow_par and s[i] == "\n" and s.find("\n", i + 1) == _next_nonblank_nl(s, i):
            break
        i += 1
    return i


def _next_nonblank_nl(s: str, i: int) -> int:  # helper for skip_ws(allow_par=False)
    j = i + 1
    while j < len(s) and s[j] in " \t\r":
        j += 1
    return j if j < len(s) and s[j] == "\n" else -1


def match_brace(s: str, i: int, open_: str = "{", close: str = "}") -> int:
    """``s[i]`` is an opening delimiter; return index of its partner (or len(s)-1)."""
    depth = 0
    n = len(s)
    j = i
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


def read_arg(s: str, i: int) -> tuple[str | None, int]:
    """Read a mandatory argument at/after i: ``{...}``, a control sequence or a char."""
    j = skip_ws(s, i)
    if j >= len(s):
        return None, j
    c = s[j]
    if c == "{":
        e = match_brace(s, j)
        return s[j + 1:e], e + 1
    if c == "\\":
        m = CS_RE.match(s, j)
        return m.group(0), m.end()
    if c in "}":
        return None, i
    return c, j + 1


def read_opt(s: str, i: int) -> tuple[str | None, int]:
    """Read an optional ``[...]`` argument (brace-aware); None if absent."""
    j = skip_ws(s, i)
    if j < len(s) and s[j] == "[":
        depth = 0
        k = j + 1
        n = len(s)
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c == "{":
                k = match_brace(s, k) + 1
                continue
            if c == "]" and depth == 0:
                return s[j + 1:k], k + 1
            k += 1
        return s[j + 1:], n
    return None, i


def read_star(s: str, i: int) -> tuple[bool, int]:
    if i < len(s) and s[i] == "*":
        return True, i + 1
    return False, i


def find_env_end(s: str, i: int, name: str) -> tuple[int, int]:
    """From i (just after ``\\begin{name}``) find the matching ``\\end{name}``.
    Returns (start_of_end, index_after_end).  Nesting-aware."""
    pat = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
    depth = 1
    for m in pat.finditer(s, i):
        # ignore escaped backslashes (\\begin is a line break + "begin")
        bs = 0
        k = m.start() - 1
        while k >= 0 and s[k] == "\\":
            bs += 1
            k -= 1
        if bs % 2:
            continue
        if m.group(1) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
    return len(s), len(s)


def find_math_end(s: str, i: int, closer: str) -> int:
    """Find closing math delimiter (``$``, ``$$``, ``\\)``, ``\\]``) at brace depth 0."""
    depth = 0
    n = len(s)
    j = i
    while j < n:
        c = s[j]
        if c == "\\":
            if depth == 0 and closer.startswith("\\") and s.startswith(closer, j):
                return j
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == "$" and depth == 0 and closer[0] == "$":
            if closer == "$$":
                if s.startswith("$$", j):
                    return j
            else:
                return j
        j += 1
    return -1


def split_top_level(tex: str, sep: str) -> list[str]:
    """Split on ``\\\\`` (sep='\\\\') or ``&`` (sep='&') at brace depth 0 and
    outside nested environments."""
    parts: list[str] = []
    depth = 0
    env_depth = 0
    cur_start = 0
    j = 0
    n = len(tex)
    while j < n:
        c = tex[j]
        if c == "\\":
            if tex.startswith("\\begin", j):
                env_depth += 1
                j += 6
                continue
            if tex.startswith("\\end", j) and not tex.startswith("\\endgroup", j):
                env_depth = max(0, env_depth - 1)
                j += 4
                continue
            if sep == "\\\\" and tex.startswith("\\\\", j) and depth == 0 and env_depth == 0:
                parts.append(tex[cur_start:j])
                k = j + 2
                if k < n and tex[k] == "*":
                    k += 1
                # optional spacing argument  \\[2pt]
                kk = k
                while kk < n and tex[kk] in " \t\n":
                    kk += 1
                if kk < n and tex[kk] == "[":
                    e = tex.find("]", kk)
                    if e > 0 and re.fullmatch(r"\s*-?[\d.]+\s*[a-z]{2}\s*", tex[kk + 1:e]):
                        k = e + 1
                cur_start = k
                j = k
                continue
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == "&" and sep == "&" and depth == 0 and env_depth == 0:
            parts.append(tex[cur_start:j])
            cur_start = j + 1
        j += 1
    parts.append(tex[cur_start:])
    return parts


def strip_cmd_with_arg(tex: str, name: str) -> tuple[str, list[str]]:
    """Remove every ``\\name{arg}`` from tex, returning (new_tex, [args])."""
    args = []
    out = []
    pos = 0
    pat = re.compile(r"\\" + re.escape(name) + r"(?![A-Za-z@])\*?")
    while True:
        m = pat.search(tex, pos)
        if not m:
            out.append(tex[pos:])
            break
        out.append(tex[pos:m.start()])
        arg, end = read_arg(tex, m.end())
        args.append(arg or "")
        pos = end
    return "".join(out), args
