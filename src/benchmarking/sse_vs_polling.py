"""Push vs pull: EventStreams (SSE) against API polling, running at the same time."""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.collectors.api_collector import ApiCollector
from src.collectors.sse_collector import SSECollector
from src.config import DATA, EXPERIMENTS, ROOT
from src.http_client import PoliteClient

COLORS = {"sse": "#1baf7a", "poll30": "#2a78d6", "poll120": "#6250d6"}


class Observations:
    """Thread-safe: rev_id -> edit time and first-seen time per observer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.edit_ts: dict[int, float] = {}
        self.first_seen: dict[tuple[int, str], float] = {}

    def see(self, rev_id: int, edit_ts: float, observer: str, when: float) -> None:
        with self.lock:
            self.edit_ts.setdefault(rev_id, edit_ts)
            self.first_seen.setdefault((rev_id, observer), when)

    def frame(self) -> pd.DataFrame:
        rows = [{"rev_id": r, "observer": o, "first_seen": t, "edit_ts": self.edit_ts[r]}
                for (r, o), t in self.first_seen.items()]
        return pd.DataFrame(rows)


def poller(name: str, interval: float, limit: int, obs: Observations, stop: threading.Event,
           t_end: float, log: list) -> PoliteClient:
    client = PoliteClient(name, min_interval_s=1.0)
    col = ApiCollector(client)
    while time.time() < t_end and not stop.is_set():
        start = time.time()
        try:
            rows = col.query(rclimit=limit)["query"]["recentchanges"]
            got = time.time()
            for rc in rows:
                ts = datetime.fromisoformat(rc["timestamp"].replace("Z", "+00:00")).timestamp()
                obs.see(rc["revid"], ts, name, got)
            log.append({"observer": name, "start": start, "end": got, "rows": len(rows),
                        "oldest_returned": min(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
                                               for r in rows) if rows else None})
        except Exception as exc:  # keep polling; record the failure
            log.append({"observer": name, "start": start, "end": time.time(), "rows": 0, "error": str(exc)[:200]})
        stop.wait(max(0.0, interval - (time.time() - start)))
    return client


def analyse(df: pd.DataFrame, t0: float, t_end: float, margin_start: float, margin_end: float,
            bytes_by: dict, requests_by: dict, outage: tuple | None) -> tuple[pd.DataFrame, dict]:
    """Coverage and freshness on edits made inside [t0 + margin_start, t_end - margin_end]."""
    lo, hi = t0 + margin_start, t_end - margin_end
    win = df[(df.edit_ts >= lo) & (df.edit_ts <= hi)].copy()
    win["lag_s"] = win.first_seen - win.edit_ts
    universe = win.rev_id.nunique()
    rows = []
    for o in ["sse", "poll30", "poll120"]:
        d = win[win.observer == o]
        all_o = df[df.observer == o]
        rows.append({
            "observer": o, "edits_in_window": universe, "seen": len(d),
            "coverage_pct": round(100 * len(d) / universe, 2) if universe else None,
            "lag_median_s": round(d.lag_s.median(), 2), "lag_p90_s": round(d.lag_s.quantile(0.9), 2),
            "lag_max_s": round(d.lag_s.max(), 2),
            "requests_or_connections": requests_by[o], "kb_received": round(bytes_by[o] / 1024, 1),
            "kb_per_useful_edit": round(bytes_by[o] / 1024 / max(all_o.rev_id.nunique(), 1), 3),
        })
    res = pd.DataFrame(rows)
    resume = {}
    if outage:
        closed, reopened = outage
        gap = win[(win.edit_ts >= closed) & (win.edit_ts < reopened)]
        gap_edits = set(gap.rev_id)
        sse_gap = set(gap[gap.observer == "sse"].rev_id)
        resume = {"outage_s": round(reopened - closed, 1), "edits_made_during_outage": len(gap_edits),
                  "of_which_sse_received": len(sse_gap),
                  "missed_by_sse": sorted(gap_edits - sse_gap)[:20]}
    return res, resume


def plot(df: pd.DataFrame, res: pd.DataFrame, t0: float, t_end: float, ms: float, me: float, outage, path) -> None:
    lo, hi = t0 + ms, t_end - me
    win = df[(df.edit_ts >= lo) & (df.edit_ts <= hi)].copy()
    win["lag_s"] = (win.first_seen - win.edit_ts).clip(lower=0.05)
    fig, (a, b, c) = plt.subplots(1, 3, figsize=(15, 4), gridspec_kw={"width_ratios": [1.3, 1, 1]})
    for o in ["sse", "poll30", "poll120"]:
        lags = np.sort(win[win.observer == o].lag_s.to_numpy())
        if len(lags):
            a.plot(lags, np.arange(1, len(lags) + 1) / len(lags), color=COLORS[o], lw=2,
                   label=f"{o} (median {np.median(lags):.1f} s)")
    a.set_xscale("log")
    a.set(xlabel="freshness lag: first seen − edit time (s, log)", ylabel="share of edits seen by then",
          title="How late does each method learn about an edit?")
    a.legend(frameon=False, fontsize=8)
    a.grid(alpha=0.3)
    r = res.set_index("observer")
    b.bar(r.index, r.kb_per_useful_edit, color=[COLORS[o] for o in r.index])
    for i, v in enumerate(r.kb_per_useful_edit):
        b.text(i, v, f"{v:.2f} KB", ha="center", va="bottom", fontsize=9)
    b.set(ylabel="KB received per useful edit", title="Bandwidth per useful edit")
    b.grid(axis="y", alpha=0.3)
    c.bar(r.index, r.coverage_pct, color=[COLORS[o] for o in r.index])
    for i, v in enumerate(r.coverage_pct):
        c.text(i, v, f"{v:.1f} %", ha="center", va="bottom", fontsize=9)
    c.set(ylabel="% of edits seen", ylim=(0, 110), title="Coverage")
    c.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=600)
    ap.add_argument("--outage", type=float, default=20)
    ap.add_argument("--id", default=None)
    a = ap.parse_args()
    exp = a.id or f"sse_vs_poll_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    obs, stop, plog = Observations(), threading.Event(), []
    raw_events: list[dict] = []

    def on_sse(ev: dict, received: float) -> None:
        obs.see(ev["revision"]["new"], ev["timestamp"], "sse", received)
        raw_events.append({"_received_epoch": received, "event": ev})

    sse = SSECollector(on_event=on_sse)
    t0 = time.time()
    t_end = t0 + a.duration
    clients = {}

    def run_poller(name, interval, limit):
        clients[name] = poller(name, interval, limit, obs, stop, t_end, plog)

    threads = [threading.Thread(target=run_poller, args=args, daemon=True)
               for args in (("poll30", 30, 100), ("poll120", 120, 300))]
    for t in threads:
        t.start()
    print(f"{exp}: running {a.duration:.0f}s; SSE forced outage at {a.duration / 2:.0f}s for {a.outage:.0f}s", flush=True)
    stats = sse.run(a.duration, stop=stop, disconnect_at_s=a.duration / 2, outage_s=a.outage)
    stop.set()
    for t in threads:
        t.join(timeout=60)
    t_end = time.time()

    df = obs.frame()
    bytes_by = {"sse": stats.bytes_total,
                **{n: sum(r.body_bytes_wire + r.header_bytes_approx for r in c.records) for n, c in clients.items()}}
    requests_by = {"sse": stats.connections, **{n: len(c.records) for n, c in clients.items()}}
    ms, me = min(60.0, a.duration * 0.1), min(150.0, a.duration * 0.25)
    res, resume = analyse(df, t0, t_end, ms, me, bytes_by, requests_by, stats.disconnect_window)

    out = EXPERIMENTS / "metrics"
    res.to_csv(out / f"{exp}.csv", index=False)
    df.to_csv(out / f"{exp}.first_seen.csv", index=False)
    pd.DataFrame(plog).to_csv(out / f"{exp}.polls.csv", index=False)
    summary = {"experiment_id": exp, "duration_s": round(t_end - t0, 1), "window_margins_s": [ms, me],
               "sse": {"connections": stats.connections, "reconnects": stats.reconnects,
                       "events_total": stats.events_total, "events_useful": stats.events_useful,
                       "bytes_total": stats.bytes_total, "bytes_useful_decoded": stats.bytes_useful,
                       "useful_share_pct": round(100 * stats.bytes_useful / max(stats.bytes_total, 1), 2),
                       "content_encoding": stats.content_encoding,
                       "replayed_after_reconnect": stats.replayed_after_reconnect,
                       "events_per_s": round(stats.events_total / (t_end - t0), 1),
                       "distinct_wikis": len(stats.wikis), "top_wikis": stats.wikis.most_common(6),
                       "errors": stats.errors},
               "resume_test": resume}
    (out / f"{exp}.summary.json").write_text(json.dumps(summary, indent=1, default=str))
    raw = DATA / "raw" / f"{exp}_sse.jsonl"
    raw.write_text(json.dumps({"experiment_id": exp, "source_method": "sse", "window_start": None,
                               "collection_timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "n": len(raw_events), "rows": raw_events}, ensure_ascii=False) + "\n", encoding="utf-8")
    fig = ROOT / "reports" / "figures" / f"{exp}.png"
    fig.parent.mkdir(parents=True, exist_ok=True)
    plot(df, res, t0, t_end, ms, me, stats.disconnect_window, fig)
    print(res.to_string(index=False))
    print(json.dumps(summary, indent=1, default=str))
    print("->", fig)


if __name__ == "__main__":
    main()
