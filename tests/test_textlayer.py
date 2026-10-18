"""force_ocr default, text-layer heuristics and backend logging (no marker models needed)."""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

import pytest

from math2kindle import cli
from math2kindle.backends import marker_backend
from math2kindle.textlayer import check_marker_output, count_formulas, inspect_text_layer, score_text

from .conftest import FIXTURES, requires_node
from .pdfgen import CLEAN_PROSE, GARBLED_PROSE, make_pdf

# the user's real symptom, verbatim (Gray, Worlds Out of Nothing, marker without force_ocr)
GRAY = ("Let the lines ” and “! meet at O … Let A be represented by the vector a and A! by a! . "
        "Then B is represented by λa and C by μa … The point P is represented by a vector of the "
        "form ˢλª + (1 − ˢ) ᴴ! a! , which says that it is on BC! …")


@pytest.fixture
def garbled_pdf(tmp_path) -> Path:
    return make_pdf(tmp_path / "garbled.pdf", [[("Helv", t) for t in GARBLED_PROSE]] * 3)


@pytest.fixture
def clean_pdf(tmp_path) -> Path:
    return make_pdf(tmp_path / "clean.pdf", [[("Helv", t) for t in CLEAN_PROSE]] * 3)


# ---------------------------------------------------------------- heuristic on synthetic PDFs

def test_garbled_prose_pdf_is_broken(garbled_pdf):
    rep = inspect_text_layer(garbled_pdf)
    assert rep.pages_sampled == 3 and rep.broken
    assert rep.bang >= 10 and rep.quotes >= 5 and rep.odd_glyphs >= 5
    assert "glued to letters" in rep.summary() and rep.examples


def test_private_use_font_is_broken(tmp_path):
    pdf = make_pdf(tmp_path / "pua.pdf", [[("Helv", t) for t in CLEAN_PROSE] + [("Garble", b"ABC CBA AB")]])
    rep = inspect_text_layer(pdf)
    assert rep.pua >= 8 and rep.broken and "private-use" in rep.summary()


def test_math_font_without_tounicode_is_suspicious_only(tmp_path):
    pdf = make_pdf(tmp_path / "cmmi.pdf", [[("Helv", t) for t in CLEAN_PROSE] + [("CMMI", b"xyz")]])
    rep = inspect_text_layer(pdf)
    assert rep.math_fonts_no_tounicode == ["CMMI10"]          # subset prefix stripped
    assert rep.suspicious and not rep.broken


def test_clean_pdfs_are_not_flagged(clean_pdf):
    rep = inspect_text_layer(clean_pdf)
    assert not rep.broken and not rep.suspicious, rep.summary()
    # real pdflatex output: CM fonts with ToUnicode, CMEX brace pieces in U+F8Fx
    rep = inspect_text_layer(FIXTURES / "paper.pdf")
    assert rep.has_text and not rep.broken and not rep.suspicious, rep.summary()


def test_score_text_on_users_example():
    rep = score_text(GRAY)
    assert rep.broken and rep.bang >= 5 and rep.quotes >= 2 and rep.odd_glyphs >= 3
    assert score_text("�" * 3 + " some text").broken
    assert not score_text("Hello! This is fine. We have n! = 1·2⋯n ways.").broken


def test_unreadable_pdf_does_not_raise(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    rep = inspect_text_layer(bad)
    assert rep.error and not rep.broken and "not inspected" in rep.summary()


# ---------------------------------------------------------------- post-conversion check

def test_output_check_flags_garbled_markdown():
    md = "# 2.4 Pole and polar\n\n" + "\n\n".join([GRAY] * 4) + "\n\n$$x^2+y^2=1$$\n"
    chk = check_marker_output(md)
    assert chk.display == 1 and chk.inline == 0 and chk.garbled


def test_output_check_passes_good_markdown():
    md = (FIXTURES / "marker_sample" / "paper.md").read_text()
    chk = check_marker_output(md)
    assert chk.formulas >= 40 and not chk.garbled
    assert count_formulas("a $x$ b $$y$$ c \\$5 and $z$") == (2, 1)


def test_postcheck_warns_loudly(caplog):
    caplog.set_level(logging.INFO, logger="math2kindle")
    w = marker_backend.postcheck_marker_output("\n\n".join([GRAY] * 4), force_ocr=False)
    assert w and "PROBABLY GARBLED" in caplog.text and "--no-force-ocr" in caplog.text


# ---------------------------------------------------------------- marker config

def test_marker_config_defaults():
    cfg = marker_backend.marker_config()
    assert cfg["force_ocr"] is True
    assert cfg["ocr_inline_math"] is True            # marker 2.x (auto = off in fast mode, i.e. on every Mac)
    assert cfg["disable_ocr_math"] is False          # marker 1.x
    assert cfg["use_llm"] is False and "redo_inline_math" not in cfg
    assert "page_range" not in cfg
    assert marker_backend.marker_config(force_ocr=False)["force_ocr"] is False
    assert marker_backend.marker_config(page_range="0,3-5")["page_range"] == [0, 3, 4, 5]


def test_config_keys_exist_in_installed_marker():
    """Every key we pass is an attribute of some marker class (else marker silently ignores it)."""
    pytest.importorskip("marker.providers.pdf")
    from marker.providers.pdf import PdfProvider

    assert hasattr(PdfProvider, "force_ocr")
    try:
        from marker.processors.equation import EquationProcessor

        two = hasattr(EquationProcessor, "ocr_inline_math")
    except Exception:
        two = False
    if two:                                            # marker >= 2
        from marker.builders.layout import LayoutBuilder

        assert hasattr(LayoutBuilder, "force_ocr")
    else:                                              # marker 1.x
        from marker.builders.ocr import OcrBuilder

        assert hasattr(OcrBuilder, "disable_ocr_math")


def _fake_marker_modules(monkeypatch, seen):
    class FakeConverter:
        def __init__(self, artifact_dict, config=None, **kw):
            seen["config"] = dict(config)

        def __call__(self, path):
            seen["path"] = path
            return types.SimpleNamespace(metadata={"pages": 1})

    mods = {
        "marker": types.ModuleType("marker"),
        "marker.converters": types.ModuleType("marker.converters"),
        "marker.converters.pdf": types.ModuleType("marker.converters.pdf"),
        "marker.models": types.ModuleType("marker.models"),
        "marker.output": types.ModuleType("marker.output"),
    }
    mods["marker.converters.pdf"].PdfConverter = FakeConverter
    mods["marker.models"].create_model_dict = lambda: {"fake": 1}
    mods["marker.models"].shutdown_models = lambda m: None
    mods["marker.output"].text_from_rendered = lambda r: ("Let $A'$ be a point.", "md", {})
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.setattr(marker_backend, "llama_server_problem", lambda dev: None)
    monkeypatch.setattr(marker_backend, "surya_servers", lambda: {})
    monkeypatch.setattr(marker_backend, "stop_new_surya_servers", lambda before: [])


@pytest.mark.parametrize("force", [True, False])
def test_run_marker_passes_force_ocr(tmp_path, monkeypatch, force):
    seen: dict = {}
    _fake_marker_modules(monkeypatch, seen)
    md, files, meta = marker_backend.run_marker(FIXTURES / "paper.pdf", tmp_path / "m", device="cpu",
                                                force_ocr=force, page_range="0-1")
    assert seen["config"]["force_ocr"] is force
    assert seen["config"]["ocr_inline_math"] is True and seen["config"]["page_range"] == [0, 1]
    assert md == "Let $A'$ be a point." and meta == {"pages": 1}


def test_run_marker_default_is_force_ocr(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_marker_modules(monkeypatch, seen)
    marker_backend.run_marker(FIXTURES / "paper.pdf", tmp_path / "m", device="cpu")
    assert seen["config"]["force_ocr"] is True
    assert marker_backend.MarkerBackend().force_ocr is True


def test_marker_single_fallback_command(tmp_path, monkeypatch):
    """No Python API -> marker_single gets --force_ocr plus the full config as --config_json."""
    for k in ("marker.converters.pdf", "marker.models", "marker.output"):
        monkeypatch.setitem(sys.modules, k, None)           # import raises ImportError
    monkeypatch.setattr(marker_backend, "llama_server_problem", lambda dev: None)
    monkeypatch.setattr(marker_backend, "surya_servers", lambda: {})
    monkeypatch.setattr(marker_backend, "stop_new_surya_servers", lambda before: [])
    monkeypatch.setattr(marker_backend, "which", lambda name: "/usr/bin/" + name)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cfg"] = json.loads(Path(cmd[cmd.index("--config_json") + 1]).read_text())
        out = Path(cmd[cmd.index("--output_dir") + 1]) / "paper"
        out.mkdir(parents=True)
        (out / "paper.md").write_text("$x$")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(marker_backend.subprocess, "run", fake_run)
    md, _, _ = marker_backend.run_marker(FIXTURES / "paper.pdf", tmp_path / "m", device="cpu")
    assert md == "$x$" and "--force_ocr" in seen["cmd"]
    assert seen["cfg"]["force_ocr"] is True and seen["cfg"]["ocr_inline_math"] is True
    assert marker_backend.marker_cli_args() == ["--output_format", "markdown", "--force_ocr",
                                                "--ocr_inline_math", "True"]


# ---------------------------------------------------------------- pre-check logging

def test_precheck_warns_with_no_force_ocr(garbled_pdf, caplog):
    caplog.set_level(logging.INFO, logger="math2kindle")
    rep = marker_backend.precheck_text_layer(garbled_pdf, force_ocr=False)
    assert rep.broken
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("GARBLED MATHS" in r.getMessage() for r in warns)


def test_precheck_only_informs_with_force_ocr(garbled_pdf, caplog):
    caplog.set_level(logging.INFO, logger="math2kindle")
    marker_backend.precheck_text_layer(garbled_pdf, force_ocr=True)
    assert "Text layer check" in caplog.text and "force_ocr is on" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------- CLI flags

def test_cli_force_ocr_flags():
    p = cli.build_parser()
    assert p.parse_args(["x.pdf"]).force_ocr is True
    assert p.parse_args(["x.pdf", "--no-force-ocr"]).force_ocr is False
    assert p.parse_args(["x.pdf", "--force-ocr"]).force_ocr is True


def _cli_with_fake_marker(monkeypatch, md: str, calls: list):
    def fake_run_marker(pdf, out_dir, **kw):
        calls.append(kw)
        return md, {}, {}

    monkeypatch.setattr(marker_backend, "run_marker", fake_run_marker)
    monkeypatch.setattr(marker_backend, "marker_available", lambda: True)


@requires_node
def test_cli_default_runs_marker_with_force_ocr(tmp_path, monkeypatch, caplog, clean_pdf):
    calls: list = []
    _cli_with_fake_marker(monkeypatch, (FIXTURES / "marker_sample" / "paper.md").read_text(), calls)
    caplog.set_level(logging.INFO, logger="math2kindle")
    rc = cli.main([str(clean_pdf), "--out", str(tmp_path), "--no-arxiv-source"])
    assert rc == 0 and calls[0]["force_ocr"] is True
    assert "backend: marker" in caplog.text and "(force_ocr" in caplog.text
    assert "PROBABLY GARBLED" not in caplog.text


@requires_node
def test_cli_no_force_ocr_on_garbled_pdf_warns_twice(tmp_path, monkeypatch, caplog, garbled_pdf):
    calls: list = []
    _cli_with_fake_marker(monkeypatch, "\n\n".join([GRAY] * 4), calls)
    caplog.set_level(logging.INFO, logger="math2kindle")
    rc = cli.main([str(garbled_pdf), "--out", str(tmp_path), "--no-force-ocr", "--no-arxiv-source"])
    assert rc == 0 and calls[0]["force_ocr"] is False
    assert "GARBLED MATHS" in caplog.text                  # before marker
    assert "PROBABLY GARBLED" in caplog.text               # after marker
    assert "(no force_ocr" in caplog.text


@requires_node
def test_cli_logs_pdftext_fallback(tmp_path, monkeypatch, caplog, clean_pdf):
    monkeypatch.setattr(marker_backend, "marker_available", lambda: False)
    caplog.set_level(logging.INFO, logger="math2kindle")
    rc = cli.main([str(clean_pdf), "--out", str(tmp_path), "--allow-text-fallback", "--no-arxiv-source"])
    assert rc == 0 and "backend: pdftext fallback" in caplog.text
