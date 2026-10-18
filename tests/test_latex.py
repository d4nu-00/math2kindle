"""LaTeX source handling: comments, main-file detection, flattening, macros, numbering, .bbl."""

from __future__ import annotations

from pathlib import Path

import pytest

from math2kindle.latex.bbl import parse_bbl, parse_biblatex_bbl, parse_thebibliography
from math2kindle.latex.converter import LatexConverter
from math2kindle.latex.macros import MacroSet, expand_macro, harvest
from math2kindle.latex.project import find_main_tex, flatten, split_preamble
from math2kindle.latex.texscan import (find_env_end, read_arg, read_opt, remove_iffalse, split_top_level,
                                       strip_comments)

# ---------------------------------------------------------------- scanning helpers


def test_strip_comments_like_tex():
    assert strip_comments("a % comment\nb") == "a b"
    assert strip_comments("50\\% off") == "50\\% off"
    assert strip_comments("para one %\n\npara two") == "para one \n\npara two"
    v = "\\begin{verbatim}\n% kept\n\\end{verbatim}"
    assert strip_comments(v) == v
    assert strip_comments("\\verb|%x| y % gone") == "\\verb|%x| y "
    assert "vanish" not in strip_comments("a\\begin{comment}\nvanish\n\\end{comment}b")


def test_remove_iffalse_nested_and_iff():
    s = "A \\iffalse B \\ifx\\a\\b C \\fi D \\fi E $p\\iff q$"
    assert remove_iffalse(s) == "A  E $p\\iff q$"


def test_read_arg_and_opt():
    s = "[a{]}b]{x{y}z} rest"
    opt, i = read_opt(s, 0)
    assert opt == "a{]}b"
    arg, j = read_arg(s, i)
    assert arg == "x{y}z" and s[j:] == " rest"


def test_find_env_end_nested():
    s = "\\begin{a} x \\begin{a} y \\end{a} z \\end{a} tail"
    start, end = find_env_end(s, len("\\begin{a}"), "a")
    assert s[end:] == " tail"


def test_split_top_level():
    rows = split_top_level(r"a &= b \\[2pt] c &= \begin{cases} 1 & 2 \\ 3 \end{cases} \\ {x \\ y}", "\\\\")
    assert len(rows) == 3
    assert rows[1].strip().startswith("c")
    cells = split_top_level(r"a &= \begin{pmatrix} 1&2 \end{pmatrix}", "&")
    assert len(cells) == 2


# ---------------------------------------------------------------- project


def _write(p: Path, s: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")
    return p


def test_find_main_tex_prefers_bbl_match(tmp_path):
    _write(tmp_path / "notes.tex", "\\documentclass{article}\\begin{document}notes\\end{document}")
    _write(tmp_path / "paper.tex", "\\documentclass{amsart}\n\\begin{document}paper\\end{document}")
    _write(tmp_path / "paper.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}")
    _write(tmp_path / "sec.tex", "\\section{Intro}")
    _write(tmp_path / "old.tex", "% \\documentclass{article}\n% \\begin{document}")
    assert find_main_tex(tmp_path).name == "paper.tex"


def test_find_main_tex_single_and_fixture(tmp_path, fixtures):
    assert find_main_tex(fixtures / "paper_src").name == "main.tex"
    _write(tmp_path / "x.tex", "\\documentclass{article}\\begin{document}\\end{document}")
    assert find_main_tex(tmp_path).name == "x.tex"


def test_flatten_inputs_and_traversal(tmp_path):
    outside = _write(tmp_path / "secret.tex", "SECRET")
    root = tmp_path / "src"
    _write(root / "main.tex", "\\documentclass{article}\n\\input{macros}\n\\begin{document}\n"
                              "\\input{sec/one}\n\\include{two}\n\\input three\n% \\input{commented}\n"
                              "\\input{../secret}\n\\end{document}\n")
    _write(root / "macros.tex", "\\newcommand{\\Z}{\\mathbb{Z}} % comment")
    _write(root / "sec/one.tex", "ONE \\input{sec/nested}")
    _write(root / "sec/nested.tex", "NESTED")
    _write(root / "two.tex", "TWO")
    _write(root / "three.tex", "THREE")
    _write(root / "commented.tex", "COMMENTED")
    flat = flatten(root / "main.tex", root)
    t = flat.text
    for s in ("ONE", "NESTED", "TWO", "THREE", "\\newcommand{\\Z}"):
        assert s in t
    assert "COMMENTED" not in t
    assert "SECRET" not in t
    assert outside.exists()
    pre, body = split_preamble(t)
    assert "\\newcommand" in pre and "ONE" in body


# ---------------------------------------------------------------- macros


def test_harvest_fixture_macros(fixtures):
    ms, spans = harvest((fixtures / "paper_src" / "macros.tex").read_text())
    m = ms.macros
    assert m["Z"].body == "\\mathbb{Z}" and m["Z"].nargs == 0
    assert m["Lfun"].nargs == 2 and m["Lfun"].default == "s"
    assert m["bigO"].kind == "def" and m["bigO"].nargs == 1
    assert m["li"].kind == "operator"
    assert m["Res"].kind == "operator" and "DeclareMathOperator*" in m["Res"].raw
    assert m["abs"].kind == "paired"
    assert m["Re"].body == "\\operatorname{Re}"
    defs = ms.mathjax_defs()
    assert any(d.startswith("\\DeclarePairedDelimiter{\\abs}") for d in defs)
    assert len(spans) >= 15


def test_harvest_theorems_and_styles():
    pre = r"""
    \theoremstyle{plain}
    \newtheorem{theorem}{Theorem}[section]
    \newtheorem{lemma}[theorem]{Lemma}
    \theoremstyle{definition}
    \newtheorem{definition}[theorem]{Definition}
    \theoremstyle{remark}
    \newtheorem*{remark}{Remark}
    \declaretheorem[name=Conjecture, style=plain, numberwithin=section]{conj}
    \numberwithin{equation}{section}
    """
    ms, _ = harvest(pre)
    t = ms.theorems
    assert t["theorem"].counter == "theorem" and t["theorem"].within == "section"
    assert t["lemma"].counter == "theorem" and t["lemma"].within == "section"
    assert t["definition"].style == "definition"
    assert t["remark"].counter is None and t["remark"].style == "remark"
    assert t["conj"].title == "Conjecture" and t["conj"].within == "section"
    assert ms.numberwithin["equation"] == "section"


def test_expand_macro_with_optional_argument():
    ms, _ = harvest(r"\newcommand{\Lfun}[2][s]{L(#1,#2)}")
    mac = ms.macros["Lfun"]
    assert expand_macro(mac, "{\\chi} rest", 0)[0] == "L(s,\\chi)"
    assert expand_macro(mac, "[1/2]{\\chi}", 0)[0] == "L(1/2,\\chi)"


# ---------------------------------------------------------------- converter


def _convert(body: str, preamble: str = "", tmp_path: Path | None = None, bib=None):
    ms, _ = harvest(preamble, MacroSet())
    conv = LatexConverter(ms, root=tmp_path or Path("."), work_dir=tmp_path or Path("."), bib=bib)
    out = conv.convert_body(body)
    return conv, out


PRE = r"""
\numberwithin{equation}{section}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\newcommand{\Z}{\mathbb{Z}}
\newcommand{\ie}{i.e.\ }
"""


def test_numbering_and_refs(tmp_path):
    body = r"""
\section{Intro}\label{sec:intro}
Text \ie more. $a\in\Z$.
\begin{equation}\label{eq:one} x=1 \end{equation}
\begin{align}
a &= b \label{eq:two}\\
c &= d \nonumber\\
e &= f \tag{$*$}\label{eq:star}
\end{align}
\section{Main}
\begin{theorem}[Main]\label{thm:main} $y$ \end{theorem}
\begin{lemma}\label{lem:x} z \end{lemma}
\begin{equation} w \label{eq:three} \end{equation}
See \eqref{eq:one}, \ref{thm:main}, \cref{lem:x}, Section~\ref{sec:intro}, \eqref{eq:star}.
\[ \stackrel{\eqref{eq:two}}{=} \]
"""
    conv, out = _convert(body, PRE, tmp_path)
    L = conv.st.labels
    assert L["sec:intro"].number == "1"
    assert L["eq:one"].number == "1.1"
    assert L["eq:two"].number == "1.2"
    assert L["eq:star"].number == "$*$"
    assert L["thm:main"].number == "2.1" and L["thm:main"].name == "Theorem"
    assert L["lem:x"].number == "2.2" and L["lem:x"].name == "Lemma"
    assert L["eq:three"].number == "2.1"     # equation counter reset per section
    # text macro expanded, maths left to MathJax
    assert "i.e." in out
    maths = [m.tex for m in conv.st.math.values()]
    assert "a\\in\\Z" in maths
    align = next(t for t in maths if t.startswith("\\begin{align*}"))
    assert "\\tag{1.2}" in align and "\\tag{1.3}" not in align and "\\bbox" not in align
    assert any("\\text{(1.2)}" in t for t in maths)      # \eqref inside maths resolved
    assert "M2KREF" in out and "\\label" not in out


def test_theorem_and_proof_markup(tmp_path):
    conv, out = _convert(r"\begin{lemma}[Key] text \end{lemma}\begin{proof}[Proof of Lemma] done \end{proof}",
                         PRE, tmp_path)
    assert "\\begin{m2kthmplain}" in out and "\\textbf{Lemma 0.1}" in out and "(Key)" in out  # as LaTeX: within section 0
    assert "\\begin{m2kproof}" in out and "\\textit{Proof of Lemma.}" in out


def test_intertext_splits_display(tmp_path):
    conv, out = _convert(r"\begin{align} a&=b \\ \intertext{and so} c&=d \end{align}", "", tmp_path)
    assert "and so" in out
    displays = [m for m in conv.st.math.values() if m.display]
    assert len(displays) == 2
    assert "\\tag{1}" in displays[0].tex and "\\tag{2}" in displays[1].tex


def test_citations_with_bbl(tmp_path, fixtures):
    bib = parse_bbl((fixtures / "paper_src" / "main.bbl").read_text())
    conv, out = _convert(r"See \cite[Thm.~2]{Huxley} and \cite{Davenport,Ivic}. \bibliography{x}", "", tmp_path,
                         bib=bib)
    assert conv.st.labels["bib::Huxley"].number == "2"
    assert "\\begin{m2kbib}" in out and out.count("\\begin{m2kbibitem}") == 6


def test_unbalanced_and_unknown_input_does_not_crash(tmp_path):
    conv, out = _convert(r"\begin{weird}{x} $a + b \foo{ \end{weird} \ref{missing} \cite{nokey}", "", tmp_path)
    assert isinstance(out, str)


# ---------------------------------------------------------------- bibliographies


def test_parse_fixture_bbl(fixtures):
    entries = parse_thebibliography((fixtures / "paper_src" / "main.bbl").read_text())
    assert [e.key for e in entries] == ["Davenport", "Huxley", "Ivic", "Korobov", "Titchmarsh", "Vinogradov"]
    assert entries[0].label == "1" and "Multiplicative" in entries[0].text


def test_parse_natbib_and_alpha_labels():
    tex = r"""\begin{thebibliography}{99}
\bibitem[{Davenport(1980)}]{dav} H. Davenport, \newblock Book, 1980.
\bibitem[Ivi85]{ivic} A. Ivi\'c, Zeta, 1985.
\end{thebibliography}"""
    e = parse_thebibliography(tex)
    assert e[0].author == "Davenport" and e[0].year == "1980"
    assert e[1].label == "Ivi85"
    assert "\\newblock" not in e[0].text


def test_parse_biblatex_bbl():
    tex = r"""
\entry{hux}{article}{}
  \name{author}{1}{}{{{hash=1}{family={Huxley},familyi={H\bibinitperiod},given={Martin},giveni={M\bibinitperiod}}}}
  \field{title}{On the difference between consecutive primes}
  \field{journaltitle}{Invent. Math.}
  \field{volume}{15}
  \field{year}{1972}
\endentry"""
    e = parse_biblatex_bbl(tex)
    assert e[0].key == "hux" and "Huxley" in e[0].text and "1972" in e[0].text


@pytest.mark.parametrize("name", ["paper_src/main.tex"])
def test_fixture_main_has_expected_constructs(fixtures, name):
    t = (fixtures / name).read_text()
    for s in ("\\newtheorem", "\\eqref", "\\cite", "\\footnote", "\\includegraphics", "\\begin{tikzcd}",
              "\\begin{multline}", "\\begin{pmatrix}"):
        assert s in t or s in (fixtures / "paper_src/sections/perron.tex").read_text() + \
            (fixtures / "paper_src/sections/intro.tex").read_text()
