"""Small shared helpers: logging, cache dirs, external tools, filenames."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

log = logging.getLogger("math2kindle")


class Math2KindleError(RuntimeError):
    """A user-facing error (bad input, missing tool, ...)."""


def cache_dir() -> Path:
    """Per-user cache directory (node_modules for the renderer, etc.)."""
    env = os.environ.get("MATH2KINDLE_CACHE")
    if env:
        p = Path(env).expanduser()
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Caches" / "math2kindle"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        p = Path(base) / "math2kindle"
    p.mkdir(parents=True, exist_ok=True)
    return p


def which(name: str) -> str | None:
    return shutil.which(name)


def run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 120, env: dict | None = None,
        input: bytes | None = None, check: bool = False) -> subprocess.CompletedProcess:
    """subprocess.run with captured output and a timeout (never uses a shell)."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None, timeout=timeout, env=full_env,
        input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise Math2KindleError(
            f"{Path(str(cmd[0])).name} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[-2000:]}")
    return proc


def pandoc_path() -> str:
    """Prefer the pandoc bundled by pypandoc_binary (newer), else the one on PATH."""
    env = os.environ.get("MATH2KINDLE_PANDOC")
    if env:
        return env
    try:
        import pypandoc  # type: ignore

        p = pypandoc.get_pandoc_path()
        if p and Path(p).exists():
            return str(p)
    except Exception:  # pragma: no cover - depends on install
        pass
    p = shutil.which("pandoc")
    if not p:
        raise Math2KindleError(
            "pandoc not found. Install it (macOS: `brew install pandoc`; Debian/Ubuntu: "
            "`sudo apt install pandoc`) or `pip install pypandoc_binary`.")
    return p


_GREEK = {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda",
          "mu", "nu", "xi", "pi", "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega"}


def _greek_name(ch: str) -> str:
    if "\u0370" <= ch <= "\u03ff":
        try:
            name = unicodedata.name(ch)
        except ValueError:
            return ch
        m = re.match(r"GREEK (CAPITAL|SMALL) LETTER (\w+)$", name)
        if m:
            w = m.group(2).lower().replace("lamda", "lambda")
            return w.capitalize() if m.group(1) == "CAPITAL" else w
    return ch


def sanitize_filename(title: str, *, max_len: int = 90, default: str = "paper") -> str:
    """Make a portable file name (no extension) from a paper title."""
    t = re.sub(r"\\(?:mathbb|mathcal|mathrm|mathbf|mathfrak|operatorname|text\w*|emph)\s*", "", title or "")
    t = re.sub(r"\\([A-Za-z]+)\*?", lambda m: m.group(1) if m.group(1)[0].isupper() or len(m.group(1)) > 2 and
               m.group(1).lower() in _GREEK else "", t)      # \Lambda -> Lambda, other control words dropped
    t = t.replace("$", "")
    t = "".join(_greek_name(ch) for ch in t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[{}^_~\\]", "", t)
    t = re.sub(r"[^A-Za-z0-9 .,()'&+-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,-")
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0].rstrip(" .,-")
    return t or default


def tex_to_plain(s: str) -> str:
    """Rough TeX -> plain text, for metadata (title/author strings)."""
    s = s or ""
    s = re.sub(r"\\(thanks|footnote|label)\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", s)
    s = re.sub(r"\\texorpdfstring\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}", r"\2", s)
    s = s.replace("\\\\", " ").replace("~", " ")
    s = re.sub(r"\\(?:emph|textbf|textit|textrm|textsc|mathrm|mathbf|mathcal|mathbb|text|mbox|operatorname)\s*\{", "{", s)
    greek = {
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
        "zeta": "ζ", "eta": "η", "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
        "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Pi": "Π", "Sigma": "Σ",
        "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "ell": "ℓ", "infty": "∞", "le": "≤", "ge": "≥",
        "leq": "≤", "geq": "≥", "times": "×", "cdot": "·", "ldots": "…", "dots": "…",
    }
    s = re.sub(r"\\([A-Za-z]+)", lambda m: greek.get(m.group(1), ""), s)
    s = re.sub(r"\\(.)", r"\1", s)
    s = s.replace("$", "").replace("{", "").replace("}", "")
    s = s.replace("``", "\u201c").replace("''", "\u201d").replace("--", "\u2013")
    return re.sub(r"\s+", " ", s).strip()
