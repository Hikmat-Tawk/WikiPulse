"""Resilience test: collect repeatedly while HTTPS is blocked and unblocked by the firewall."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src.collectors import bs4_collector, selenium_collector
from src.collectors.api_collector import ApiCollector
from src.config import EXPERIMENTS, ROOT
from src.http_client import PoliteClient

EVENTS = EXPERIMENTS / "captures" / "firewall_events.log"


def error_kind(text: str | None) -> str | None:
    """Classify an error message: what does it say about the network?"""
    if not text:
        return None
    t = text.lower()
    if "connecttimeout" in t or "connect timeout" in t or "err_connection_timed_out" in t:
        return "connect_timeout"
    if "operation not permitted" in t:
        return "blocked_locally"
    if "refused" in t or "unreachable" in t or "err_connection" in t or "newconnectionerror" in t:
        return "refused"
    if "readtimeout" in t or "read timed out" in t or "read timeout" in t:
        return "read_timeout"
    if "timeout" in t:
        return "timeout_other"
    return "other"


def collect_once(method: str, n: int, exp_id: str, connect_timeout: float, read_timeout: float,
                 retries: int) -> tuple[int, list[dict]]:
    """One collection. Returns (records, request records). Raises on final failure."""
    if method == "selenium":
        os.environ.setdefault("SE_OFFLINE", "true")
        res = selenium_collector.collect(n, exp_id, max_retries=retries, timeout_s=read_timeout)
        recs = [json.loads(l) for l in (EXPERIMENTS / "metrics" / f"{exp_id}.requests.jsonl").read_text().splitlines()]
        return res["rows"], recs
    client = PoliteClient(method, max_retries=retries, timeout_s=read_timeout, connect_timeout_s=connect_timeout)
    try:
        if method == "api":
            rows = len(ApiCollector(client).latest(n))
        else:
            rows = bs4_collector.collect(n, exp_id, client=client)["rows"]
        return rows, client.records_as_dicts()
    finally:
        collect_once.last_records = client.records_as_dicts()  # keep the failed attempts too


def run(method: str, duration: float, interval: float, connect_timeout: float, read_timeout: float, retries: int,
        n: int, run_id: str) -> pd.DataFrame:
    out = EXPERIMENTS / "metrics" / f"{run_id}.resilience.csv"
    if out.exists():
        raise SystemExit(f"{out.name} already exists - choose a new --id (results are never overwritten)")
    rows = []
    t_begin = time.time()
    k = 0
    while time.time() - t_begin < duration:
        k += 1
        exp_id = f"{run_id}_{k:03d}"
        start = time.time()
        collect_once.last_records = []
        try:
            n_rows, recs = collect_once(method, n, exp_id, connect_timeout, read_timeout, retries)
            status, error = "ok", None
        except Exception as exc:  # the failure *is* the measurement here
            n_rows, recs = 0, getattr(collect_once, "last_records", [])
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:200]
        end = time.time()
        if method != "selenium":
            (EXPERIMENTS / "metrics" / f"{exp_id}.requests.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        attempts_err = [r.get("error") for r in recs if r.get("error")]
        kinds = sorted({error_kind(e) for e in attempts_err + ([error] if error else [])} - {None})
        rows.append({"run_id": run_id, "k": k, "method": method, "start_epoch": round(start, 3), "end_epoch": round(end, 3),
                     "duration_s": round(end - start, 2), "status": status, "records": n_rows,
                     "http_attempts": len(recs), "failed_attempts": len(attempts_err),
                     "backoff_wait_s": round(sum(r.get("retry_wait_s") or 0 for r in recs), 2),
                     "error_kinds": ",".join(kinds) or None,
                     "first_attempt_error": attempts_err[0][:160] if attempts_err else None, "error": error})
        r = rows[-1]
        print(f"[{datetime.now():%H:%M:%S}] #{k} {status:6s} {r['duration_s']:6.1f}s records={n_rows} "
              f"attempts={r['http_attempts']} failed={r['failed_attempts']} wait={r['backoff_wait_s']}s "
              f"errors={r['error_kinds'] or '-'}", flush=True)
        pd.DataFrame(rows).to_csv(out, index=False)
        time.sleep(max(0.0, interval - (time.time() - start)))
    return pd.DataFrame(rows)


def read_events() -> pd.DataFrame:
    """firewall_events.log lines: '<epoch> <EVENT> <details>'."""
    if not EVENTS.exists():
        return pd.DataFrame(columns=["epoch", "event", "detail"])
    rows = []
    for line in EVENTS.read_text().splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2:
            rows.append({"epoch": float(parts[0]), "event": parts[1], "detail": parts[2] if len(parts) > 2 else ""})
    return pd.DataFrame(rows)


def plot(run_id: str) -> None:
    df = pd.read_csv(EXPERIMENTS / "metrics" / f"{run_id}.resilience.csv")
    ev = read_events()
    t0 = df.start_epoch.min()
    ev = ev[(ev.epoch >= t0) & (ev.epoch <= df.end_epoch.max() + 70)]  # this run's block (+ its restore) only
    first_block = ev[ev.event == "BLOCK"].epoch.min() if (ev.event == "BLOCK").any() else None
    if first_block is not None and first_block > df.end_epoch.max():
        ev = ev.iloc[0:0]
    surface, grid, ink2, ok, bad, shade = "#fcfcfb", "#e4e3df", "#52514e", "#008300", "#e34948", "#f3d9d9"
    fig, ax = plt.subplots(figsize=(11, 2.8), facecolor=surface)
    ax.set_facecolor(surface)
    blocks = [b for b in ev[ev.event == "BLOCK"].epoch.tolist() if b <= df.end_epoch.max()]
    restores = ev[ev.event == "RESTORE"].epoch.tolist()
    for b in blocks:
        r = next((x for x in restores if x > b), df.end_epoch.max())
        ax.axvspan(b - t0, r - t0, color=shade, zorder=0, label="outbound 443 blocked")
        first_ok = df[(df.status == "ok") & (df.end_epoch > r)]  # same rule as resilience_summary
        if len(first_ok):
            rec = first_ok.end_epoch.iloc[0] - r
            ax.annotate(f"first success {rec:.1f}s after restore", (first_ok.end_epoch.iloc[0] - t0, 0.25),
                        fontsize=8, color=ink2, ha="left")
    for _, r in df.iterrows():
        ax.barh(0, r.end_epoch - r.start_epoch, left=r.start_epoch - t0, height=0.35,
                color=ok if r.status == "ok" else bad, edgecolor=surface, linewidth=2)
    ax.set_yticks([0], [df.method.iloc[0]])
    ax.set_ylim(-0.6, 0.6)
    ax.set_xlabel("seconds since test start (bar = one collection attempt incl. retries; green ok, red failed)")
    ax.set_title(f"Resilience: {run_id}", fontweight="bold")
    h, l = ax.get_legend_handles_labels()
    if h:
        ax.legend(h[:1], l[:1], frameon=False, fontsize=8, loc="upper right")
    ax.grid(axis="x", color=grid, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    path = ROOT / "reports" / "figures" / f"resilience_{run_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    print("->", path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--method", choices=["api", "bs4", "selenium"], default="api")
    ap.add_argument("--duration", type=float, default=240)
    ap.add_argument("--interval", type=float, default=20)
    ap.add_argument("--connect-timeout", type=float, default=5,
                    help="s to wait for the TCP connection (the firewall affects this)")
    ap.add_argument("--read-timeout", type=float, default=30,
                    help="s to wait for the answer (the API itself needs ~8-10 s for 500 edits); Selenium: page-load timeout")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--id", default=None)
    ap.add_argument("--plot", metavar="RUN_ID", default=None, help="only draw the figure for an existing run")
    a = ap.parse_args()
    if a.plot:
        plot(a.plot)
        return
    run_id = a.id or f"resil_{a.method}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    run(a.method, a.duration, a.interval, a.connect_timeout, a.read_timeout, a.retries, a.n, run_id)
    plot(run_id)


if __name__ == "__main__":
    main()
