"""Thin, robust wrappers around the pandoc executable."""

from __future__ import annotations

import html
import re
import subprocess

from .util import Math2KindleError, log, pandoc_path

LATEX_WRAPPER = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def _pandoc(src: str, fmt_from: str, extra: list[str] | None = None, timeout: int = 300) -> tuple[int, str, str]:
    cmd = [pandoc_path(), "-f", fmt_from, "-t", "html5", "--wrap=none", "--mathjax"] + (extra or [])
    try:
        proc = subprocess.run(cmd, input=src.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", "pandoc timed out"
    except FileNotFoundError as e:
        raise Math2KindleError(f"pandoc not runnable: {e}")
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def _plain_fallback(chunk: str) -> str:
    """Last resort for a chunk pandoc cannot parse: strip commands, keep text."""
    t = re.sub(r"\\begin\{[^}]*\}|\\end\{[^}]*\}", "\n\n", chunk)
    t = re.sub(r"\\(section|subsection|subsubsection)\*?\{([^{}]*)\}", r"\n\n@@H@@\2\n\n", t)
    t = re.sub(r"\\[A-Za-z@]+\*?(\[[^\]]*\])?", " ", t)
    t = t.replace("{", "").replace("}", "").replace("~", " ")
    out = []
    for para in re.split(r"\n\s*\n", t):
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        if para.startswith("@@H@@"):
            out.append("<h2>%s</h2>" % html.escape(para[5:]))
        else:
            out.append("<p>%s</p>" % html.escape(para))
    return "\n".join(out)


def latex_to_html(body: str) -> str:
    """Convert a (pre-processed) LaTeX body to an HTML5 fragment.

    If pandoc rejects the whole document, retry section by section and fall
    back to plain text for any section it still rejects."""
    rc, out, err = _pandoc(LATEX_WRAPPER % body, "latex")
    if rc == 0:
        if err.strip():
            log.debug("pandoc: %s", err.strip()[:2000])
        return out
    log.warning("pandoc failed on the whole document (%s); converting section by section",
                err.strip().splitlines()[-1:] or "?")
    chunks = re.split(r"(?=\n\n\\(?:section|subsection)\*\{)", body)
    parts = []
    for ch in chunks:
        rc, out, err = _pandoc(LATEX_WRAPPER % ch, "latex")
        if rc == 0:
            parts.append(out)
            continue
        # try paragraph by paragraph
        sub = []
        for para in re.split(r"\n\s*\n", ch):
            rc2, out2, _ = _pandoc(LATEX_WRAPPER % para, "latex", timeout=60)
            sub.append(out2 if rc2 == 0 else _plain_fallback(para))
        log.warning("pandoc could not parse a section; degraded to paragraph-level conversion")
        parts.append("\n".join(sub))
    return "\n".join(parts)


def markdown_to_html(md: str) -> str:
    fmt = "markdown+tex_math_dollars+tex_math_single_backslash+raw_html+pipe_tables-implicit_figures"
    rc, out, err = _pandoc(md, fmt)
    if rc != 0:
        raise Math2KindleError("pandoc failed to convert markdown: " + err[-2000:])
    return out
