"""Summarize an experiment's runs and draw the README charts.

    python results/analyze.py final

Reads results/<prefix>-n<N>-rep<R>-<policy>.csv (+ .metrics.json), writes
results/<prefix>-summary.md and results/figures/<prefix>-*.png.
"""

import csv
import glob
import json
import os
import re
import statistics as st
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
POLICIES = ["round_robin", "cache_aware", "hybrid"]
LABELS = {"round_robin": "Round-robin", "cache_aware": "Cache-aware", "hybrid": "Hybrid"}
# Categorical slots 1-3 of the validated reference palette (light mode).
COLORS = {"round_robin": "#2a78d6", "cache_aware": "#eb6834", "hybrid": "#1baf7a"}
SURFACE, INK, INK_MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"


def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def load(prefix):
    """{(n, policy): [per-run stats]} for every run of this experiment."""
    runs = defaultdict(list)
    for path in glob.glob(os.path.join(HERE, f"{prefix}-n*-rep*-*.csv")):
        m = re.search(rf"{prefix}-n(\d+)-rep(\d+)-(\w+)\.csv$", path)
        n, rep, policy = int(m[1]), int(m[2]), m[3]
        rows = [r for r in csv.DictReader(open(path)) if r["status"] == "200" and r["ttft"]]
        ttft = [float(r["ttft"]) for r in rows]
        hit_rate = None
        metrics_path = path[:-4] + ".metrics.json"
        if os.path.exists(metrics_path):
            hit_rate = json.load(open(metrics_path))["total"]["hit_rate"]
        runs[(n, policy)].append({
            "rep": rep,
            "p50": st.median(ttft),
            "p95": percentile(ttft, 0.95),
            "hit_rate": None if hit_rate is None else 100 * hit_rate,
        })
    return runs


def mean_range(values):
    return st.mean(values), min(values), max(values)


def write_summary(prefix, runs, ns):
    lines = [
        f"# {prefix}: summary",
        "",
        "Mean across repeats, with the min–max range across repeats in brackets.",
        "",
        "| System prompts | Policy | Runs | Cache hit rate (%) | TTFT p50 (s) | TTFT p95 (s) |",
        "|---|---|---|---|---|---|",
    ]
    for n in ns:
        for policy in POLICIES:
            rs = runs.get((n, policy), [])
            if not rs:
                continue
            cells = []
            for key, fmt in [("hit_rate", "{:.1f}"), ("p50", "{:.2f}"), ("p95", "{:.2f}")]:
                vals = [r[key] for r in rs if r[key] is not None]
                if vals:
                    mean, lo, hi = mean_range(vals)
                    cells.append(f"{fmt.format(mean)} [{fmt.format(lo)}–{fmt.format(hi)}]")
                else:
                    cells.append("–")
            lines.append(f"| {n} | {LABELS[policy]} | {len(rs)} | " + " | ".join(cells) + " |")
    path = os.path.join(HERE, f"{prefix}-summary.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def chart(prefix, runs, ns, key, title, ylabel, filename):
    """One line per policy: the mean across repeats, with each repeat as a faint dot,
    so the spread is visible directly instead of summarized by an error bar."""
    if not any(r[key] is not None for rs in runs.values() for r in rs):
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    x = list(range(len(ns)))
    ends = {}
    for policy in POLICIES:
        means = []
        for i, n in enumerate(ns):
            vals = [r[key] for r in runs.get((n, policy), []) if r[key] is not None]
            # Offset each policy slightly so overlapping repeats stay visible.
            dx = (POLICIES.index(policy) - 1) * 0.06
            ax.scatter([i + dx] * len(vals), vals, s=18, color=COLORS[policy], alpha=0.35,
                       linewidths=0, zorder=2)
            means.append(st.mean(vals) if vals else None)
        pts = [(i, m) for i, m in zip(x, means) if m is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=COLORS[policy], lw=2,
                marker="o", ms=8, mec=SURFACE, mew=2, zorder=3)
        ends[policy] = pts[-1]
    ax.set_xlim(-0.3, len(ns) - 1 + 0.95)
    ax.set_xlabel("Distinct system prompts", color=INK_MUTED, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=11, pad=10)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    if key == "hit_rate":
        ax.set_ylim(bottom=0, top=100)
    else:
        ax.set_ylim(bottom=0)
    # Direct labels at the line ends, nudged apart when line ends are too close to
    # read. Each starts with a short key line in the series color, so a nudged label
    # still names its own line; the text itself stays in ink.
    lo, hi = ax.get_ylim()
    gap = 0.07 * (hi - lo)
    placed = []
    for policy, (px, py) in sorted(ends.items(), key=lambda kv: kv[1][1]):
        y = max(py, placed[-1] + gap) if placed else py
        placed.append(y)
        ax.plot([px + 0.12, px + 0.24], [y, y], color=COLORS[policy], lw=2,
                solid_capstyle="round", clip_on=False)
        ax.text(px + 0.29, y, LABELS[policy], va="center", fontsize=9, color=INK)
    ax.set_xticks(x, [str(n) for n in ns])
    os.makedirs(os.path.join(HERE, "figures"), exist_ok=True)
    fig.savefig(os.path.join(HERE, "figures", f"{prefix}-{filename}.png"), facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    prefix = sys.argv[1]
    runs = load(prefix)
    ns = sorted({n for n, _ in runs})
    write_summary(prefix, runs, ns)
    chart(prefix, runs, ns, "hit_rate", "Prefix cache hit rate", "Prompt tokens served from cache (%)", "hit-rate")
    chart(prefix, runs, ns, "p50", "Median time to first token", "TTFT p50 (s)", "ttft-p50")
    chart(prefix, runs, ns, "p95", "Tail time to first token", "TTFT p95 (s)", "ttft-p95")
