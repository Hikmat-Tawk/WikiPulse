"""Report figures for the benchmark (Phase 3). Reads only measured files; writes PNGs to reports/figures/."""
from __future__ import annotations

import argparse
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src.config import EXPERIMENTS, ROOT

FIG = ROOT / "reports" / "figures"
METRICS = EXPERIMENTS / "metrics"
ORDER = ["api", "bs4", "selenium"]
LABEL = {"api": "API", "bs4": "BS4", "selenium": "Selenium"}
METHOD_COLOR = {"api": "#2a78d6", "bs4": "#eb6834", "selenium": "#1baf7a"}
COMP = ["#6250d6", "#e87ba4", "#eda100", "#008300"]  # violet, magenta, yellow, green
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK,
                     "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
                     "axes.titlesize": 11, "axes.titleweight": "bold"})


def _save(fig, name: str) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / name, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("->", FIG / name)


def required_metrics(runs: pd.DataFrame, bench_id: str) -> None:
    """Small multiples: the 4 required metrics, mean ± std over runs (one y-axis per metric)."""
    g = runs.groupby("method")
    panels = [("bandwidth_kb", "Bandwidth (KB, app-level)", "{:.0f}"), ("time_s", "Time (s)", "{:.1f}"),
              ("n_requests", "# HTTP requests", "{:.1f}"), ("avg_latency_ms", "Avg latency (ms, all requests)", "{:.0f}")]
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.6))
    for ax, (col, title, fmt) in zip(axes, panels):
        m, s = g[col].mean().reindex(ORDER), g[col].std().reindex(ORDER)
        x = range(len(ORDER))
        ax.bar(x, m, yerr=s, color=[METHOD_COLOR[k] for k in ORDER], width=0.6, capsize=4,
               error_kw={"ecolor": INK2, "elinewidth": 1}, edgecolor=SURFACE, linewidth=2)
        for i, v in enumerate(m):
            ax.text(i, v + (s.iloc[i] if pd.notna(s.iloc[i]) else 0), fmt.format(v), ha="center", va="bottom",
                    fontsize=9, color=INK)
        if col == "avg_latency_ms":
            md = g["main_latency_ms"].mean().reindex(ORDER)
            ax.scatter([i + 0.38 for i in x], md, marker="D", s=40, color=INK, zorder=3,
                       label="main page request only")
            ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.12))
        ax.set_xticks(list(x), [LABEL[k] for k in ORDER])
        ax.set_title(title)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    n = int(g.size().min())
    fig.suptitle(f"Same workload (newest 500 edits), {n} runs per method, mean ± std", color=INK2, fontsize=10, y=1.02)
    _save(fig, f"{bench_id}_required_metrics.png")


def bandwidth_breakdown(runs: pd.DataFrame, split: pd.DataFrame, bench_id: str) -> None:
    """Where the bytes go: (A) traffic to Wikimedia per method; (B) Selenium runs incl. browser background traffic."""
    r = runs.merge(split[["experiment_id", "target_kb", "other_kb"]], on="experiment_id")
    g = r.groupby("method")[["bandwidth_kb_main", "bandwidth_kb_supplementary", "bandwidth_kb", "target_kb",
                             "other_kb"]].mean().reindex(ORDER)
    g["overhead"] = (g.target_kb - g.bandwidth_kb).clip(lower=0)
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 3.8), gridspec_kw={"width_ratios": [1.1, 1]})
    parts = [("bandwidth_kb_main", "data request (page / API response)"),
             ("bandwidth_kb_supplementary", "browser assets (JS, CSS, images)"),
             ("overhead", "TCP/IP + TLS overhead (pcap − app)")]
    left = pd.Series(0.0, index=ORDER)
    for (col, lab), c in zip(parts, COMP):
        a.barh([LABEL[k] for k in ORDER], g[col], left=left, color=c, edgecolor=SURFACE, linewidth=2, label=lab)
        left += g[col]
    for i, k in enumerate(ORDER):
        a.text(left[k] + 8, i, f"{left[k]:.0f} KB", va="center", fontsize=9)
    a.set_title("A. Traffic to Wikimedia per run (pcap)")
    a.set_xlabel("KB")
    a.invert_yaxis()
    a.set_xlim(0, left.max() * 1.18)
    a.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2)
    a.grid(axis="x", color=GRID, linewidth=0.8)
    a.set_axisbelow(True)

    sel = r[r.method == "selenium"].sort_values("round")
    labels = [f"run {int(x)}" for x in sel["round"]]
    b.barh(labels, sel.target_kb / 1024, color=COMP[0], edgecolor=SURFACE, linewidth=2, label="Wikimedia")
    b.barh(labels, sel.other_kb / 1024, left=sel.target_kb / 1024, color=COMP[3], edgecolor=SURFACE, linewidth=2,
           label="browser background (Google services, incl. QUIC)")
    for i, (t, o) in enumerate(zip(sel.target_kb, sel.other_kb)):
        b.text((t + o) / 1024 + 0.2, i, f"{(t + o) / 1024:.1f} MB", va="center", fontsize=9)
    b.set_title("B. Selenium: everything the browser sent/received")
    b.set_xlabel("MB")
    b.invert_yaxis()
    b.set_xlim(0, (sel.target_kb + sel.other_kb).max() / 1024 * 1.18)
    b.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2)
    b.grid(axis="x", color=GRID, linewidth=0.8)
    b.set_axisbelow(True)
    _save(fig, f"{bench_id}_bandwidth_breakdown.png")


def time_breakdown(runs: pd.DataFrame, bench_id: str) -> None:
    """Application overhead vs network: browser startup, network/page load, parsing, rest (file writes, driver quit)."""
    g = runs.groupby("method")[["startup_s", "network_s", "parse_s", "time_s"]].mean().reindex(ORDER)
    g["other"] = (g.time_s - g.startup_s - g.network_s - g.parse_s).clip(lower=0)
    parts = [("startup_s", "browser startup"), ("network_s", "network / page load"), ("parse_s", "parsing"),
             ("other", "other (writes, browser shutdown)")]
    fig, ax = plt.subplots(figsize=(9, 3))
    left = pd.Series(0.0, index=ORDER)
    for (col, lab), c in zip(parts, COMP):
        ax.barh([LABEL[k] for k in ORDER], g[col], left=left, color=c, edgecolor=SURFACE, linewidth=2, label=lab)
        left += g[col]
    for i, k in enumerate(ORDER):
        ax.text(left[k] + 0.3, i, f"{left[k]:.1f} s", va="center", fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, left.max() * 1.15)
    ax.set_xlabel("seconds (mean over runs)")
    ax.set_title("Where the time goes")
    ax.legend(frameon=False, fontsize=8, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.28))
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _save(fig, f"{bench_id}_time_breakdown.png")


def selenium_resources(runs: pd.DataFrame, bench_id: str) -> None:
    """Selenium: bytes per resource type (DevTools), mean per run."""
    rows = []
    for exp in runs.loc[runs.method == "selenium", "experiment_id"]:
        for line in (METRICS / f"{exp}.requests.jsonl").read_text().splitlines():
            r = json.loads(line)
            rows.append({"exp": exp, "type": r.get("resource_type") or "?", "kb": (r.get("body_bytes_wire") or 0) / 1024})
    d = pd.DataFrame(rows)
    nruns = d.exp.nunique()
    t = d.groupby("type").agg(kb=("kb", "sum"), n=("kb", "size"))
    t = (t / nruns).sort_values("kb")
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.barh(t.index, t.kb, color=METHOD_COLOR["selenium"], edgecolor=SURFACE, linewidth=2)
    for i, (kb, n) in enumerate(zip(t.kb, t.n)):
        ax.text(kb + 5, i, f"{kb:.0f} KB · {n:.1f} req", va="center", fontsize=9)
    ax.set_xlim(0, t.kb.max() * 1.3)
    ax.set_xlabel("KB per run (Chrome DevTools, mean)")
    ax.set_title("Selenium: what the browser downloads from Wikipedia")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _save(fig, f"{bench_id}_selenium_resources.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench-id", default="bench_main")
    ap.add_argument("--pcap-stem", default="scraper_trace")
    args = ap.parse_args()
    runs = pd.read_csv(METRICS / f"{args.bench_id}.runs.csv")
    runs = runs[runs.collection_status == "ok"]
    required_metrics(runs, args.bench_id)
    time_breakdown(runs, args.bench_id)
    selenium_resources(runs, args.bench_id)
    split_path = METRICS / f"{args.pcap_stem}.traffic_split.csv"
    if split_path.exists():
        bandwidth_breakdown(runs, pd.read_csv(split_path), args.bench_id)


if __name__ == "__main__":
    main()
