from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def have_node() -> bool:
    return shutil.which("node") is not None and (
        shutil.which("npm") is not None or os.environ.get("MATH2KINDLE_NODE_DIR"))


requires_node = pytest.mark.skipif(not have_node(), reason="Node.js/npm not available")
requires_pdflatex = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not available")


def epubcheck_cmd() -> list[str] | None:
    jar = os.environ.get("EPUBCHECK_JAR")
    if jar:
        return ["java", "-jar", jar]
    p = shutil.which("epubcheck")
    if not p:
        return None
    with open(p, "rb") as fh:
        if fh.read(2) == b"PK":          # Debian ships the bare jar as /usr/bin/epubcheck
            return ["java", "-jar", p] if shutil.which("java") else None
    return [p]


def run_epubcheck(epub: Path) -> tuple[bool, str]:
    cmd = epubcheck_cmd()
    if cmd is None:
        pytest.skip("epubcheck not installed")
    proc = subprocess.run(cmd + [str(epub)], capture_output=True, text=True, timeout=300)
    out = proc.stdout + proc.stderr
    ok = proc.returncode == 0 and "0 errors" in out and "0 fatals" in out
    return ok, out


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def paper_epub(tmp_path_factory) -> Path:
    """The fixture paper converted once through the CLI (LaTeX backend)."""
    if not have_node():
        pytest.skip("Node.js not available")
    from math2kindle.cli import main

    out = tmp_path_factory.mktemp("paper_out")
    rc = main([str(FIXTURES / "paper_src.tar.gz"), "--out", str(out), "-q"])
    assert rc == 0
    epubs = list(out.glob("*.epub"))
    assert len(epubs) == 1
    return epubs[0]


def read_epub(epub: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(epub) as z:
        return {n: z.read(n) for n in z.namelist()}
