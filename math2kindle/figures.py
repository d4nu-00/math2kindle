"""Convert figures (PDF/EPS/PS/SVG/PNG/JPEG) to grayscale raster images for e-ink."""

from __future__ import annotations

from pathlib import Path

from .util import log, run, which

MAX_W = 1200          # px; Kindle Paperwhite screens are 1072-1264 px wide
TEXT_WIDTH_PT = 345.0  # LaTeX article \textwidth, used to turn natural sizes into %
FIG_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg", ".gif", ".bmp", ".tif", ".tiff",
            ".PDF", ".PNG", ".JPG", ".JPEG", ".EPS")


def find_graphic(name: str, bases: list[Path], root: Path) -> Path | None:
    name = name.strip().strip('"')
    cand_names = [name] + [name + e for e in FIG_EXTS]
    root_r = root.resolve()
    for b in bases:
        for c in cand_names:
            p = (b / c)
            try:
                rp = p.resolve()
            except OSError:
                continue
            if (root_r == rp or root_r in rp.parents) and rp.is_file():
                return rp
    return None


def _trim(img, pad: int = 12):
    from PIL import ImageOps

    g = img.convert("L")
    bbox = ImageOps.invert(g).point(lambda v: 255 if v > 8 else 0).getbbox()
    if bbox:
        g = g.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                    min(g.width, bbox[2] + pad), min(g.height, bbox[3] + pad)))
    return g


def convert_graphic(src: Path, out_dir: Path, stem: str) -> tuple[Path, float | None] | None:
    """Return (output image, natural width in pt or None)."""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    natural_pt: float | None = None
    try:
        if ext == ".pdf":
            import pymupdf

            doc = pymupdf.open(str(src))
            page = doc[0]
            natural_pt = page.rect.width
            zoom = min(300 / 72.0, MAX_W * 1.4 / max(1.0, page.rect.width))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            doc.close()
        elif ext in (".eps", ".ps"):
            gs = which("gs") or which("gswin64c")
            if not gs:
                log.warning("Ghostscript not found; cannot convert %s", src.name)
                return None
            tmp = out_dir / f"{stem}.gs.png"
            proc = run([gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop", "-sDEVICE=pnggray", "-r250",
                        f"-sOutputFile={tmp}", str(src)], timeout=120)
            if proc.returncode != 0 or not tmp.exists():
                log.warning("Ghostscript failed on %s", src.name)
                return None
            img = Image.open(tmp)
            img.load()
            tmp.unlink(missing_ok=True)
            natural_pt = img.width * 72 / 250
        elif ext == ".svg":
            try:
                import cairosvg  # type: ignore

                png = cairosvg.svg2png(url=str(src), output_width=MAX_W)
                import io

                img = Image.open(io.BytesIO(png))
            except Exception:
                conv = which("rsvg-convert")
                if not conv:
                    return None
                tmp = out_dir / f"{stem}.svg.png"
                run([conv, "-w", str(MAX_W), "-o", str(tmp), str(src)], timeout=60)
                img = Image.open(tmp)
                img.load()
                tmp.unlink(missing_ok=True)
        else:
            img = Image.open(src)
            img.load()
            dpi = img.info.get("dpi", (None,))[0]
            if dpi:
                try:
                    natural_pt = img.width * 72.0 / float(dpi)
                except (TypeError, ZeroDivisionError):
                    natural_pt = None
    except Exception as e:  # pragma: no cover - depends on file
        log.warning("Could not convert figure %s: %s", src.name, e)
        return None
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    g = _trim(img)
    if g.width > MAX_W:
        h = round(g.height * MAX_W / g.width)
        g = g.resize((MAX_W, max(1, h)), Image.LANCZOS)
    hist = g.histogram()
    total = max(1, sum(hist))
    extremes = sum(hist[:48]) + sum(hist[208:])
    photo = extremes / total < 0.8          # mostly mid-tones => photograph, keep JPEG
    if not photo and ext in (".jpg", ".jpeg", ".webp"):
        g = g.point(lambda v: 255 if v > 232 else v)   # clean JPEG ringing around line art
    out = out_dir / (f"{stem}.jpg" if photo else f"{stem}.png")
    if photo:
        g.save(out, quality=85, optimize=True)
    else:
        g.save(out, optimize=True)
    return out, natural_pt


def width_percent(opts: str | None, natural_pt: float | None) -> int | None:
    """CSS width (%) from \\includegraphics options or the natural size."""
    import re

    if opts:
        m = re.search(r"width\s*=\s*([\d.]*)\s*\\(textwidth|linewidth|columnwidth|hsize)", opts)
        if m:
            f = float(m.group(1) or 1)
            return max(10, min(100, round(f * 100)))
        m = re.search(r"width\s*=\s*([\d.]+)\s*(pt|cm|mm|in|bp)", opts)
        if m:
            v = float(m.group(1)) * {"pt": 1, "bp": 1.00375, "cm": 28.45, "mm": 2.845, "in": 72.27}[m.group(2)]
            return max(10, min(100, round(100 * v / TEXT_WIDTH_PT)))
        m = re.search(r"scale\s*=\s*([\d.]+)", opts)
        if m and natural_pt:
            return max(10, min(100, round(100 * float(m.group(1)) * natural_pt / TEXT_WIDTH_PT)))
    if natural_pt:
        return max(25, min(100, round(100 * natural_pt / TEXT_WIDTH_PT)))
    return None
