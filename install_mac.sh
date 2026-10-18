#!/bin/bash
# math2kindle installer for macOS (Apple Silicon or Intel). Idempotent: safe to re-run.
#
#   bash ~/development/math2kindle/install_mac.sh [--with-tex] [--with-marker] [--recreate-venv] [--no-tests]
#
#   --with-tex       install BasicTeX (cask, asks for your password) + the TeX packages used by the
#                    LaTeX fallback renderer and TikZ / tikz-cd / xy-pic figures
#   --with-marker    install marker-pdf (ML backend for PDFs without LaTeX source; pulls in torch,
#                    and downloads several GB of models on its first conversion; uses MPS on Apple Silicon)
#                    plus llama.cpp (brew), whose llama-server runs marker's vision model
#   --recreate-venv  delete and rebuild .venv
#   --no-tests       skip the smoke test
#
# Environment escape hatches (for testing on Linux / unusual setups):
#   M2K_SKIP_BREW=1     do not use Homebrew at all; python3, node and npm must already be on PATH
#   M2K_PYTHON=/path    use this Python (>= 3.11) for the venv
#   M2K_BIN_DIR=/path   where to put the launchers (default ~/.local/bin)
#   M2K_CONFIG_DIR=/p   where to put config.toml (default ~/.config/math2kindle; the tool reads only
#                       that default, or $MATH2KINDLE_CONFIG)
#
# Written for macOS's stock bash 3.2: no associative arrays, no ${var,,}, no mapfile.

set -euo pipefail

# ---------------------------------------------------------------------------- helpers
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s==> [%d] %s%s\n' "$B" "$STEP" "$*" "$N"; }
ok()   { printf '    %sok%s  %s\n' "$G" "$N" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %swarning:%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '\n%sInstall stopped (exit %d) during step %d. Fix the problem above and re-run; finished steps are skipped.%s\n' "$R" "$rc" "$STEP" "$N" >&2
  fi
}
trap on_exit EXIT

WITH_TEX=0; WITH_MARKER=0; RECREATE=0; RUN_TESTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --with-tex) WITH_TEX=1 ;;
    --with-marker) WITH_MARKER=1 ;;
    --recreate-venv) RECREATE=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

REPO="$(cd -P "$(dirname "$0")" && pwd)"
VENV="$REPO/.venv"
BIN_DIR="${M2K_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${M2K_CONFIG_DIR:-$HOME/.config/math2kindle}"
SKIP_BREW="${M2K_SKIP_BREW:-0}"
TEXBIN=/Library/TeX/texbin

# Keep conda / stray Python settings out of the build.
unset PYTHONPATH PYTHONHOME PIP_REQUIRE_VIRTUALENV PIP_USER 2>/dev/null || true
export PIP_DISABLE_PIP_VERSION_CHECK=1 HOMEBREW_NO_ENV_HINTS=1
# Don't put the cwd on sys.path: from ~/development the repo dir math2kindle/ would
# shadow the real package as a namespace package (Python >= 3.11; the venv requires it).
export PYTHONSAFEPATH=1

printf '%smath2kindle installer%s\n    repo: %s\n' "$B" "$N" "$REPO"
if [ ! -f "$REPO/pyproject.toml" ] || [ ! -d "$REPO/math2kindle" ]; then
  die "run this script from the math2kindle repo (pyproject.toml not found next to it)"
fi
case "$REPO" in
  "$HOME/development/math2kindle") ;;
  *) warn "expected the repo at ~/development/math2kindle; installing in place at $REPO anyway" ;;
esac
if [ -n "${CONDA_PREFIX:-}" ]; then
  info "(conda env '$(basename "$CONDA_PREFIX")' is active; it is ignored - the venv uses Homebrew's Python)"
fi

# Files copied from another machine may carry the quarantine flag; harmless if absent.
if command -v xattr >/dev/null 2>&1; then xattr -dr com.apple.quarantine "$REPO" 2>/dev/null || true; fi
chmod +x "$REPO/kindle" "$REPO/install_mac.sh" 2>/dev/null || true

py_ok() {  # $1 = python executable; true if it is >= 3.11 and not a conda python
  [ -x "$1" ] || return 1
  case "$1" in *conda*|*miniforge*|*mambaforge*) return 1 ;; esac
  "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

# ---------------------------------------------------------------------------- 1. Homebrew + system packages
PY=""
if [ "$SKIP_BREW" = "1" ]; then
  step "System packages (M2K_SKIP_BREW=1: Homebrew skipped)"
  for t in node npm; do command -v "$t" >/dev/null 2>&1 || die "M2K_SKIP_BREW=1 but '$t' is not on PATH"; done
  ok "node $(node --version) at $(command -v node)"
else
  step "Homebrew"
  [ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS (set M2K_SKIP_BREW=1 to use system packages elsewhere)"
  [ "$(uname -m)" = "arm64" ] || warn "not Apple Silicon ($(uname -m)); continuing"
  BREW=""
  for c in "$HOME/.homebrew/bin/brew" /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$c" ]; then BREW="$c"; break; fi
  done
  if [ -z "$BREW" ] && command -v brew >/dev/null 2>&1; then BREW="$(command -v brew)"; fi
  [ -n "$BREW" ] || die "Homebrew not found in ~/.homebrew, /opt/homebrew or /usr/local. Install it from https://brew.sh and re-run."
  eval "$("$BREW" shellenv)"
  ok "brew at $BREW (prefix $HOMEBREW_PREFIX)"

  brew_formula() {
    if "$BREW" list --formula "$1" >/dev/null 2>&1; then ok "$1 (already installed)"
    else info "installing $1 ..."; "$BREW" install "$1"; ok "$1"; fi
  }

  step "Python (Homebrew, >= 3.11; conda is avoided)"
  if [ -n "${M2K_PYTHON:-}" ]; then
    py_ok "$M2K_PYTHON" || die "M2K_PYTHON=$M2K_PYTHON is not a usable Python >= 3.11"
    PY="$M2K_PYTHON"
  else
    for v in 3.12 3.13 3.14 3.11; do
      c="$HOMEBREW_PREFIX/opt/python@$v/bin/python$v"
      if py_ok "$c"; then PY="$c"; break; fi
    done
    if [ -z "$PY" ]; then
      brew_formula python@3.12
      PY="$HOMEBREW_PREFIX/opt/python@3.12/bin/python3.12"
      py_ok "$PY" || die "python@3.12 installed but $PY does not run"
    fi
  fi
  ok "$("$PY" -V 2>&1) at $PY"

  step "Node.js, pandoc, Ghostscript"
  brew_formula node          # MathJax 4 + resvg renderer
  brew_formula pandoc        # pypandoc_binary also bundles pandoc; this is the fallback
  brew_formula ghostscript   # EPS figures (PDF figures use PyMuPDF; no poppler needed)
  ok "node $(node --version)"

  if [ "$WITH_MARKER" = "1" ]; then
    step "llama.cpp (llama-server runs marker-pdf's vision model)"
    if command -v llama-server >/dev/null 2>&1; then ok "llama-server already on PATH ($(command -v llama-server))"
    else brew_formula llama.cpp; fi
  fi

  if [ "$WITH_TEX" = "1" ]; then
    step "TeX (BasicTeX + packages for the LaTeX fallback and TikZ figures)"
    if [ -x "$TEXBIN/pdflatex" ]; then ok "TeX already installed ($TEXBIN)"
    else
      info "installing BasicTeX (the macOS installer will ask for your password) ..."
      "$BREW" install --cask basictex
    fi
    export PATH="$TEXBIN:$PATH"
    [ -x "$TEXBIN/tlmgr" ] || die "tlmgr not found in $TEXBIN after installing BasicTeX; open a new Terminal and re-run with --with-tex"
    # tlmgr package:file it provides (dsfont.sty lives in the TeX Live package "doublestroke")
    TEX_PKGS="preview:preview.sty standalone:standalone.cls pgf:tikz.sty tikz-cd:tikz-cd.sty xypic:xy.sty
      mathtools:mathtools.sty amsfonts:amssymb.sty amscls:amsthm.sty rsfs:mathrsfs.sty stmaryrd:stmaryrd.sty
      esint:esint.sty doublestroke:dsfont.sty bbm-macros:bbm.sty xcolor:xcolor.sty cleveref:cleveref.sty
      thmtools:thmtools.sty enumitem:enumitem.sty"
    missing=""
    if [ -x "$TEXBIN/kpsewhich" ]; then
      for pf in $TEX_PKGS; do
        "$TEXBIN/kpsewhich" "${pf#*:}" >/dev/null 2>&1 || missing="$missing ${pf%%:*}"
      done
    else
      installed="$("$TEXBIN/tlmgr" info --only-installed --data name 2>/dev/null || true)"
      for pf in $TEX_PKGS; do
        # here-string, not printf | grep -q: grep -q exits early and printf then hits EPIPE
        grep -qx "${pf%%:*}" <<<"$installed" || missing="$missing ${pf%%:*}"
      done
    fi
    if [ -z "$missing" ]; then ok "TeX packages already installed"
    else
      info "tlmgr install$missing   (sudo: asks for your password)"
      sudo "$TEXBIN/tlmgr" update --self || warn "tlmgr update --self failed; trying the install anyway"
      # shellcheck disable=SC2086  # word splitting of the package list is intended
      if ! sudo "$TEXBIN/tlmgr" install $missing; then
        warn "batch install failed; retrying one package at a time"
        for p in $missing; do sudo "$TEXBIN/tlmgr" install "$p" || warn "could not install TeX package $p"; done
      fi
    fi
    ok "$("$TEXBIN/pdflatex" --version | head -n 1)"
  fi
fi

if [ -z "$PY" ]; then  # M2K_SKIP_BREW path
  step "Python (>= 3.11, not conda)"
  if [ -n "${M2K_PYTHON:-}" ]; then
    py_ok "$M2K_PYTHON" || die "M2K_PYTHON=$M2K_PYTHON is not a usable Python >= 3.11"
    PY="$M2K_PYTHON"
  else
    for n in python3.12 python3.13 python3.11 python3; do
      c="$(command -v "$n" 2>/dev/null || true)"
      if [ -n "$c" ] && py_ok "$c"; then PY="$c"; break; fi
    done
  fi
  [ -n "$PY" ] || die "no Python >= 3.11 found (set M2K_PYTHON)"
  ok "$("$PY" -V 2>&1) at $PY"
fi

# ---------------------------------------------------------------------------- 2. venv + pip
step "Virtual environment at $VENV"
if [ -d "$VENV" ] && [ "$RECREATE" = "0" ] && \
   "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  ok "reusing $("$VENV/bin/python" -V 2>&1) (use --recreate-venv to rebuild)"
else
  if [ -d "$VENV" ]; then info "removing old/broken venv"; rm -rf "$VENV"; fi
  "$PY" -m venv "$VENV"
  ok "created with $("$VENV/bin/python" -V 2>&1)"
fi
VPY="$VENV/bin/python"

step "Python packages (pip install -e .)"
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel
EXTRAS="send,test"
if [ "$WITH_MARKER" = "1" ]; then
  EXTRAS="send,test,marker"
  info "including marker-pdf + torch (a large download; be patient) ..."
fi
"$VPY" -m pip install --quiet -e "${REPO}[${EXTRAS}]" keyring
ok "math2kindle $("$VPY" -c 'import math2kindle._version as v; print(v.__version__)') installed (extras: $EXTRAS)"
if [ "$WITH_MARKER" = "1" ]; then
  dev="$("$VPY" -c 'import torch; print("mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu"))' 2>/dev/null || echo "?")"
  ok "marker-pdf installed; torch device: $dev"
  LLAMA="${LLAMA_CPP_BINARY:-$(command -v llama-server 2>/dev/null || true)}"
  if [ -z "$LLAMA" ] && [ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "$HOMEBREW_PREFIX/bin/llama-server" ]; then
    LLAMA="$HOMEBREW_PREFIX/bin/llama-server"
  fi
  if [ -n "$LLAMA" ] && [ -x "$LLAMA" ]; then
    lv="$("$LLAMA" --version 2>&1 || true)"
    ok "llama-server: $LLAMA ($(grep -i -m1 'version' <<<"$lv" || echo "version unknown"))"
  elif [ "$SKIP_BREW" = "1" ]; then
    warn "llama-server (llama.cpp) not found; marker-pdf cannot run without it. Install llama.cpp, or set LLAMA_CPP_BINARY"
  else
    die "llama-server not found after 'brew install llama.cpp'; run 'brew reinstall llama.cpp' (or set LLAMA_CPP_BINARY) and re-run"
  fi
  info "NOTE: the first PDF conversion downloads several GB of marker models. To do that now:"
  info "      $VPY -m math2kindle.backends.marker_backend $REPO/tests/fixtures/paper.pdf"
fi

# ---------------------------------------------------------------------------- 3. Node renderer packages
step "MathJax 4 + resvg (npm ci into math2kindle's cache)"
# math2kindle installs its vendored package-lock into a per-user cache dir
# (~/Library/Caches/math2kindle/node-<hash>, or $MATH2KINDLE_NODE_DIR); let it do exactly that.
# (python -c rather than a heredoc: bash 3.2 mis-parses heredocs inside $(...))
NODE_DIR="$("$VPY" -c 'import logging, sys
logging.basicConfig(level=logging.INFO, format="    %(message)s", stream=sys.stderr)
from math2kindle.mathrender import NodeRuntime
print(NodeRuntime().ensure())')"
ok "renderer ready in $NODE_DIR"

# ---------------------------------------------------------------------------- 4. launchers
step "Launchers in $BIN_DIR"
mkdir -p "$BIN_DIR"
for name in kindle math2kindle; do
  target="$BIN_DIR/$name"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    warn "$target exists and is not a symlink; left alone"
  else
    ln -sfn "$REPO/kindle" "$target"
    ok "$target -> $REPO/kindle"
  fi
done
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on your PATH" ;;
  *) warn "$BIN_DIR is not on your PATH. Add it (zsh is the macOS default shell):"
     info "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zprofile && source ~/.zprofile"
     info "    Until then use $REPO/kindle directly." ;;
esac

# ---------------------------------------------------------------------------- 5. config
step "Send-to-Kindle config"
CONF="$CONFIG_DIR/config.toml"
if [ -f "$CONF" ]; then
  ok "keeping existing $CONF"
else
  mkdir -p "$CONFIG_DIR"
  if [ -f "$REPO/config.toml" ]; then cp "$REPO/config.toml" "$CONF"
  else cp "$REPO/math2kindle/config_example.toml" "$CONF"; warn "copied the EXAMPLE config; edit kindle_email/from_email in $CONF"; fi
  chmod 600 "$CONF"
  ok "wrote $CONF"
fi
if [ "$CONF" = "$HOME/.config/math2kindle/config.toml" ]; then
  "$VPY" -c 'from math2kindle.send import load_config; c = load_config(); print("    ok  config parses: %s -> %s via %s:%s" % (c.from_email, c.kindle_email, c.smtp_host, c.smtp_port))' \
    || warn "config did not validate; see the message above"
fi

# ---------------------------------------------------------------------------- 6. smoke test
if [ "$RUN_TESTS" = "1" ]; then
  step "Smoke test (tests/test_send.py, local fake SMTP server; nothing is e-mailed)"
  (cd "$REPO" && "$VPY" -m pytest -q -x -p no:cacheprovider tests/test_send.py)
  ok "smoke test passed"
fi

# ---------------------------------------------------------------------------- next steps
trap - EXIT
printf '\n%sDone.%s Next steps (details: %s/docs/MAC_SETUP.md):\n' "$G$B" "$N" "$REPO"
cat <<EOF
  1. Amazon > Manage Your Content and Devices > Preferences > Personal Document Settings:
     add the sender from your config to the Approved Personal Document E-mail List.
  2. Create a Gmail App Password (needs 2-Step Verification): https://myaccount.google.com/apppasswords
  3. $REPO/kindle set-password     # stores it in the macOS Keychain
  4. $REPO/kindle check            # tests the Gmail login; sends nothing
  5. $REPO/kindle 2101.00001       # convert an arXiv paper and send it to your Kindle
EOF
if [ "$WITH_TEX" = "0" ] && [ ! -x "$TEXBIN/pdflatex" ] && ! command -v pdflatex >/dev/null 2>&1; then
  printf '  (optional) re-run with --with-tex for TikZ figures and the LaTeX fallback renderer.\n'
fi
