# Mac setup (Apple Silicon)

The repo lives at `~/development/math2kindle`. Everything below is run in
Terminal; nothing needs conda (the installer uses Homebrew's Python and ignores
an active conda env).

## 1. Run the installer

```sh
cd ~/development/math2kindle && bash install_mac.sh --with-tex
```

It installs `python@3.12`, `node`, `pandoc` and `ghostscript` with Homebrew
(`~/.homebrew` or `/opt/homebrew`), creates `.venv`, runs `pip install -e .`,
installs MathJax 4 + resvg with `npm ci`, links `kindle` and `math2kindle` into
`~/.local/bin`, copies `config.toml` to `~/.config/math2kindle/config.toml`
(only if absent) and runs a smoke test. It is safe to re-run.

- `--with-tex` adds BasicTeX and the TeX packages for TikZ/tikz-cd/xy-pic
  figures and the LaTeX fallback renderer. It asks for your Mac password
  (`sudo tlmgr`). Leave it off for a quicker install.
- `--with-marker` adds the ML backend for PDFs that have no LaTeX source
  (marker-pdf + torch, which uses the Apple GPU via MPS) and Homebrew's
  `llama.cpp`, whose `llama-server` runs marker's vision model (skipped if
  `llama-server` is already on your PATH). The first PDF conversion downloads
  several GB of models. If you installed marker earlier without it, run
  `brew install llama.cpp` (or re-run the installer with `--with-marker`).

If it says `~/.local/bin` is not on your PATH, run the `echo ... >> ~/.zprofile`
line it prints, or use `~/development/math2kindle/kindle` directly.

## 2. Approve your Gmail address at Amazon

Amazon → **Manage Your Content and Devices → Preferences → Personal Document
Settings → Approved Personal Document E-mail List** → add
`you@gmail.com`. Amazon silently drops mail from addresses not on
this list. On the same page, note the Kindle's address (like
`yourname_XXXX@kindle.com`) for `kindle_email` in `config.toml`.

## 3. Create a Gmail App Password

1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
   (App Passwords require it).
2. Create one at <https://myaccount.google.com/apppasswords>, name it
   `math2kindle`, and copy the 16-character code.

## 4. Store it in the Keychain

```sh
kindle set-password
```

This is the same as `.venv/bin/python -m math2kindle.send set-password`. It
prompts twice without echoing the code. The password is never written to the
config file. If macOS asks whether Python may access the Keychain, choose
**Always Allow**.

## 5. Check the login

```sh
kindle check
```

This validates `~/.config/math2kindle/config.toml` and logs in to
`smtp.gmail.com:587` (STARTTLS). Nothing is sent.

## 6. Send a paper

```sh
kindle 2101.00001
```

The EPUB goes to `~/Documents/math2kindle/` (change it with `--out DIR` or
`M2K_OUT`) and is e-mailed to your Kindle. It usually arrives within a few
minutes. The first time, Amazon may e-mail `you@gmail.com` asking
you to verify the document. Click the link or it won't be delivered.

Other forms:

```sh
kindle https://arxiv.org/abs/math/0601001v2
kindle ~/Downloads/paper.pdf --no-send        # convert only
kindle 2101.00001 --dry-run                   # writes the .eml, sends nothing
kindle send ~/Documents/math2kindle/Some\ Paper.epub
```

A PDF you downloaded from arXiv is recognised by its margin stamp
(`arXiv:1406.7309v1 [math.MG] ...`); `kindle` then fetches the paper's LaTeX
source, which gives exact maths, and converts the PDF only if arXiv has no
source or you are offline (`--no-arxiv-source` skips the lookup).

If marker cannot run (for example `llama-server` is missing), `kindle` stops
with the cause and the fix and writes nothing, rather than mailing a book with
garbled formulas. `--allow-text-fallback` writes a text-only EPUB anyway; it is
never e-mailed.

Any other option goes straight to `math2kindle` (see `math2kindle --help`).
Troubleshooting is in [SEND_TO_KINDLE.md](SEND_TO_KINDLE.md).
