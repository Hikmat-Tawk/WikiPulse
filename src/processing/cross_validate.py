"""Cross-method validation: do the methods agree on the edits they all saw?"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from src.config import EXPERIMENTS

FIELDS = ["page_id", "parent_rev_id", "page_title", "namespace", "editor", "is_anonymous", "timestamp_utc",
          "byte_change", "old_size", "new_size", "is_bot", "is_minor", "is_new_page", "tags", "edit_summary"]


def _loose(s) -> str:
    """Summary comparison ignoring formatting: '/* History */ copyedit' ~ '→History: copyedit'."""
    return re.sub(r"[^0-9a-z]", "", str(s).lower())


def _norm(field: str, v):
    if pd.isna(v):
        return None
    if field == "tags":
        return tuple(sorted(json.loads(v)))
    if field in ("page_id", "parent_rev_id", "byte_change", "old_size", "new_size", "namespace"):
        return int(v)
    return v


def compare(ref: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    """Per-field agreement between two methods on shared rev_ids."""
    m = ref.merge(other, on="rev_id", suffixes=("_ref", "_oth"))
    rows = []
    for f in FIELDS:
        a = m[f"{f}_ref"].map(lambda v: _norm(f, v))
        b = m[f"{f}_oth"].map(lambda v: _norm(f, v))
        both = a.notna() & b.notna()
        eq = (a[both] == b[both])
        row = {"field": f, "shared": len(m), "compared": int(both.sum()),
               "null_in_ref": int(a.isna().sum()), "null_in_other": int(b.isna().sum()),
               "agree": int(eq.sum()), "agree_pct": round(100 * eq.mean(), 2) if both.any() else None}
        if f == "edit_summary":
            row["agree_loose_pct"] = round(100 * (a[both].map(_loose) == b[both].map(_loose)).mean(), 2) if both.any() else None
        rows.append(row)
    return pd.DataFrame(rows)


def mismatches(ref: pd.DataFrame, other: pd.DataFrame, field: str, k: int = 5) -> pd.DataFrame:
    """A few example disagreements for one field (for debugging / the report)."""
    m = ref.merge(other, on="rev_id", suffixes=("_ref", "_oth"))
    a, b = m[f"{field}_ref"].map(lambda v: _norm(field, v)), m[f"{field}_oth"].map(lambda v: _norm(field, v))
    bad = m[a.notna() & b.notna() & (a != b)]
    return bad[["rev_id", f"{field}_ref", f"{field}_oth"]].head(k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference", type=Path)
    ap.add_argument("others", type=Path, nargs="+")
    args = ap.parse_args()
    ref = pd.read_csv(args.reference)
    out = []
    for p in args.others:
        oth = pd.read_csv(p)
        shared = set(ref.rev_id) & set(oth.rev_id)
        method = oth["source_method"].iloc[0]
        print(f"\n=== {method} ({len(oth)} rows) vs {ref['source_method'].iloc[0]} ({len(ref)} rows): "
              f"{len(shared)} shared rev_ids ===")
        c = compare(ref, oth)
        print(c.to_string(index=False))
        for f in c.loc[(c.agree_pct.notna()) & (c.agree_pct < 100), "field"]:
            print(f"\n  examples where {f} differs:\n{mismatches(ref, oth, f).to_string(index=False)}")
        c.insert(0, "method", method)
        out.append(c)
    path = EXPERIMENTS / "metrics" / f"cross_validation_{args.reference.stem}.csv"
    pd.concat(out).to_csv(path, index=False)
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
