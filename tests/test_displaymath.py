"""Display structure and break-point analysis (no Node needed)."""

from __future__ import annotations

from math2kindle.displaymath import break_candidates, display_spec, expand_user_macros, soften_left_right
from math2kindle.latex.macros import harvest


def prios(tex):
    return [(s["prio"], s["tex"]) for s in break_candidates(tex)]


def test_relations_then_top_level_plus_minus():
    segs = prios(r"\psi(x) = x - \sum_{\rho}\frac{x^\rho}{\rho} + O\bigl(\log^2 x\bigr) \le 2x")
    assert [p for p, _ in segs] == [None, 1, 2, 2, 1]
    assert segs[0][1] == r"\psi(x)"


def test_never_between_big_operator_and_operand():
    # (2.6) of the fixture: \sum_{...} and \min(...) must stay together
    segs = prios(r"\sum_{x/2<n\le2x} \ll A(2x)\sum_{x/2<n\le 2x}\min\left(1,\frac{x}{T|x-n|}\right)")
    assert any(r"\sum_{x/2<n\le 2x}\min" in t for _, t in segs)
    segs = prios(r"\sum_{n}\int_0^1 f_n(t)\,dt = \prod_p g(p)")
    assert segs == [(None, r"\sum_{n}\int_0^1 f_n(t)\,dt"), (1, r"= \prod_p g(p)")]


def test_no_breaks_inside_braces_left_right_or_envs():
    tex = r"a + \frac{b + c}{d} + \left( e + f \right) + \begin{pmatrix} g + h \end{pmatrix}"
    for _, t in prios(tex)[1:]:
        assert t.startswith("+")
    assert len(prios(tex)) == 4


def test_unary_minus_is_not_a_break():
    assert [p for p, _ in prios(r"-a = -b + (-c)")] == [None, 1, 2]


def test_bracket_groups_rank_by_depth_and_size():
    segs = prios(r"x = \Bigl( a + \frac{t}{6}\bigl(n\,s - 6\,s\bigr) + O(t^3)\Bigr)")
    assert segs[2][0] == 3                    # top level of the \Bigl group
    assert any(p == 8 for p, _ in segs)       # inside the short (n s - 6 s) group: last resort


def test_quad_separated_formulas_break_first():
    segs = prios(r"a = b,\qquad c = d")
    assert segs[2][0] == 0 and segs[2][1] == "c"


def test_soften_long_left_right_only():
    long = r"\left( \frac{x}{T}\sum_{n=1}^{\infty}\frac{|a_n|}{n^{c}} + \frac{x A(2x)\log x}{T} \right)"
    assert r"\biggl(" in soften_left_right(long)
    short = r"\left( a + b \right)"
    assert soften_left_right(short) == short
    segs = prios(soften_left_right("y = O" + long))
    assert any(p == 7 for p, _ in segs)       # breakable, but only as a last resort


def test_user_macros_expanded_for_analysis():
    ms, _ = harvest(r"\def\bigO#1{O\!\left(#1\right)} \newcommand{\Z}{\mathbb{Z}}")
    assert expand_user_macros(r"\bigO{x} + y^\Z", ms) == r"O\!\left(x\right) + y^{\mathbb{Z}}"


def test_display_specs():
    sp = display_spec(r"E = mc^2 \tag{4}")
    assert sp["kind"] == "single" and sp["group_tag"] == "(4)"
    sp = display_spec(r"\begin{align*} a &= b \tag{1}\\ &\le c \notag\\ \end{align*}")
    assert sp["env"] == "align" and [r["tag"] for r in sp["rows"]] == ["(1)", None]
    assert sp["rows"][1]["cells"] == ["", r"\le c"]
    sp = display_spec(r"\begin{multline*} a + b \\ c + d \tag{3}\end{multline*}")
    assert sp["env"] == "multline" and sp["group_tag"] == "(3)"
    sp = display_spec(r"\begin{aligned} x &= y \\ &= z \end{aligned}. \tag{2}")
    assert sp["kind"] == "rows" and sp["group_tag"] == "(2)" and sp["rows"][1]["cells"][1] == "= z."
