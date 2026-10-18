"""Maths rendering: MathJax 4 batch renderer, tag layout, fallbacks."""

from __future__ import annotations

import pytest
from PIL import Image

from math2kindle.mathrender import (LatexPreamble, MathRenderer, RenderOptions, split_single_tag)

from .conftest import requires_node, requires_pdflatex


def test_split_single_tag():
    assert split_single_tag(r"E=mc^2 \tag{1.1}") == ("E=mc^2", r"\tag{1.1}", "(1.1)")
    assert split_single_tag(r"x \tag*{(A)}") == ("x", r"\tag*{(A)}", "(A)")
    assert split_single_tag(r"\begin{align*} a\tag{1}\end{align*}") is None
    assert split_single_tag(r"a \tag{1} \\ b \tag{2}") is None
    assert split_single_tag("x") is None


HEAT = (r"\begin{multline*}\operatorname{tr} e^{-t\Delta_1} = (4\pi t)^{-n/2}\Bigl( n\vol(M) + \frac{t}{6}"
        r"\int_M \bigl(n\,\mathrm{scal} - 6\,\mathrm{scal}\bigr)\,d\vol_g \\ + \frac{t^2}{360}\int_M \bigl( 5n\,"
        r"\mathrm{scal}^2 - 2n\abs{\Ric}^2 + 2n\abs{R}^2 - 60\,\mathrm{scal}^2 + 180\abs{\Ric}^2 - 30\abs{R}^2\bigr)"
        r"\,d\vol_g + O(t^3)\Bigr). \tag{1.4}\end{multline*}")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("math")
    r = MathRenderer(RenderOptions(latex_fallback=False), out_dir=out,
                     macros=[r"\newcommand{\Z}{\mathbb{Z}}", r"\DeclareMathOperator{\li}{li}",
                             r"\DeclareMathOperator{\vol}{vol}", r"\newcommand{\Ric}{\operatorname{Ric}}",
                             r"\newcommand{\abs}[1]{\lvert #1\rvert}",
                             r"\def\bigO#1{O\!\left(#1\right)}"])
    items = {
        "inline": (r"x\in\Z", False),
        "frac": (r"\frac{a}{b}", False),
        "long": (r"\sum_{n\le x}\Lambda(n)\log\frac{x}{n} = x + O\bigl(x e^{-c\sqrt{\log x}}\bigr) "
                 r"+ \sum_{p\le x}\frac{\log p}{p} - \li(x)", False),
        "tag": (r"\psi(x)=\sum_{n\le x}\Lambda(n) \tag{1.1}", True),
        "tagwide": (r"\psi(x+h)-\psi(x) = \frac{h}{\varphi(q)}\left(1+O\left(\exp\left(-c\frac{\log x}"
                    r"{(\log\log x)^{2/3}}\right)\right)\right) \tag{3.3}", True),
        "plain": (r"\int_0^1 f(t)\,dt", True),
        "bad": (r"\undefinedcommand{x}", False),
        "heat": (HEAT, True),
        "gather": (r"\begin{gather*} a = b \tag{1}\\ c = d \tag{2}\end{gather*}", True),
        "alignfit": (r"\begin{align*} \psi(x) &= \sum_{n\le x}\Lambda(n) \tag{2.1}\\ "
                     r"&\le x\log x \tag{2.2}\end{align*}", True),
        "bigo": (r"\sum_{n\le x}' a_n = \frac{1}{2\pi i}\int_{c-iT}^{c+iT}F(s)\frac{x^{s}}{s}\,ds + \bigO{\frac{x}{T}"
                 r"\sum_{n=1}^{\infty}\frac{|a_n|}{n^{c}} + \frac{x A(2x)\log x}{T} + A(N)\min\left(1,"
                 r"\frac{x}{T\langle x\rangle}\right)} \tag{2.4}", True),
        "expfrac": (r"e^{-\frac{d(x,y)^2}{4t}}", False),
    }
    return r, r.render_all(items)


@requires_node
def test_inline_metrics_and_png(rendered):
    r, res = rendered
    inl = res["inline"]
    assert inl.method == "mathjax" and len(inl.pieces) == 1
    p = inl.pieces[0]
    assert 1.0 < p.w_em < 5 and 0.6 < p.h_em < 1.5
    assert -0.4 < p.va_em < 0.05           # sits on the baseline, small descent
    with Image.open(p.file) as im:
        assert im.mode == "L"               # grayscale
        assert im.getpixel((0, 0)) == 255   # opaque white background (visible in dark mode)
        assert min(im.getdata()) < 40       # real black ink
        assert abs(im.height / 48 - p.h_em) < 0.05
    frac = res["frac"].pieces[0]
    assert frac.va_em < -0.2                # fractions hang below the baseline


@requires_node
def test_long_inline_is_split_into_pieces(rendered):
    r, res = rendered
    long = res["long"]
    assert long.method == "mathjax"
    assert len(long.pieces) >= 2
    assert len(long.breaks) == len(long.pieces) - 1
    assert all(p.w_em < 12 for p in long.pieces)


@requires_node
def test_equation_numbers_are_text_not_pixels(rendered):
    r, res = rendered
    tag = res["tag"].blocks
    assert len(tag) == 1 and tag[0].tag == "(1.1)"
    assert tag[0].piece.w_em < 8                     # natural width, not a full-line image
    plain = res["plain"].blocks
    assert len(plain) == 1 and plain[0].tag is None
    g = res["gather"].blocks
    assert [b.tag for b in g] == ["(1)", "(2)"]
    al = res["alignfit"].blocks                      # fits: rows padded to common column widths
    assert [b.tag for b in al] == ["(2.1)", "(2.2)"]
    assert abs(al[0].piece.w_px - al[1].piece.w_px) <= 2


@requires_node
def test_wide_displays_are_broken_to_the_line_width(rendered):
    r, res = rendered
    W = r.opt.width_em
    for key in ("tagwide", "heat", "bigo"):
        blocks = res[key].blocks
        assert blocks, key
        for b in blocks:
            assert b.indent + b.piece.w_em <= W * 1.5, (key, b.indent, b.piece.w_em)
        # the number always sits on the last line of the display
        assert [b.tag for b in blocks[:-1]] == [None] * (len(blocks) - 1), key
    heat = res["heat"].blocks
    assert len(heat) >= 4 and heat[-1].tag == "(1.4)"
    assert heat[0].indent == 0 and all(b.indent > 0 for b in heat[1:])   # continuation lines indented
    assert res["tagwide"].blocks[-1].tag == "(3.3)"
    # (3.3): broken at the relation only, never inside the bracket group
    assert len(res["tagwide"].blocks) <= 2


@requires_node
def test_break_inside_brackets_indents_past_relation(rendered):
    """A line that starts inside a bracket group is never at or left of the '=' column."""
    r, res = rendered
    bigo = res["bigo"].blocks
    assert len(bigo) >= 2 and bigo[-1].tag == "(2.4)"
    rel = bigo[1].indent                     # second line starts with the relation or a top-level +
    assert all(b.indent >= rel for b in bigo[1:])


@requires_node
def test_qquad_gap_is_kept_or_used_as_break(tmp_path):
    tex = (r"g = \begin{pmatrix}1&0&0\\0&1&0\\0&0&1\end{pmatrix}, \qquad \mathrm{II} = "
           r"\begin{bmatrix}h_{11}&h_{12}\\h_{21}&h_{22}\end{bmatrix}, \qquad \mathcal{L}_X g = \nabla X^\flat "
           r"+ (\nabla X^\flat)^T + \frac{a+b}{c+d} + \frac{e+f}{g+h}.")
    narrow = MathRenderer(RenderOptions(latex_fallback=False), out_dir=tmp_path / "n")
    wide = MathRenderer(RenderOptions(latex_fallback=False, width_em=40), out_dir=tmp_path / "w")
    nb = narrow.render_all({"m": (tex, True)})["m"].blocks
    wb = wide.render_all({"m": (tex, True)})["m"].blocks
    assert len(nb) >= 3                       # broken first at the two \qquad gaps
    assert nb[0].piece.w_em < 9 and nb[1].piece.w_em < 9
    assert len(wb) == 1                       # fits: one image, gaps kept
    # the joined image is at least two quads wider than its parts laid side by side
    assert wb[0].piece.w_em > sum(b.piece.w_em for b in nb) + 1.5


@requires_node
def test_minimum_script_size(rendered, tmp_path):
    r, res = rendered
    small = MathRenderer(RenderOptions(latex_fallback=False, script_min=0.4), out_dir=tmp_path)
    ref = small.render_all({"e": (r"e^{-\frac{d(x,y)^2}{4t}}", False)})["e"].pieces[0]
    assert res["expfrac"].pieces[0].h_em > ref.h_em * 1.08


@requires_node
def test_failure_falls_back_to_source(rendered):
    r, res = rendered
    assert res["bad"].method == "source"
    assert "Undefined control sequence" in (res["bad"].error or "")
    assert r.stats["source"] == 1
@requires_node
@requires_pdflatex
def test_latex_fallback_renders_what_mathjax_cannot(tmp_path):
    pre = LatexPreamble(packages=[r"\usepackage{tikz-cd}"], definitions=[r"\newcommand{\cA}{\mathcal{A}}"])
    r = MathRenderer(RenderOptions(), out_dir=tmp_path, latex_preamble=pre)
    res = r.render_all({
        "cd": (r"\begin{tikzcd} \cA \arrow[r] & B \end{tikzcd}", True),
        "inl": (r"\cA \xrightarrow{\ \sim\ } \mathcal{B} \mathbin{\text{\textcircled{\tiny 1}}}", False),
    })
    assert res["cd"].method == "latex"
    with Image.open(res["cd"].pieces[0].file) as im:
        assert im.mode == "L" and min(im.getdata()) < 60
    assert res["inl"].method in ("mathjax", "latex")
