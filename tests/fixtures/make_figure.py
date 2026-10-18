"""Generate the contour figure used by the test paper (run once; output is committed)."""
from pathlib import Path

import matplotlib

matplotlib.use("pdf")
import matplotlib.pyplot as plt  # noqa: E402

out = Path(__file__).parent / "paper_src" / "figures" / "contour.pdf"
fig, ax = plt.subplots(figsize=(4.2, 3.2))
c, T, sig = 1.25, 2.6, -0.6
ax.axhline(0, color="0.6", lw=0.8)
ax.axvline(0, color="0.6", lw=0.8)
ax.axvline(1, color="0.6", lw=0.8, ls=":")
ax.axvline(0.5, color="0.6", lw=0.8, ls="--")
kw = dict(color="black", lw=1.6)
ax.plot([c, c], [-T, T], **kw)
ax.plot([c, sig], [T, T], **kw)
ax.plot([sig, sig], [T, -T], **kw)
ax.plot([sig, c], [-T, -T], **kw)
for (x0, y0, dx, dy) in [(c, -0.3, 0, 0.6), (0.4, T, -0.3, 0), (sig, 0.3, 0, -0.6), (0.2, -T, 0.3, 0)]:
    ax.annotate("", xy=(x0 + dx, y0 + dy), xytext=(x0, y0), arrowprops=dict(arrowstyle="-|>", color="black", lw=1.4))
ax.plot([1], [0], "ko", ms=5)
ax.plot([0], [0], "ko", ms=5, mfc="white")
for g in (1.41, 2.10):
    ax.plot([0.5, 0.5], [g, -g], "kx", ms=6)
ax.text(1.05, 0.12, "$s=1$", fontsize=10)
ax.text(c + 0.06, T - 0.35, "$c+iT$", fontsize=10)
ax.text(c + 0.06, -T + 0.1, "$c-iT$", fontsize=10)
ax.text(0.53, 1.55, r"$\rho$", fontsize=10)
ax.set_xlim(-1.0, 2.0)
ax.set_ylim(-3.1, 3.1)
ax.set_xlabel(r"$\sigma=\Re s$")
ax.set_ylabel(r"$t=\Im s$")
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
fig.tight_layout()
fig.savefig(out)
print("wrote", out)
