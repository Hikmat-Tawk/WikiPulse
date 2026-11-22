"""Method B - requests + BeautifulSoup on Special:RecentChanges."""
from __future__ import annotations

import argparse
import gzip
import json
import time
from datetime import datetime, timezone

from src.collectors.rc_html_parser import parse_recentchanges
from src.config import DATA, EXPERIMENTS, load_settings
from src.http_client import PoliteClient

HTML_PARAMS = {"namespace": 0, "days": 30, "enhanced": 0, "hidebots": 0, "hidelog": 1, "hidecategorization": 1}


def page_params(n: int) -> dict:
    """Query string for the newest `n` rows."""
    return {**HTML_PARAMS, "limit": n}


def collect(n: int, experiment_id: str, client: PoliteClient | None = None) -> dict:
    """Fetch + parse once; write raw files; return a small result dict (for the benchmark)."""
    client = client if client is not None else PoliteClient("bs4")
    url = load_settings()["endpoints"]["recentchanges_html"]
    t0 = time.perf_counter()
    resp = client.get(url, params=page_params(n))
    t_fetch = time.perf_counter() - t0
    rows, stats = parse_recentchanges(resp.content)
    t_total = time.perf_counter() - t0
    collected = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_dir = DATA / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{experiment_id}.html.gz").write_bytes(gzip.compress(resp.content))
    line = {"experiment_id": experiment_id, "source_method": "bs4", "window_start": None,
            "collection_timestamp_utc": collected, "n": len(rows), "rows": rows}
    (raw_dir / f"{experiment_id}.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    _save_request_log(client, experiment_id)
    return {"experiment_id": experiment_id, "rows": len(rows), **stats,
            "fetch_s": round(t_fetch, 3), "parse_s": round(t_total - t_fetch, 3), "total_s": round(t_total, 3),
            **client.summary()}


def _save_request_log(client: PoliteClient, experiment_id: str) -> None:
    path = EXPERIMENTS / "metrics" / f"{experiment_id}.requests.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in client.records_as_dicts():
            f.write(json.dumps(rec) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--experiment-id", default=None)
    args = ap.parse_args()
    exp = args.experiment_id or f"bs4_latest_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    print(collect(args.n, exp))
    from src.processing.normalizer import export_csv
    csv_path, stats = export_csv(DATA / "raw" / f"{exp}.jsonl")
    print(f"CSV ({stats['final_rows']} edits, common schema) -> {csv_path}")


if __name__ == "__main__":
    main()
