"""Render TeX maths to crisp grayscale PNGs for e-ink.

Primary path: ONE Node.js process running MathJax 4 (TeX -> standalone SVG,
with automatic line-breaking of wide displays and of long inline formulas)
and resvg (SVG -> PNG).  Expressions MathJax rejects are re-tried with real
LaTeX (pdflatex + preview.sty, the document's own macros); if that fails too
the TeX source is shown in monospace.

All sizes are reported in em so the EPUB can size images relative to the
reader's chosen font size.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .util import Math2KindleError, cache_dir, log, run, which

NODE_ASSETS = ("package.json", "package-lock.json", "render.mjs")
# Located via __file__, not importlib.resources.files("math2kindle"): when Python
# runs from a directory that contains the repo checkout (e.g. cwd ~/development,
# which holds math2kindle/ without an __init__.py), the cwd entry on sys.path makes
# the top-level `math2kindle` a namespace package rooted at the repo, and resources
# then point at the wrong directory.  This module's own file is always right.
NODE_SRC = Path(__file__).resolve().parent / "node"
_EMPTY_HASH_DIR = "node-e3b0c44298"   # left behind by builds that hashed missing assets


@dataclass
class Piece:
    file: Path
    w_em: float
    h_em: float
    va_em: float          # CSS vertical-align (negative = below baseline)
    w_px: int
    h_px: int
    full: bool = False    # spans the full display width (tagged equation)


@dataclass
class Block:
    """One display line/row image plus its equation number (HTML text)."""
    piece: Piece
    tag: str | None = None
    align: str = "center"          # center | left | right
    indent: float = 0.0            # em
    alt: str = ""
    autobreak: str | None = None   # row TeX still too wide after our own breaking


@dataclass
class RenderedMath:
    tex: str
    display: bool
    method: str                      # "mathjax" | "latex" | "source"
    pieces: list[Piece] = field(default_factory=list)
    breaks: list[str] = field(default_factory=list)
    error: str | None = None
    blocks: list[Block] = field(default_factory=list)   # displays


@dataclass
class RenderOptions:
    em_px: int = 48                  # raster pixels per em (~300 ppi Kindle text)
    width_em: float = 16.0           # line width for display maths (fits large Kindle fonts too)
    script_min: float = 0.66         # minimum size of 2nd-level scripts (TeX: 0.5), relative to the text
    inline_split_em: float = 9.0     # split inline maths wider than this into pieces
    stroke: float = 12.0             # glyph emboldening for e-ink (font units, 1000/em; 0 = off)
    scale: float = 1.06              # maths size relative to body text (CM has a small x-height)
    keep_svg: bool = False
    latex_fallback: bool = True
    chunk_size: int = 400            # items per Node process


# --------------------------------------------------------------------------- Node runtime

class NodeRuntime:
    """Locates node and makes sure the vendored npm packages are installed."""

    def __init__(self, base: Path | None = None):
        self.node = os.environ.get("MATH2KINDLE_NODE") or which("node")
        if not self.node:
            raise Math2KindleError(
                "Node.js (>= 18) is required to render maths but `node` was not found.\n"
                "  macOS:  brew install node        Linux: sudo apt install nodejs npm\n"
                "  or download from https://nodejs.org/")
        self.dir = base or self._default_dir()

    @staticmethod
    def _asset(name: str) -> bytes:
        path = NODE_SRC / name
        try:
            data = path.read_bytes()
        except OSError as e:
            raise Math2KindleError(
                f"vendored renderer file missing: {path} ({e.strerror or e}).\n"
                "  The math2kindle checkout is incomplete; re-extract it and re-run the installer.") from e
        if not data.strip():
            raise Math2KindleError(f"vendored renderer file is empty: {path}")
        return data

    def _default_dir(self) -> Path:
        override = os.environ.get("MATH2KINDLE_NODE_DIR")
        if override:
            return Path(override).expanduser()
        base = cache_dir()
        stale = base / _EMPTY_HASH_DIR
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        digest = hashlib.sha256(self._asset("package.json")).hexdigest()[:10]
        return base / f"node-{digest}"

    def ensure(self) -> Path:
        d = self.dir
        d.mkdir(parents=True, exist_ok=True)
        for name in NODE_ASSETS:
            data = self._asset(name)
            target = d / name
            if not target.exists() or target.read_bytes() != data:
                target.write_bytes(data)
        if not (d / "node_modules" / "@mathjax" / "src").is_dir() or \
                not (d / "node_modules" / "@resvg" / "resvg-js").is_dir():
            npm = which("npm")
            if not npm:
                raise Math2KindleError(
                    "npm not found; it is needed once to install MathJax into "
                    f"{d}. Install Node.js (which includes npm) and retry.")
            log.info("Installing MathJax 4 + resvg into %s (one-off, ~120 MB)...", d)
            cmd = [npm, "ci" if (d / "package-lock.json").exists() else "install",
                   "--no-audit", "--no-fund", "--omit=dev", "--loglevel=error"]
            proc = run(cmd, cwd=d, timeout=900)
            if proc.returncode != 0 and cmd[1] == "ci":
                cmd[1] = "install"
                proc = run(cmd, cwd=d, timeout=900)
            if proc.returncode != 0:
                raise Math2KindleError(
                    "npm install failed in %s:\n%s" % (d, proc.stderr.decode("utf-8", "replace")[-3000:]))
        return d

    @property
    def script(self) -> Path:
        return self.dir / "render.mjs"


# --------------------------------------------------------------------------- MathJax batch

def split_single_tag(tex: str) -> tuple[str, str, str] | None:
    """For a one-line display with exactly one ``\\tag``, return
    (tex_without_tag, tag_command, printed_label) so the renderer can move the
    number below the formula when both do not fit on one line."""
    from .latex.texscan import match_brace, split_top_level

    tags = list(re.finditer(r"\\tag(\*?)\s*\{", tex))
    if len(tags) != 1 or tex.lstrip().startswith("\\begin{"):
        return None
    if len(split_top_level(tex, "\\\\")) > 1:
        return None
    m = tags[0]
    e = match_brace(tex, m.end() - 1)
    arg = tex[m.end():e]
    body = (tex[:m.start()] + tex[e + 1:]).strip()
    if not body:
        return None
    label = arg if m.group(1) else "(%s)" % arg
    return body, tex[m.start():e + 1], label


def _png_to_gray(path: Path) -> None:
    from PIL import Image

    with Image.open(path) as im:
        g = im.convert("L")
    g.save(path, optimize=True)


class MathRenderer:
    def __init__(self, options: RenderOptions | None = None, *, macros: list[str] | None = None,
                 latex_preamble: "LatexPreamble | None" = None, out_dir: Path,
                 runtime: NodeRuntime | None = None):
        self.opt = options or RenderOptions()
        self.macros = list(macros or [])
        self.latex_preamble = latex_preamble
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = runtime
        self.macro_errors: list[dict] = []
        try:
            from .latex.macros import harvest

            self._macroset, _ = harvest("\n".join(m.lstrip("%") for m in self.macros))
        except Exception:  # pragma: no cover
            self._macroset = None
        self.stats = {"mathjax": 0, "latex": 0, "source": 0}

    # public ---------------------------------------------------------------
    def render_all(self, items: dict[str, tuple[str, bool]]) -> dict[str, RenderedMath]:
        """items: id -> (tex, display).  Returns id -> RenderedMath."""
        results: dict[str, RenderedMath] = {}
        if not items:
            return results
        runtime = self.runtime or NodeRuntime()
        runtime.ensure()
        # Displays and inline maths go to separate Node processes: an inline
        # conversion leaves MathJax 4 state behind that makes later display
        # measurements slightly too narrow (closing delimiters got clipped).
        for want_display in (False, True):
            pending = [(k, v) for k, v in items.items() if bool(v[1]) == want_display]
            for start in range(0, len(pending), self.opt.chunk_size):
                chunk = pending[start:start + self.opt.chunk_size]
                results.update(self._run_chunk(runtime, chunk))
        self._autobreak(runtime, results)
        failed = {k: v for k, v in results.items() if v.method != "mathjax"}
        if failed:
            log.info("MathJax could not render %d of %d expressions; trying LaTeX fallback",
                        len(failed), len(items))
            for k, r in failed.items():
                log.debug("MathJax error for %r: %s", r.tex[:120], r.error)
            self._latex_fallback(failed)
        for r in results.values():
            self.stats[r.method] += 1
            for p in r.pieces + [b.piece for b in r.blocks]:
                _png_to_gray(p.file)
            if r.display and r.method == "mathjax":
                for b in r.blocks:
                    if b.piece.w_em > self.opt.width_em * 1.3:
                        log.warning("Display too wide to break nicely (%.1f em); it will be scaled to the page: %s",
                                    b.piece.w_em, (b.alt or r.tex)[:80].replace("\n", " "))
        return results

    # internals ------------------------------------------------------------
    def _run_chunk(self, runtime: NodeRuntime, chunk: list[tuple[str, tuple[str, bool]]]) -> dict[str, RenderedMath]:
        out: dict[str, RenderedMath] = {}
        todo = list(chunk)
        attempts = 0
        while todo and attempts < 50:
            attempts += 1
            job = {
                "config": {
                    "em_px": self.opt.em_px, "width_em": self.opt.width_em,
                    "inline_split_em": self.opt.inline_split_em, "stroke": self.opt.stroke,
                    "scale": self.opt.scale, "script_min": self.opt.script_min,
                    "out_dir": str(self.out_dir), "keep_svg": self.opt.keep_svg,
                },
                "macros": self.macros,
                "items": [self._job_item(k, t, d) for k, (t, d) in todo],
            }
            timeout = 120 + 2.0 * len(todo)
            try:
                proc = subprocess.run(
                    [runtime.node, str(runtime.script)], input=json.dumps(job).encode("utf-8"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(runtime.dir), timeout=timeout)
                stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as e:
                stdout, stderr, rc = e.stdout or b"", e.stderr or b"", -1
            got = self._parse_output(stdout, dict(todo))
            out.update(got)
            remaining = [(k, v) for k, v in todo if k not in out]
            if not remaining:
                break
            # The process died (or timed out) on the first remaining item:
            # mark that one as failed and carry on with the rest.
            k, (t, d) = remaining[0]
            err = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["node renderer crashed"]
            if rc != 0 and not got and attempts == 1 and b"Cannot find" in stderr:
                raise Math2KindleError("Node renderer failed to start:\n" + stderr.decode("utf-8", "replace")[-2000:])
            out[k] = RenderedMath(tex=t, display=d, method="source", error=err[0])
            todo = remaining[1:]
        return out

    def _job_item(self, k: str, tex: str, display: bool) -> dict:
        item = {"id": k, "tex": tex, "display": display}
        if display:
            try:
                from .displaymath import display_spec

                item["spec"] = display_spec(tex, getattr(self, "_macroset", None))
            except Exception as e:  # pragma: no cover - never let layout analysis kill a render
                log.debug("display analysis failed for %r: %s", tex[:80], e)
        return item

    def _autobreak(self, runtime: NodeRuntime, results: dict[str, RenderedMath]) -> None:
        """Rows our breaker could not fit: try MathJax 4's automatic line
        breaking, one fresh Node process each (that mode taints MathJax state)."""
        todo = [(k, i, b) for k, r in results.items() for i, b in enumerate(r.blocks) if b.autobreak]
        for n, (k, i, b) in enumerate(todo[:40]):
            ident = f"{k}-ab{i}"
            job = {"config": {"em_px": self.opt.em_px, "width_em": self.opt.width_em, "stroke": self.opt.stroke,
                              "scale": self.opt.scale, "script_min": self.opt.script_min,
                              "out_dir": str(self.out_dir), "autobreak": True},
                   "macros": self.macros, "items": [{"id": ident, "tex": b.autobreak, "display": True}]}
            try:
                proc = subprocess.run([runtime.node, str(runtime.script)], input=json.dumps(job).encode("utf-8"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(runtime.dir),
                                      timeout=120)
            except subprocess.TimeoutExpired:
                continue
            got = self._parse_output(proc.stdout, {ident: (b.autobreak, True)})
            r = got.get(ident)
            if r and r.method == "mathjax" and r.pieces and r.pieces[0].w_px < b.piece.w_px:
                b.piece = r.pieces[0]
            b.autobreak = None

    def _parse_output(self, stdout: bytes, items: dict[str, tuple[str, bool]]) -> dict[str, RenderedMath]:
        got: dict[str, RenderedMath] = {}
        for line in stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "ready":
                if msg.get("macro_errors") and not self.macro_errors:
                    self.macro_errors = msg["macro_errors"]
                    for e in self.macro_errors[:10]:
                        log.warning("MathJax could not define macro %s: %s", e["def"][:80], e["error"])
            elif msg.get("type") == "result" and msg.get("id") in items:
                tex, disp = items[msg["id"]]
                if msg.get("ok"):
                    pieces = [Piece(file=Path(p["file"]), w_em=p["w_em"], h_em=p["h_em"], va_em=p["va_em"],
                                    w_px=p["w_px"], h_px=p["h_px"], full=bool(p.get("full")))
                              for p in msg["pieces"]]
                    blocks = [Block(Piece(file=Path(b["file"]), w_em=b["w_em"], h_em=b["h_em"], va_em=0.0,
                                          w_px=b["w_px"], h_px=b["h_px"]),
                                    tag=b.get("tag"), align=b.get("align", "center"),
                                    indent=float(b.get("indent") or 0), alt=b.get("alt") or "",
                                    autobreak=b.get("autobreak"))
                              for b in msg.get("blocks") or []]
                    got[msg["id"]] = RenderedMath(tex=tex, display=disp, method="mathjax", pieces=pieces,
                                                  breaks=list(msg.get("breaks") or []), blocks=blocks)
                else:
                    got[msg["id"]] = RenderedMath(tex=tex, display=disp, method="source", error=msg.get("error"))
        return got

    def _latex_fallback(self, failed: dict[str, RenderedMath]) -> None:
        if not self.opt.latex_fallback:
            return
        renderer = LatexSnippetRenderer(self.latex_preamble or LatexPreamble(), em_px=self.opt.em_px,
                                        width_em=self.opt.width_em, scale=self.opt.scale)
        if not renderer.available:
            log.warning("pdflatex not found: %d formulas will be shown as TeX source", len(failed))
            return
        for k, r in failed.items():
            piece = renderer.render(r.tex, r.display, self.out_dir / f"{k}-latex.png")
            if piece is not None:
                r.method = "latex"
                r.pieces = [piece]
            else:
                log.warning("Could not render formula; showing TeX source: %s", r.tex[:100].replace("\n", " "))


# --------------------------------------------------------------------------- LaTeX fallback

SAFE_TEX_ENV = {"openout_any": "p", "openin_any": "p", "shell_escape": "f"}

# Packages that are pointless or harmful in a one-formula preview document.
PACKAGE_DENYLIST = {
    "hyperref", "cleveref", "biblatex", "natbib", "geometry", "fullpage", "a4wide", "fancyhdr",
    "titlesec", "titling", "setspace", "showkeys", "showlabels", "lineno", "draftwatermark",
    "background", "tocloft", "babel", "polyglossia", "fontspec", "unicode-math", "hyperxmp",
    "pdfpages", "microtype", "todonotes", "fixme", "refcheck", "showframe", "vmargin", "anysize",
    "crop", "layout", "caption", "subcaption", "subfig", "float", "wrapfig", "sectsty", "abstract",
    "authblk", "footmisc", "endnotes", "bookmark", "doi", "url", "xr", "xr-hyper", "nameref",
    "backref", "cite", "comment", "verbatim", "listings", "minted", "algorithm2e", "algorithmic",
    "algorithmicx", "algpseudocode", "multicol", "parskip", "indentfirst", "lipsum", "blindtext",
    "fancyvrb", "imakeidx", "makeidx", "glossaries", "nomencl", "biblatex-chicago", "csquotes",
    "times", "mathptmx", "newtxtext", "newtxmath", "palatino", "mathpazo", "kpfonts", "libertine",
    "lmodern", "charter", "fourier", "txfonts", "pxfonts", "ebgaramond", "garamondx", "mlmodern",
}


@dataclass
class LatexPreamble:
    packages: list[str] = field(default_factory=list)   # full \usepackage lines
    definitions: list[str] = field(default_factory=list)  # macro definitions
    extra: list[str] = field(default_factory=list)        # \usetikzlibrary etc.
    support_files: list[Path] = field(default_factory=list)  # local .sty/.tex copied in

    def candidates(self) -> list[str]:
        base = ["\\usepackage{amsmath,amssymb,amsthm,mathtools}"]
        full = base + self.packages + self.extra + self.definitions
        no_pkgs = base + ["\\usepackage{tikz}", "\\usepackage{tikz-cd}"] + self.definitions
        minimal = base
        out = []
        for c in (full, no_pkgs, minimal):
            s = "\n".join(c)
            if s not in out:
                out.append(s)
        return out


class LatexSnippetRenderer:
    """Compile single formulas / pictures with pdflatex + preview.sty."""

    def __init__(self, preamble: LatexPreamble, *, em_px: int = 48, width_em: float = 24.0, scale: float = 1.0):
        self.preamble = preamble
        self.em_px = em_px
        self.scale = scale
        self.width_pt = width_em * 10.0 / scale   # 10pt document => 1em = 10pt
        self.pdflatex = which("pdflatex")
        self._good: list[str] | None = None

    @property
    def available(self) -> bool:
        return self.pdflatex is not None

    def _ordered_preambles(self) -> list[str]:
        if self._good is None:
            cands = self.preamble.candidates()
            ok = [c for c in cands if self._compile(c, "$x$", probe=True) is not None]
            self._good = ok or cands[-1:]
        return self._good

    def _doc(self, preamble: str, body: str) -> str:
        return (
            "\\documentclass[10pt]{article}\n"
            "\\nonstopmode\n"
            f"{preamble}\n"
            "\\usepackage[active,tightpage]{preview}\n"
            "\\setlength\\PreviewBorder{0pt}\n"
            f"\\textwidth={self.width_pt:.1f}pt\n"
            "\\pagestyle{empty}\n"
            "\\begin{document}\n"
            f"{body}\n"
            "\\end{document}\n"
        )

    def _compile(self, preamble: str, body: str, probe: bool = False):
        with tempfile.TemporaryDirectory(prefix="m2k-tex-") as td:
            tdp = Path(td)
            for f in self.preamble.support_files:
                try:
                    shutil.copy(f, tdp / f.name)
                except OSError:
                    pass
            (tdp / "s.tex").write_text(self._doc(preamble, body), encoding="utf-8")
            try:
                proc = run([self.pdflatex, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
                            "s.tex"], cwd=tdp, timeout=60, env=SAFE_TEX_ENV)
            except subprocess.TimeoutExpired:
                return None
            pdf = tdp / "s.pdf"
            if proc.returncode != 0 or not pdf.exists():
                return None
            if probe:
                return True
            logtxt = (tdp / "s.log").read_text("latin-1", "replace")
            return pdf.read_bytes(), logtxt

    def _wrap(self, tex: str, display: bool, picture: bool) -> str:
        if picture:
            return "\\begin{preview}%s\\end{preview}" % tex
        if not display:
            return "\\begin{preview}$%s$\\end{preview}" % tex
        t = tex.strip()
        if not re.match(r"\\begin\{(align|gather|multline|flalign|alignat|equation|eqnarray)\*?\}", t):
            t = "\\[ %s \\]" % t
        return ("\\begin{preview}\\begin{minipage}{%.1fpt}\\centering %s\\end{minipage}\\end{preview}"
                % (self.width_pt, t))

    def render(self, tex: str, display: bool, out_png: Path, *, picture: bool = False) -> Piece | None:
        body = self._wrap(tex, display, picture)
        for pre in self._ordered_preambles():
            res = self._compile(pre, body)
            if res is not None:
                pdf_bytes, logtxt = res
                try:
                    return self._rasterize(pdf_bytes, logtxt, out_png, display=display, picture=picture,
                                           tagged="\\tag" in tex or "\\label" in tex)
                except Exception as e:  # pragma: no cover - defensive
                    log.debug("rasterize failed: %s", e)
                    return None
        return None

    def _rasterize(self, pdf_bytes: bytes, logtxt: str, out_png: Path, *, display: bool, picture: bool,
                   tagged: bool) -> Piece:
        import pymupdf
        from PIL import Image

        depth_pt = 0.0
        m = re.search(r"Preview: Snippet 1 ended\.\((\d+)\+(\d+)x(\d+)\)", logtxt)
        if m:
            depth_pt = int(m.group(2)) / 65536.0
        zoom = self.em_px * self.scale / 10.0
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_png))
        doc.close()
        full = False
        if display or picture:
            with Image.open(out_png) as im:
                g = im.convert("L")
            if display and tagged:
                full = True
            else:
                from PIL import ImageOps

                bbox = ImageOps.invert(g).getbbox()
                if bbox:
                    pad = max(2, self.em_px // 8)
                    g = g.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                                min(g.width, bbox[2] + pad), min(g.height, bbox[3] + pad)))
            g.save(out_png, optimize=True)
        with Image.open(out_png) as im:
            w, h = im.size
        return Piece(file=out_png, w_em=w / self.em_px, h_em=h / self.em_px,
                     va_em=-(depth_pt / 10.0) * self.scale if not display else 0.0, w_px=w, h_px=h, full=full)
