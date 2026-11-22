"""Threads 1 vs 4: bounded concurrency under a shared rate limit."""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src.collectors.api_collector import ApiCollector, plan_windows
from src.config import EXPERIMENTS, ROOT
from src.http_client import PoliteClient
from src.networking.mock_server import MockState, start

MAX_WORKERS = 4


def run_config(target: str, workers: int, windows: list[datetime], min_interval: float, port: int) -> tuple[dict, list]:
    """Fetch all windows with `workers` threads sharing one rate-limited client."""
    client = PoliteClient(f"threads{workers}", min_interval_s=min_interval)
    if target == "api":
        col = ApiCollector(client)
        task = lambda w: len(col.window(w))
    else:
        task = lambda w: client.get(f"http://127.0.0.1:{port}/w/api.php").json()["request_no"] and 500
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = list(pool.map(task, windows))
    wall = time.perf_counter() - t0
    recs = client.records_as_dicts()
    lat = [r["latency_ms"] for r in recs]
    starts = sorted(datetime.fromisoformat(r["started_utc"]).timestamp() for r in recs)
    return ({"workers": workers, "requests": len(recs), "edits": sum(counts), "wall_s": round(wall, 2),
             "edits_per_s": round(sum(counts) / wall, 1), "avg_latency_ms": round(sum(lat) / len(lat), 1),
             "min_gap_between_starts_s": round(min(b - a for a, b in zip(starts, starts[1:])), 3) if len(starts) > 1 else None,
             "http_429": sum(r["status"] == 429 for r in recs), "retries": sum(r["attempt"] > 0 for r in recs)},
            [{**r, "workers": workers, "t0": t0} for r in recs])


def plot(df: pd.DataFrame, reqs: pd.DataFrame, target: str) -> None:
    colors = {1: "#2a78d6", 2: "#eb6834", 3: "#6250d6", 4: "#1baf7a"}
    surface, grid, ink2 = "#fcfcfb", "#e4e3df", "#52514e"
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 3.8), facecolor=surface, gridspec_kw={"width_ratios": [1, 1.6]})
    g = df.groupby("workers").agg(wall=("wall_s", "mean"), wall_sd=("wall_s", "std"), eps=("edits_per_s", "mean"))
    a.bar([f"{w} thread{'s' if w > 1 else ''}" for w in g.index], g.wall, yerr=g.wall_sd.fillna(0), capsize=4,
          color=[colors[w] for w in g.index], edgecolor=surface, linewidth=2, width=0.55)
    for i, (w, sd, v) in enumerate(zip(g.wall, g.wall_sd.fillna(0), g.eps)):
        a.text(i, w + sd + g.wall.max() * 0.03, f"{w:.1f} s\n{v:.0f} edits/s", ha="center", va="bottom", fontsize=9)
    a.set_ylabel("wall time (s)")
    a.set_ylim(0, g.wall.max() * 1.3)
    a.set_title("Time for the same workload", fontweight="bold")
    first = reqs[reqs.repeat == reqs.repeat.min()]
    y = 0
    labels = []
    for w in sorted(first.workers.unique()):
        d = first[first.workers == w].copy()
        d["s"] = pd.to_datetime(d.started_utc).map(lambda t: t.timestamp())
        base = d.s.min()
        for _, r in d.sort_values("s").iterrows():
            b.barh(y, r.total_ms / 1000, left=r.s - base, color=colors[w], height=0.8, edgecolor=surface, linewidth=1)
            y += 1
        labels.append((y - len(d) / 2 - 0.5, f"{w} thread{'s' if w > 1 else ''}"))
        y += 1
    b.set_yticks([p for p, _ in labels], [t for _, t in labels])
    b.invert_yaxis()
    b.set_xlabel("seconds since first request")
    b.set_title("Each bar = one request (start → end)", fontweight="bold")
    for ax in (a, b):
        ax.set_facecolor(surface)
        ax.grid(axis="x" if ax is b else "y", color=grid, lw=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(colors=ink2)
    path = ROOT / "reports" / "figures" / f"threads_{target}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    print("->", path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["api", "mock"], default="api")
    ap.add_argument("--workers", default="1,4")
    ap.add_argument("--windows", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--min-interval", type=float, default=1.0)
    ap.add_argument("--delay", type=float, default=2.0, help="mock only: server delay per request (s)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    workers = [int(w) for w in args.workers.split(",")]
    if max(workers) > MAX_WORKERS:
        raise SystemExit(f"max {MAX_WORKERS} threads (bounded, polite experiment)")

    now = datetime.now(timezone.utc)
    windows = plan_windows(now - timedelta(days=29), now - timedelta(days=1), args.windows, args.seed)
    server = None
    if args.target == "mock":
        server, state = start(8430, MockState(delay_s=args.delay))
    rows, reqs = [], []
    try:
        for rep in range(1, args.repeats + 1):
            for w in (workers if rep % 2 else workers[::-1]):  # alternate order between repeats
                summary, recs = run_config(args.target, w, windows, args.min_interval, 8430)
                summary["repeat"] = rep
                rows.append(summary)
                reqs += [{**r, "repeat": rep} for r in recs]
                print(summary, flush=True)
                time.sleep(3)
    finally:
        if server:
            server.shutdown()
    df, rq = pd.DataFrame(rows), pd.DataFrame(reqs)
    out = EXPERIMENTS / "metrics" / f"threads_{args.target}.csv"
    df.to_csv(out, index=False)
    rq.to_csv(out.with_suffix(".requests.csv"), index=False)
    print(df.groupby("workers")[["wall_s", "edits_per_s", "avg_latency_ms", "min_gap_between_starts_s"]]
          .agg(["mean", "std"]).round(2).to_string())
    plot(df, rq, args.target)


if __name__ == "__main__":
    main()
