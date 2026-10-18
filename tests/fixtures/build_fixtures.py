"""Rebuild the committed test fixtures from tests/fixtures/paper_src:

* paper_src.tar.gz  - arXiv-style source tarball (main.tex, macros, sections, .bbl, PDF figure)
* paper.pdf         - the same paper compiled with pdflatex (input for the marker backend)

Usage: python tests/fixtures/build_fixtures.py
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "paper_src"
FILES = ["main.tex", "macros.tex", "main.bbl", "sections/intro.tex", "sections/perron.tex", "figures/contour.pdf"]


def build_tarball() -> Path:
    out = HERE / "paper_src.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for f in FILES:
            info = tf.gettarinfo(str(SRC / f), arcname=f)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 1700000000
            with open(SRC / f, "rb") as fh:
                tf.addfile(info, fh)
    return out


def build_pdf() -> Path:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for f in FILES:
            (tdp / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SRC / f, tdp / f)
        for _ in range(2):
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "-no-shell-escape", "main.tex"], cwd=tdp,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        out = HERE / "paper.pdf"
        shutil.copy(tdp / "main.pdf", out)
    return out


if __name__ == "__main__":
    print(build_tarball())
    print(build_pdf())
