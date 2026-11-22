"""Summarise firewall resilience runs: what happened before, during and after each block."""
from __future__ import annotations

import sys

import pandas as pd

from src.benchmarking.resilience_runner import plot, read_events
from src.config import EXPERIMENTS


def summarize(run_id: str, events: pd.DataFrame) -> dict:
    df = pd.read_csv(EXPERIMENTS / "metrics" / f"{run_id}.resilience.csv")
    lo, hi = df.start_epoch.min(), df.end_epoch.max()
    blocks = events[(events.event == "BLOCK") & (events.epoch.between(lo, hi))]
    row = {"run_id": run_id, "method": df.method.iloc[0], "collections": len(df), "ok": int((df.status == "ok").sum()),
           "failed": int((df.status == "failed").sum())}
    if blocks.empty:
        return {**row, "note": "no block inside this run"}
    b = blocks.epoch.iloc[0]
    restores = events[(events.event == "RESTORE") & (events.epoch > b)]
    r = restores.epoch.iloc[0] if len(restores) else float("nan")
    mode = blocks.detail.iloc[0].split()[0]
    during = df[(df.end_epoch > b) & (df.start_epoch < r)]
    after = df[(df.status == "ok") & (df.end_epoch > r)]
    before = df[df.end_epoch <= b]
    kinds = sorted({k for v in during.error_kinds.dropna() for k in str(v).split(",")}) if "error_kinds" in df else []
    return {**row, "mode": mode, "block_s": round(r - b, 1),
            "ok_before_block": int((before.status == "ok").sum()),
            "collections_touching_block": len(during), "failed_touching_block": int((during.status == "failed").sum()),
            "error_kinds_during_block": ",".join(kinds) or "-",
            "mean_failed_duration_s": round(during[during.status == "failed"].duration_s.mean(), 1)
            if (during.status == "failed").any() else None,
            "s_per_failed_attempt": round(((f := during[during.status == "failed"]).duration_s - f.backoff_wait_s).sum()
                                          / f.failed_attempts.sum(), 2) if (during.status == "failed").any() else None,
            "recovery_s_after_restore": round(after.end_epoch.iloc[0] - r, 1) if len(after) else None,
            "read_timeouts_outside_block": int(df[(df.end_epoch <= b) | (df.start_epoch >= r)]
                                               .error_kinds.fillna("").str.contains("read_timeout").sum())
            if "error_kinds" in df else None}


def main() -> None:
    ids = sys.argv[1:]
    ev = read_events()
    res = pd.DataFrame([summarize(i, ev) for i in ids])
    out = EXPERIMENTS / "metrics" / "resilience_summary.csv"
    res.to_csv(out, index=False)
    pd.set_option("display.width", 220)
    print(res.T.to_string(header=False))
    for i in ids:
        plot(i)
    print("->", out)


if __name__ == "__main__":
    main()
