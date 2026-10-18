"""Opt-in check of the REAL marker-pdf pipeline (downloads several GB of models on first run).

Run on your own machine with:  M2K_RUN_MARKER=1 pytest tests/test_marker_real.py -s
"""

from __future__ import annotations

import os

import pytest

from math2kindle.backends.marker_backend import MarkerBackend, marker_available
from math2kindle.pipeline import Builder

from .conftest import FIXTURES, run_epubcheck

pytestmark = pytest.mark.skipif(
    os.environ.get("M2K_RUN_MARKER") != "1" or not marker_available(),
    reason="set M2K_RUN_MARKER=1 (and install marker-pdf) to run the real marker backend")


def test_marker_on_fixture_pdf(tmp_path):
    doc = MarkerBackend().convert(FIXTURES / "paper.pdf", tmp_path)
    b = Builder(doc, tmp_path)
    res = b.build(tmp_path / "marker.epub")
    print("maths:", b.stats, "-> EPUB:", res.epub)
    assert b.stats.get("mathjax", 0) >= 50          # the paper has ~130 formulas
    ok, log = run_epubcheck(res.epub)
    assert ok, log
