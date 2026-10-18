"""Locate the main .tex file of an (arXiv-style) source tree and flatten it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..util import log
from .texscan import read_arg, read_opt, strip_comments

DOCCLASS_RE = re.compile(r"\\document(class|style)\s*(\[[^\]]*\])?\s*\{")
BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")


def _read(p: Path) -> str:
    data = p.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def find_main_tex(root: Path) -> Path:
    """Pick the main file: has \\documentclass and \\begin{document};
    prefer one whose stem matches a .bbl, then conventional names, then size."""
    root = Path(root)
    cands = []
    for p in sorted(root.rglob("*.tex")):
        if not p.is_file():
            continue
        try:
            txt = strip_comments(_read(p))
        except OSError:
            continue
        if DOCCLASS_RE.search(txt) and BEGIN_DOC_RE.search(txt):
            cands.append(p)
    if not cands:
        # 00README.XXX (arXiv) may name the toplevel file
        for readme in list(root.glob("00README*")):
            m = re.search(r"^\s*(\S+\.tex)\s+toplevelfile", _read(readme), re.M)
            if m and (root / m.group(1)).exists():
                return root / m.group(1)
        texs = sorted(root.rglob("*.tex"), key=lambda q: q.stat().st_size, reverse=True)
        if texs:
            log.warning("No file with \\documentclass + \\begin{document}; guessing %s", texs[0].name)
            return texs[0]
        raise FileNotFoundError(f"no .tex files under {root}")
    if len(cands) == 1:
        return cands[0]
    bbl_stems = {p.stem for p in root.rglob("*.bbl")}

    def score(p: Path):
        s = 0
        if p.stem in bbl_stems:
            s += 100
        if p.stem.lower() in {"main", "ms", "paper", "article", "manuscript", "arxiv"}:
            s += 10
        s -= len(p.relative_to(root).parts) * 5      # prefer top level
        return (s, p.stat().st_size)

    best = max(cands, key=score)
    log.info("Main TeX file: %s (of %d candidates)", best.relative_to(root), len(cands))
    return best


INPUT_RE = re.compile(r"\\(input|include|subfile|import|subimport|includefrom|subincludefrom)(?![A-Za-z@])")


@dataclass
class FlatSource:
    text: str
    root: Path
    main: Path
    local_styles: list[Path] = field(default_factory=list)
    files_read: list[Path] = field(default_factory=list)


def _resolve(root: Path, base: Path, name: str, exts=(".tex", "")) -> Path | None:
    name = name.strip().strip('"')
    if not name:
        return None
    for d in (base, root):
        for ext in exts:
            p = (d / (name + ext)) if not name.endswith(ext) or not ext else (d / name)
            try:
                rp = p.resolve()
            except OSError:
                continue
            # Never read outside the source tree (path traversal from \input{../../..}).
            if root.resolve() not in rp.parents and rp != root.resolve():
                continue
            if rp.is_file():
                return rp
    return None


def flatten(main: Path, root: Path | None = None, *, max_depth: int = 25) -> FlatSource:
    main = Path(main).resolve()
    root = (Path(root) if root else main.parent).resolve()
    files: list[Path] = []
    styles: list[Path] = []

    def expand(text: str, base: Path, depth: int) -> str:
        text = strip_comments(text)
        if depth > max_depth:
            return text
        out = []
        pos = 0
        for m in INPUT_RE.finditer(text):
            if m.start() < pos:
                continue
            cmd = m.group(1)
            j = m.end()
            sub_base = base
            if cmd in ("import", "subimport", "includefrom", "subincludefrom"):
                d, j = read_arg(text, j)
                if d is None:
                    continue
                sub_base = (base / d) if cmd.startswith("sub") else (root / d)
            # \input file.tex (TeX primitive syntax without braces)
            k = j
            while k < len(text) and text[k] in " \t":
                k += 1
            if k < len(text) and text[k] == "{":
                name, j2 = read_arg(text, k)
            elif cmd == "input":
                mm = re.match(r"[^\s{}\\%]+", text[k:])
                if not mm:
                    continue
                name, j2 = mm.group(0), k + mm.end()
            else:
                continue
            if name is None:
                continue
            p = _resolve(root, sub_base, name)
            out.append(text[pos:m.start()])
            if p is None:
                if not name.endswith((".sty", ".cls", ".bbl", ".clo")):
                    log.warning("\\%s{%s}: file not found, skipped", cmd, name)
                pos = j2
                continue
            if p.suffix in (".bbl",):
                out.append(_read(p))
            else:
                files.append(p)
                out.append("\n" + expand(_read(p), p.parent, depth + 1) + "\n")
            if cmd == "include":
                out.append("\n")
            pos = j2
        out.append(text[pos:])
        return "".join(out)

    files.append(main)
    text = expand(_read(main), main.parent, 0)
    # Local style files (\usepackage{mymacros} with mymacros.sty in the tree):
    # their definitions are harvested later, never executed by us.
    pre_end = BEGIN_DOC_RE.search(text)
    preamble = text[: pre_end.start()] if pre_end else text
    for m in re.finditer(r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", preamble):
        for name in m.group(1).split(","):
            p = _resolve(root, main.parent, name.strip(), exts=(".sty",))
            if p and p not in styles:
                styles.append(p)
    return FlatSource(text=text, root=root, main=main, local_styles=styles, files_read=files)


def split_preamble(text: str) -> tuple[str, str]:
    """Return (preamble, body) split at \\begin{document} ... \\end{document}."""
    m = BEGIN_DOC_RE.search(text)
    if not m:
        return "", text
    pre = text[: m.start()]
    body = text[m.end():]
    e = re.search(r"\\end\s*\{document\}", body)
    if e:
        body = body[: e.start()]
    return pre, body


def document_class(preamble: str) -> tuple[str, str]:
    m = re.search(r"\\document(?:class|style)\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}", preamble)
    if not m:
        return "article", ""
    return m.group(2).strip(), (m.group(1) or "")


def read_bbl_for(main: Path, root: Path) -> str | None:
    """The .bbl matching the main file (or the only .bbl in the tree)."""
    p = main.with_suffix(".bbl")
    if p.exists():
        return _read(p)
    bbls = sorted(root.rglob("*.bbl"))
    if len(bbls) == 1:
        return _read(bbls[0])
    return None


__all__ = ["find_main_tex", "flatten", "split_preamble", "document_class", "read_bbl_for", "FlatSource",
           "read_opt"]
