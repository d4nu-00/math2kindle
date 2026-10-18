"""Backend-independent intermediate document."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .mathrender import LatexPreamble


@dataclass
class MathRef:
    tex: str
    display: bool
    anchor: str | None = None


@dataclass
class ConvertedDoc:
    title: str = "Untitled"
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None              # plain text (metadata / title page fallback)
    source: str | None = None                # e.g. "arXiv:2101.00001v2"
    source_url: str | None = None
    date: str | None = None
    language: str = "en"
    body: object = None                      # lxml <div> root with the content
    math: dict[str, MathRef] = field(default_factory=dict)   # token -> maths (LaTeX backend)
    images: dict[str, Path] = field(default_factory=dict)    # src attribute -> file on disk
    mathjax_macros: list[str] = field(default_factory=list)
    latex_preamble: LatexPreamble = field(default_factory=LatexPreamble)
    backend: str = "latex"
    backend_note: str = ""                   # human-readable: "marker (force_ocr)", "pdftext fallback", ...
    warnings: list[str] = field(default_factory=list)
