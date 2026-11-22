"""Benchmark the three collection methods on the same workload."""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import pandas as pd

from src.benchmarking.metrics import load_records, mean_std, summarize_requests
from src.collectors import api_collector, bs4_collector, selenium_collector
from src.config import DATA, EXPERIMENTS
from src.processing.normalizer import normalize_jsonl

COLLECTORS = {"api": api_collector.collect, "bs4": bs4_collector.collect, "selenium": selenium_collector.collect}


def run_one(method: str, n: int, bench_id: str, rnd: int) -> dict:
    """Run one collector once and return a flat result row."""
    exp_id = f"{bench_id}_{method}_r{rnd}"
    start_epoch = time.time()
    t0 = time.perf_counter()
    cpu0 = time.process_time()
    res = COLLECTORS[method](n, exp_id)
    total_s = time.perf_counter() - t0
    cpu_s = time.process_time() - cpu0
    end_epoch = time.time()

    req = summarize_requests(load_records(EXPERIMENTS / "metrics" / f"{exp_id}.requests.jsonl"))
    df, _ = normalize_jsonl(DATA / "raw" / f"{exp_id}.jsonl")
    newest = pd.to_datetime(df["timestamp_utc"], utc=True).max() if len(df) else None
    network_s = res.get("network_idle_s", res.get("fetch_s"))
    return {
        "bench_id": bench_id, "round": rnd, "method": method, "experiment_id": exp_id,
        "start_utc": datetime.fromtimestamp(start_epoch, timezone.utc).isoformat(),
        "start_epoch": round(start_epoch, 3), "end_epoch": round(end_epoch, 3),
        "records": len(df),
        "time_s": round(total_s, 3),
        "startup_s": res.get("startup_s", 0.0),       # browser launch (Selenium only)
        "network_s": network_s,                        # fetch / page load until network idle
        "parse_s": res.get("parse_s", 0.0),
        "python_cpu_s": round(cpu_s, 3),
        **req,
        "kb_per_record": round(req["bandwidth_kb"] / len(df), 3) if len(df) else None,
        "records_per_s": round(len(df) / total_s, 1) if total_s else None,
        "freshness_newest_s": round(end_epoch - newest.timestamp(), 1) if newest is not None else None,
        "protocols": ",".join(res["protocols"]) if "protocols" in res else "http/1.1",
        "distinct_connections_app": res.get("distinct_connections"),
    }


SUMMARY_COLS = ["time_s", "bandwidth_kb", "n_requests", "avg_latency_ms", "bandwidth_kb_main",
                "bandwidth_kb_supplementary", "n_requests_supplementary", "main_latency_ms", "startup_s",
                "network_s", "parse_s", "python_cpu_s", "kb_per_record", "records_per_s", "freshness_newest_s",
                "records"]


def summarize(runs: pd.DataFrame, ndigits: int | None = 2) -> pd.DataFrame:
    """Mean and std per method (+ number of runs). ndigits=None keeps full precision."""
    out = []
    for method, g in runs.groupby("method", sort=False):
        row = {"method": method, "runs": len(g)}
        for c in SUMMARY_COLS:
            m, s = mean_std(g[c].tolist())
            rnd = (lambda v: round(v, ndigits)) if ndigits is not None else (lambda v: v)
            row[f"{c}_mean"] = rnd(m) if m is not None else None
            row[f"{c}_std"] = rnd(s) if s is not None else None
        out.append(row)
    return pd.DataFrame(out)


def required_table(summary: pd.DataFrame) -> pd.DataFrame:
    """Pass an UNROUNDED summary (summarize(..., ndigits=None)) to avoid rounding twice."""
    """The assignment's table: Method | bandwidth (KB) | time (s) | #requests | avg latency (ms), as mean ± std."""
    def fmt(r, c, nd):
        return f"{r[c + '_mean']:.{nd}f} ± {r[c + '_std']:.{nd}f}"
    return pd.DataFrame([{"Method": r["method"], "Bandwidth (KB)": fmt(r, "bandwidth_kb", 1),
                          "Time (s)": fmt(r, "time_s", 2), "#Requests": fmt(r, "n_requests", 1),
                          "Avg latency (ms)": fmt(r, "avg_latency_ms", 0), "Runs": r["runs"]}
                         for _, r in summary.iterrows()])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--methods", default="api,bs4,selenium")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between runs")
    ap.add_argument("--bench-id", default=None)
    args = ap.parse_args()
    methods = [m.strip() for m in args.methods.split(",")]
    bench_id = args.bench_id or f"bench_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out_dir = EXPERIMENTS / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{bench_id}: methods={methods} runs={args.runs} n={args.n} pause={args.pause}s")

    rows = []
    for rnd in range(1, args.runs + 1):
        k = (rnd - 1) % len(methods)
        for method in methods[k:] + methods[:k]:  # rotated order
            print(f"[{datetime.now():%H:%M:%S}] round {rnd} {method} ...", flush=True)
            try:
                row = run_one(method, args.n, bench_id, rnd)
                row["collection_status"] = "ok"
            except Exception as exc:
                row = {"bench_id": bench_id, "round": rnd, "method": method, "collection_status": f"failed: {exc}"[:300]}
            rows.append(row)
            print(f"    -> {row.get('records')} records, {row.get('time_s')} s, {row.get('bandwidth_kb')} KB, "
                  f"{row.get('n_requests')} req, latency {row.get('avg_latency_ms')} ms  [{row['collection_status'][:60]}]",
                  flush=True)
            pd.DataFrame(rows).to_csv(out_dir / f"{bench_id}.runs.csv", index=False)  # save after every run
            time.sleep(args.pause)

    runs = pd.DataFrame(rows)
    ok = runs[runs["collection_status"] == "ok"]
    if ok.empty:
        print("no successful runs")
        return
    summary = summarize(ok)
    summary.to_csv(out_dir / f"{bench_id}.summary.csv", index=False)
    table = required_table(summarize(ok, ndigits=None))
    table.to_csv(out_dir / f"{bench_id}.table.csv", index=False)
    print("\n" + table.to_string(index=False))
    print(f"\nruns -> {out_dir / (bench_id + '.runs.csv')}\nsummary -> {out_dir / (bench_id + '.summary.csv')}")


if __name__ == "__main__":
    main()
