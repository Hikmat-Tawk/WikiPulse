"""Turn per-request records into the assignment's metrics."""
from __future__ import annotations

import json
import statistics
from pathlib import Path


def load_records(path: Path) -> list[dict]:
    """Read a <experiment>.requests.jsonl file."""
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def summarize_requests(records: list[dict]) -> dict:
    """Required metrics + split between the main data request(s) and supplementary (browser asset) requests."""
    def wire(r: dict) -> int:
        return (r.get("body_bytes_wire") or 0) + (r.get("header_bytes_approx") or 0)

    main = [r for r in records if r.get("is_main_document", True)]
    supp = [r for r in records if not r.get("is_main_document", True)]
    lat = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    return {
        "n_requests": len(records),
        "n_requests_main": len(main),
        "n_requests_supplementary": len(supp),
        "bandwidth_kb": round(sum(map(wire, records)) / 1024, 1),
        "bandwidth_kb_main": round(sum(map(wire, main)) / 1024, 1),
        "bandwidth_kb_supplementary": round(sum(map(wire, supp)) / 1024, 1),
        "decoded_kb": round(sum((r.get("body_bytes_decoded") or 0) for r in records) / 1024, 1),
        "avg_latency_ms": round(statistics.fmean(lat), 1) if lat else None,
        "median_latency_ms": round(statistics.median(lat), 1) if lat else None,
        "main_latency_ms": round(statistics.fmean([r["latency_ms"] for r in main if r.get("latency_ms") is not None]), 1)
        if any(r.get("latency_ms") is not None for r in main) else None,
        "http_errors": sum(1 for r in records if r.get("status") and r["status"] >= 400),
        "failed_requests": sum(1 for r in records if r.get("error")),
        "retries": sum(1 for r in records if (r.get("attempt") or 0) > 0),
    }


def mean_std(values: list[float]) -> tuple[float | None, float | None]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return statistics.fmean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)
