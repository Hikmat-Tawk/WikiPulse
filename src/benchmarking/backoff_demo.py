"""Demonstrate our client's rate-limit handling against the local mock server (no Wikipedia traffic)."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src.config import EXPERIMENTS, ROOT
from src.http_client import PoliteClient
from src.networking.mock_server import MockState, start

PORT = 8429
SCENARIOS = [
    ("A: 429 + Retry-After 3 s (3×)", dict(fail_first=3, fail_status=429, retry_after=3)),
    ("B: 503, no Retry-After (4×) → exponential", dict(fail_first=4, fail_status=503, retry_after=None)),
    ("C: 429 forever → give up", dict(fail_first=10**6, fail_status=429, retry_after=1)),
]


def run() -> pd.DataFrame:
    server, state = start(PORT, MockState())
    rows = []
    try:
        for name, cfg in SCENARIOS:
            state.reset(**cfg)
            client = PoliteClient("mock", min_interval_s=0, max_retries=5, timeout_s=5)
            outcome = "success"
            try:
                client.get(f"http://127.0.0.1:{PORT}/w/api.php")
            except RuntimeError as exc:
                outcome = f"gave up: {exc}"[:80]
            t0 = state.log[0]["arrived"]
            waits = {r.attempt: r.retry_wait_s for r in client.records}
            for e in state.log:
                attempt = e["request_no"] - 1
                rows.append({"scenario": name, "attempt": attempt, "t_s": round(e["arrived"] - t0, 3),
                             "status": e["status"], "client_wait_after_s": round(waits.get(attempt, 0.0), 3),
                             "outcome": outcome})
            print(f"{name}: {outcome}; arrivals at "
                  + ", ".join(f"{e['arrived'] - t0:.2f}s[{e['status']}]" for e in state.log))
    finally:
        server.shutdown()
    return pd.DataFrame(rows)


def plot(df: pd.DataFrame) -> None:
    ok, bad, ink, grid, surface = "#008300", "#e34948", "#0b0b0b", "#e4e3df", "#fcfcfb"
    fig, ax = plt.subplots(figsize=(11, 3.4), facecolor=surface)
    ax.set_facecolor(surface)
    names = list(dict.fromkeys(df.scenario))
    for i, name in enumerate(names):
        d = df[df.scenario == name]
        ax.plot(d.t_s, [i] * len(d), color=grid, lw=2, zorder=1)
        for _, r in d.iterrows():
            good = r.status == 200
            ax.scatter(r.t_s, i, s=90, color=ok if good else bad, marker="o" if good else "X", zorder=3,
                       edgecolor=surface, linewidth=1.5)
            ax.annotate(str(r.status), (r.t_s, i), textcoords="offset points", xytext=(0, 9), ha="center",
                        fontsize=8, color=ink)
        gaps = d.t_s.diff().dropna()
        for (t_prev, gap) in zip(d.t_s.iloc[:-1], gaps):
            ax.annotate(f"{gap:.1f}s", (t_prev + gap / 2, i), textcoords="offset points", xytext=(0, -14),
                        ha="center", fontsize=8, color="#52514e")
    ax.set_yticks(range(len(names)), names)
    ax.set_ylim(len(names) - 0.4, -0.6)  # top-down order with room for the labels
    ax.set_xlabel("seconds since first request (server-side arrival times)")
    ax.set_title("Client backoff against a local mock server (x = 429/503, dot = 200)", fontweight="bold", pad=14)
    ax.grid(axis="x", color=grid, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    path = ROOT / "reports" / "figures" / "backoff_demo.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    print("->", path)


def main() -> None:
    df = run()
    out = EXPERIMENTS / "metrics" / "backoff_demo.csv"
    df.to_csv(out, index=False)
    print("->", out)
    plot(df)


if __name__ == "__main__":
    main()
