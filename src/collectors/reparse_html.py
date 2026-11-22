"""Re-parse a saved raw page (data/raw/<id>.html.gz) without downloading it again."""
from __future__ import annotations

import gzip
import json
import sys

from src.collectors.rc_html_parser import parse_recentchanges
from src.config import DATA


def reparse(experiment_id: str) -> dict:
    raw = DATA / "raw"
    old = json.loads((raw / f"{experiment_id}.jsonl").read_text(encoding="utf-8").splitlines()[0])
    rows, stats = parse_recentchanges(gzip.decompress((raw / f"{experiment_id}.html.gz").read_bytes()))
    old.update(rows=rows, n=len(rows))
    (raw / f"{experiment_id}.jsonl").write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"experiment_id": experiment_id, **stats}


if __name__ == "__main__":
    for exp in sys.argv[1:]:
        print(reparse(exp))
