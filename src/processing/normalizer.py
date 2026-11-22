"""Turn raw collector output into the common WikiPulse schema."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.config import DATA
from src.processing.deduplicator import deduplicate

COLUMNS = [
    "rev_id", "rc_id", "page_id", "parent_rev_id", "page_title", "namespace", "editor", "user_id",
    "is_anonymous", "timestamp_utc", "byte_change", "old_size", "new_size", "edit_summary",
    "is_bot", "is_minor", "is_new_page", "tags",
    "source_method", "collection_timestamp_utc", "freshness_lag_ms", "experiment_id", "collection_status",
]


def parse_utc(ts: str) -> datetime:
    """'2026-11-14T11:51:48Z' or '20261114115148' -> aware UTC datetime."""
    if "T" in ts:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freshness_lag_ms(edit_ts: str, collected_ts: str) -> int:
    """How long after the edit we received it, in ms (>= 0 unless clocks disagree)."""
    return int((parse_utc(collected_ts) - parse_utc(edit_ts)).total_seconds() * 1000)


def normalize_api_row(rc: dict, meta: dict) -> dict | None:
    """One raw API recentchanges row -> common schema dict, or None if it is not a ns0 edit."""
    if rc.get("type") not in ("edit", "new") or rc.get("ns") != 0:
        return None
    old, new = rc.get("oldlen"), rc.get("newlen")
    user = None if rc.get("userhidden") else rc.get("user")
    return {
        "rev_id": rc.get("revid"),
        "rc_id": rc.get("rcid"),
        "page_id": rc.get("pageid"),
        "parent_rev_id": rc.get("old_revid") or None,  # 0 for page creations -> None
        "page_title": rc.get("title"),
        "namespace": rc.get("ns"),
        "editor": user,
        "user_id": rc.get("userid") if user is not None else None,
        "is_anonymous": bool(rc.get("anon") or rc.get("temp")),
        "timestamp_utc": rc["timestamp"],
        "byte_change": new - old if old is not None and new is not None else None,
        "old_size": old,
        "new_size": new,
        "edit_summary": None if rc.get("commenthidden") else rc.get("comment"),
        "is_bot": bool(rc.get("bot")),
        "is_minor": bool(rc.get("minor")),
        "is_new_page": rc.get("type") == "new" or bool(rc.get("new")),
        "tags": json.dumps(rc.get("tags", [])),
        "source_method": "api",
        "collection_timestamp_utc": meta["collection_timestamp_utc"],
        "freshness_lag_ms": freshness_lag_ms(rc["timestamp"], meta["collection_timestamp_utc"]),
        "experiment_id": meta["experiment_id"],
        "collection_status": "ok",
    }


def normalize_html_row(row: dict, meta: dict) -> dict | None:
    """One parsed Recent Changes HTML row (see rc_html_parser) -> common schema, or None if not ns0."""
    if row.get("namespace") != 0 or not row.get("rev_id"):
        return None
    ts = parse_utc(row["timestamp"])
    new, delta = row.get("new_size"), row.get("byte_change")
    old = new - delta if new is not None and delta is not None else None  # 0 for page creations
    tags = [t.replace("_", " ") for t in row.get("tag_classes", [])]
    return {
        "rev_id": int(row["rev_id"]),
        "rc_id": None,
        "page_id": int(row["page_id"]) if row.get("page_id") else None,
        "parent_rev_id": int(row["parent_rev_id"]) if row.get("parent_rev_id") else None,
        "page_title": row.get("page_title"),
        "namespace": row["namespace"],
        "editor": row.get("editor"),
        "user_id": None,
        "is_anonymous": bool(row.get("is_logged_out")),
        "timestamp_utc": iso_utc(ts),
        "byte_change": delta,
        "old_size": old,
        "new_size": new,
        "edit_summary": row.get("edit_summary") or None,
        "is_bot": bool(row.get("is_bot")),
        "is_minor": bool(row.get("is_minor")),
        "is_new_page": bool(row.get("is_new_page")),
        "tags": json.dumps(tags),
        "source_method": meta["source_method"],
        "collection_timestamp_utc": meta["collection_timestamp_utc"],
        "freshness_lag_ms": freshness_lag_ms(iso_utc(ts), meta["collection_timestamp_utc"]),
        "experiment_id": meta["experiment_id"],
        "collection_status": "ok",
    }


_IP = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:]+:[0-9a-fA-F:]+)$")


def normalize_sse_row(row: dict, meta: dict) -> dict | None:
    """One EventStreams `recentchange` event -> common schema, or None if not an enwiki ns0 edit."""
    ev = row["event"]
    if ev.get("wiki") != "enwiki" or ev.get("namespace") != 0 or ev.get("type") not in ("edit", "new"):
        return None
    received = datetime.fromtimestamp(row["_received_epoch"], timezone.utc)
    edit_ts = datetime.fromtimestamp(ev["timestamp"], timezone.utc)
    length, rev = ev.get("length") or {}, ev.get("revision") or {}
    old, new = length.get("old"), length.get("new")
    if old is None and ev.get("type") == "new":
        old = 0
    user = ev.get("user")
    return {
        "rev_id": rev.get("new"),
        "rc_id": ev.get("id"),
        "page_id": None,
        "parent_rev_id": rev.get("old") or None,
        "page_title": ev.get("title"),
        "namespace": ev.get("namespace"),
        "editor": user,
        "user_id": None,
        "is_anonymous": bool(user) and (user.startswith("~") or bool(_IP.match(user))),
        "timestamp_utc": iso_utc(edit_ts),
        "byte_change": new - old if old is not None and new is not None else None,
        "old_size": old,
        "new_size": new,
        "edit_summary": ev.get("comment") or None,
        "is_bot": bool(ev.get("bot")),
        "is_minor": bool(ev.get("minor")),
        "is_new_page": ev.get("type") == "new",
        "tags": None,  # the stream does not carry change tags
        "source_method": "sse",
        "collection_timestamp_utc": received.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "freshness_lag_ms": int((row["_received_epoch"] - ev["timestamp"]) * 1000),
        "experiment_id": meta["experiment_id"],
        "collection_status": "ok",
    }


NORMALIZERS = {"api": normalize_api_row, "bs4": normalize_html_row, "selenium": normalize_html_row,
               "sse": normalize_sse_row}


def normalize_jsonl(path: Path) -> tuple[pd.DataFrame, dict]:
    """Read a raw JSONL file (one collector line per row-batch) -> DataFrame in COLUMNS order + stats."""
    out, stats = [], {"raw_rows": 0, "filtered_non_ns0_edit": 0}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            batch = json.loads(line)
            fn = NORMALIZERS[batch["source_method"]]
            for raw in batch["rows"]:
                stats["raw_rows"] += 1
                rec = fn(raw, batch)
                if rec is None:
                    stats["filtered_non_ns0_edit"] += 1
                else:
                    out.append(rec)
    df = pd.DataFrame(out, columns=COLUMNS)
    return df, stats


def export_csv(raw: Path) -> tuple[Path, dict]:
    """Normalise + deduplicate one raw JSONL file and save it as data/normalized/<name>.csv (common schema)."""
    df, stats = normalize_jsonl(raw)
    df, n_dupes = deduplicate(df)
    stats.update(normalized_rows=len(df) + n_dupes, duplicates_removed=n_dupes, final_rows=len(df))
    out = DATA / "normalized" / (raw.stem + ".csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", type=Path)
    args = ap.parse_args()
    out, stats = export_csv(args.raw)
    print(f"{stats}\n-> {out}")


if __name__ == "__main__":
    main()
