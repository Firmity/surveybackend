"""
Chart rendering for the PDF report (matplotlib -> PNG bytes). Editorial look:
cream background, Work Sans, bold black numbers, blue/lime accents. Status colors
(green/amber/red) are kept where they carry meaning (scores, ratings, priority).
Each function returns PNG bytes or None on failure (renderer skips missing charts).
"""
from __future__ import annotations

import io
import logging
import os
from typing import Callable, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.font_manager as fm  # noqa: E402
from matplotlib.patches import Wedge  # noqa: E402

log = logging.getLogger("charts")

_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
try:
    for _f in ("WorkSans-Regular.ttf", "WorkSans-Bold.ttf", "WorkSans-Italic.ttf"):
        _p = os.path.join(_FONT_DIR, _f)
        if os.path.exists(_p):
            fm.fontManager.addfont(_p)
    plt.rcParams["font.family"] = "Work Sans"
except Exception as e:  # noqa: BLE001
    log.warning("[FONT_LOAD] %s", e)

# Colours come from the editable theme (report_theme.py); converted RGB -> hex here.
from . import report_theme as _t  # noqa: E402


def _hx(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(c) for c in rgb)


INK = _hx(_t.INK)
BLUE = _hx(_t.BLUE)
LIME = _hx(_t.LIME)
CREAM = _hx(_t.CREAM)
MUTED = _hx(_t.MUTED)
GREEN = _hx(_t.GREEN)
LIME_G = _hx(_t.LIME_G)
AMBER = _hx(_t.AMBER)
RED = _hx(_t.RED)
SLATE = _hx(_t.SLATE)
GREY = "#e6e2d6"


def score_color(s: Optional[float]) -> str:
    if s is None:
        return SLATE
    if s >= 85:
        return GREEN
    if s >= 70:
        return LIME_G
    if s >= 50:
        return AMBER
    return RED


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return buf.getvalue()


def _safe(fn: Callable[..., bytes]) -> Callable[..., Optional[bytes]]:
    def wrapped(*a, **k) -> Optional[bytes]:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            log.warning("[CHART_ERR] %s: %s", fn.__name__, e)
            plt.close("all")
            return None
    return wrapped


@_safe
def gauge(score: Optional[float], grade: str = "") -> bytes:
    """Thin-ring gauge: light base ring + colored arc + big black center number."""
    s = 0 if score is None else max(0, min(100, score))
    col = score_color(s)
    fig, ax = plt.subplots(figsize=(2.9, 2.9))
    ax.set_aspect("equal"); ax.axis("off"); ax.set_xlim(-1.25, 1.25); ax.set_ylim(-1.25, 1.25)
    ax.add_patch(Wedge((0, 0), 1.0, 0, 360, width=0.12, facecolor=GREY, edgecolor="none"))
    theta1 = 90 - (s / 100.0) * 360
    ax.add_patch(Wedge((0, 0), 1.0, theta1, 90, width=0.12, facecolor=col, edgecolor="none"))
    ax.text(0, 0.06, f"{int(round(s))}", ha="center", va="center", fontsize=42, fontweight="bold", color=INK)
    ax.text(0, -0.36, (f"/ 100   {grade}").strip(), ha="center", va="center", fontsize=12, color=MUTED)
    return _png(fig)


@_safe
def category_bars(rows: list[tuple[str, float, int]]) -> bytes:
    rows = rows[:12]
    labels = [r[0] for r in rows]
    scores = [max(0, min(100, r[1])) for r in rows]
    colors = [score_color(s) for s in scores]
    fig, ax = plt.subplots(figsize=(7.6, max(1.4, 0.55 * len(rows) + 0.4)))
    y = range(len(rows))
    ax.barh(list(y), scores, color=colors, height=0.62, zorder=3)
    ax.set_xlim(0, 108)
    ax.set_yticks(list(y)); ax.set_yticklabels(labels, fontsize=11, color=INK)
    ax.invert_yaxis()
    for i, s in enumerate(scores):
        ax.text(s + 2, i, f"{int(round(s))}", va="center", ha="left", fontsize=11, fontweight="bold", color=INK)
    ax.set_xticks([0, 50, 70, 85, 100]); ax.tick_params(axis="x", labelsize=8, colors=MUTED)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.xaxis.grid(True, color=GREY, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
    return _png(fig)


@_safe
def rating_donut(good: int, satis: int, poor: int, na: int = 0) -> bytes:
    data = [("Good", good, GREEN), ("Satisfactory", satis, AMBER), ("Unsatisfactory", poor, RED)]
    if na:
        data.append(("N/A", na, SLATE))
    data = [d for d in data if d[1] > 0]
    if not data:
        raise ValueError("no rating data")
    vals = [d[1] for d in data]; cols = [d[2] for d in data]; total = sum(vals)
    fig, ax = plt.subplots(figsize=(3.1, 3.1))
    ax.pie(vals, colors=cols, startangle=90, counterclock=False,
           wedgeprops=dict(width=0.40, edgecolor=CREAM, linewidth=2.5))
    ax.text(0, 0.12, f"{total}", ha="center", va="center", fontsize=27, fontweight="bold", color=INK)
    ax.text(0, -0.24, "graded", ha="center", va="center", fontsize=11, color=MUTED)
    ax.legend([d[0] for d in data], loc="center", bbox_to_anchor=(0.5, -0.14),
              ncol=len(data), frameon=False, fontsize=8, handlelength=1)
    return _png(fig)


@_safe
def action_priority(high: int, medium: int) -> bytes:
    fig, ax = plt.subplots(figsize=(4.6, 1.5))
    ax.barh(["High", "Medium"], [high, medium], color=[RED, AMBER], height=0.62, zorder=3)
    for i, v in enumerate([high, medium]):
        ax.text(v + max(high, medium, 1) * 0.02, i, str(v), va="center", fontsize=13, fontweight="bold", color=INK)
    ax.invert_yaxis(); ax.set_xlim(0, max(high, medium, 1) * 1.2)
    ax.tick_params(axis="y", labelsize=11, colors=INK); ax.set_xticks([])
    for sp in ("top", "right", "bottom"):
        ax.spines[sp].set_visible(False)
    return _png(fig)


@_safe
def gantt(rows: list[tuple[str, float, float, str]], weeks: int = 8) -> bytes:
    """rows: [(label, start_week, end_week, priority)] -> Gantt bars over weeks."""
    rows = rows[:14]
    if not rows:
        raise ValueError("no rows")
    fig, ax = plt.subplots(figsize=(7.8, max(1.8, 0.55 * len(rows) + 0.7)))
    for i, (label, s, e, pri) in enumerate(rows):
        ax.barh(i, max(e - s, 0.4), left=s, height=0.52,
                color=(RED if pri == "high" else AMBER), zorder=3,
                edgecolor="white", linewidth=0.5)
    ax.set_yticks(range(len(rows))); ax.set_yticklabels([r[0] for r in rows], fontsize=9.5, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, weeks)
    ax.set_xticks(range(0, weeks + 1))
    ax.set_xticklabels(["Now"] + [f"W{w}" for w in range(1, weeks + 1)], fontsize=8.5, color=MUTED)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.xaxis.grid(True, color=GREY, linewidth=0.9, zorder=0); ax.set_axisbelow(True)
    # re-inspection marker
    ax.axvline(weeks - 0.02, color=BLUE, linewidth=1.4, linestyle=(0, (3, 2)), zorder=4)
    ax.text(weeks, -0.7, "Re-inspect", ha="right", va="bottom", fontsize=8.5, color=BLUE, fontweight="bold")
    return _png(fig)


@_safe
def risk_heatmap(cats: list[str], areas: list[str], matrix: list[list[Optional[float]]]) -> bytes:
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("ryg", [RED, AMBER, LIME_G, GREEN])
    arr = np.array([[(v if v is not None else np.nan) for v in row] for row in matrix], dtype=float)
    fig, ax = plt.subplots(figsize=(max(3.6, 1.3 * len(areas) + 2.4), max(1.6, 0.55 * len(cats) + 0.8)))
    ax.imshow(arr, cmap=cmap, vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(areas))); ax.set_xticklabels(areas, fontsize=9.5, color=INK, rotation=18, ha="right")
    ax.set_yticks(range(len(cats))); ax.set_yticklabels(cats, fontsize=9.5, color=INK)
    for i in range(len(cats)):
        for j in range(len(areas)):
            v = matrix[i][j]
            if v is None:
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=GREY, edgecolor=CREAM))
                continue
            ax.text(j, i, f"{int(round(v))}", ha="center", va="center", fontsize=10, fontweight="bold", color="white")
    ax.set_xticks([x - 0.5 for x in range(1, len(areas))], minor=True)
    ax.set_yticks([y - 0.5 for y in range(1, len(cats))], minor=True)
    ax.grid(which="minor", color=CREAM, linewidth=3); ax.tick_params(which="both", length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    return _png(fig)
