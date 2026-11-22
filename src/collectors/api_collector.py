"""Method C - MediaWiki Action API collector (`action=query&list=recentchanges`)."""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import DATA, EXPERIMENTS, load_settings
from src.http_client import PoliteClient, TransientError

RCPROP = "title|ids|sizes|flags|user|userid|timestamp|comment|tags"
MAX_LIMIT = 500


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApiCollector:
    """Fetches recentchanges rows politely through PoliteClient."""

    def __init__(self, client: PoliteClient | None = None) -> None:
        self.settings = load_settings()
        self.api = self.settings["endpoints"]["api"]
        self.client = client or PoliteClient("api")

    def _check_maxlag(self, resp) -> None:
        """MediaWiki answers 'maxlag' as HTTP 200 + JSON error: treat it as 'slow down'."""
        if '"maxlag"' in resp.text[:300]:
            raise TransientError("maxlag")

    def query(self, **params) -> dict:
        """One API call; returns the parsed JSON (raises on API errors)."""
        base = {"action": "query", "list": "recentchanges", "format": "json", "formatversion": 2,
                "rcnamespace": 0, "rctype": "edit|new", "rcprop": RCPROP,
                "maxlag": self.settings["http"].get("maxlag", 5)}
        resp = self.client.get(self.api, params={**base, **params}, check=self._check_maxlag)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"API error: {data['error']}")
        return data

    def latest(self, n: int, show: str | None = None) -> list[dict]:
        """Newest `n` main-namespace edits (incl. bots unless `show='!bot'`), following rccontinue."""
        rows: list[dict] = []
        cont: dict = {}
        while len(rows) < n:
            params = {"rclimit": min(MAX_LIMIT, n - len(rows)), **cont}
            if show:
                params["rcshow"] = show
            data = self.query(**params)
            rows += data["query"]["recentchanges"]
            if "continue" not in data:
                break
            cont = data["continue"]
        return rows[:n]

    def window(self, start: datetime, limit: int = MAX_LIMIT, show: str = "!bot") -> list[dict]:
        """Up to `limit` edits going backwards in time from `start`."""
        data = self.query(rcstart=_iso(start), rcdir="older", rclimit=limit, rcshow=show)
        return data["query"]["recentchanges"]

    def oldest_timestamp(self) -> datetime:
        """Timestamp of the oldest row the API still keeps (~30 days)."""
        rc = self.query(rcdir="newer", rclimit=1)["query"]["recentchanges"][0]
        return datetime.fromisoformat(rc["timestamp"].replace("Z", "+00:00"))


def plan_windows(oldest: datetime, now: datetime, k: int, seed: int, margin_min: int = 30) -> list[datetime]:
    """K window start times, uniformly random (seeded) between oldest+margin and now-5min."""
    rng = random.Random(seed)
    lo = (oldest + timedelta(minutes=margin_min)).timestamp()
    hi = (now - timedelta(minutes=5)).timestamp()
    starts = sorted(datetime.fromtimestamp(rng.uniform(lo, hi), tz=timezone.utc).replace(microsecond=0)
                    for _ in range(k))
    return starts


def run_sample(windows: int, seed: int, experiment_id: str) -> Path:
    """Historical, resumable sample pull. Writes data/raw/<experiment_id>.jsonl (one line per window)."""
    out = DATA / "raw" / f"{experiment_id}.jsonl"
    plan_file = DATA / "raw" / f"{experiment_id}.plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    col = ApiCollector()

    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
        print(f"resuming plan {plan_file.name}: {len(plan['starts'])} windows")
    else:
        now = _now()
        oldest = col.oldest_timestamp()
        starts = plan_windows(oldest, now, windows, seed)
        plan = {"experiment_id": experiment_id, "seed": seed, "created_utc": _iso(now),
                "oldest_available_utc": _iso(oldest), "rcshow": "!bot", "limit": MAX_LIMIT,
                "starts": [_iso(s) for s in starts]}
        plan_file.write_text(json.dumps(plan, indent=1))
        print(f"new plan: {windows} windows between {_iso(oldest)} and {_iso(now)} (seed {seed})")

    done = set()
    if out.exists():
        with out.open(encoding="utf-8") as f:
            done = {json.loads(line)["window_start"] for line in f if line.strip()}
    todo = [s for s in plan["starts"] if s not in done]
    print(f"{len(done)} windows already done, {len(todo)} to fetch")

    t0 = time.perf_counter()
    with out.open("a", encoding="utf-8") as f:
        for i, start in enumerate(todo, 1):
            rows = col.window(datetime.fromisoformat(start.replace("Z", "+00:00")))
            line = {"experiment_id": experiment_id, "source_method": "api", "window_start": start,
                    "collection_timestamp_utc": _iso(_now()), "n": len(rows), "rows": rows}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0 or i == len(todo):
                s = col.client.summary()
                print(f"  {i}/{len(todo)} windows, {s['requests']} req, {s['kb_wire_approx']:,} KB wire, "
                      f"{time.perf_counter() - t0:.0f}s, retries={s['retries']} 429={s['http_429']}")
    _save_request_log(col.client, experiment_id)
    return out


def collect(n: int, experiment_id: str) -> dict:
    """Benchmark workload: newest n edits -> data/raw/<experiment_id>.jsonl. Returns timing + summary."""
    out = DATA / "raw" / f"{experiment_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    col = ApiCollector()
    t0 = time.perf_counter()
    rows = col.latest(n)
    elapsed = time.perf_counter() - t0
    line = {"experiment_id": experiment_id, "source_method": "api", "window_start": None,
            "collection_timestamp_utc": _iso(_now()), "n": len(rows), "rows": rows}
    out.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    _save_request_log(col.client, experiment_id, quiet=True)
    return {"experiment_id": experiment_id, "rows": len(rows), "fetch_s": round(elapsed, 3), "parse_s": 0.0,
            "startup_s": 0.0, **col.client.summary()}


def run_latest(n: int, experiment_id: str) -> Path:
    """CLI wrapper around collect()."""
    res = collect(n, experiment_id)
    print(res)
    from src.processing.normalizer import export_csv
    csv_path, stats = export_csv(DATA / "raw" / f"{experiment_id}.jsonl")
    print(f"CSV ({stats['final_rows']} edits, common schema) -> {csv_path}")
    return DATA / "raw" / f"{experiment_id}.jsonl"


def _save_request_log(client: PoliteClient, experiment_id: str, quiet: bool = False) -> None:
    path = EXPERIMENTS / "metrics" / f"{experiment_id}.requests.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for rec in client.records_as_dicts():
            f.write(json.dumps(rec) + "\n")
    if not quiet:
        print(f"request log -> {path}  summary: {client.summary()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("sample", help="historical windows for the ML dataset")
    s.add_argument("--windows", type=int, default=170)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--experiment-id", default=None)
    l = sub.add_parser("latest", help="newest N edits (benchmark workload)")
    l.add_argument("--n", type=int, default=500)
    l.add_argument("--experiment-id", default=None)
    args = ap.parse_args()
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    if args.mode == "sample":
        run_sample(args.windows, args.seed, args.experiment_id or f"api_sample_seed{args.seed}")
    else:
        run_latest(args.n, args.experiment_id or f"api_latest_{stamp}")


if __name__ == "__main__":
    main()
