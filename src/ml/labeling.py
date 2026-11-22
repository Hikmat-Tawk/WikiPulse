"""Revert labels from MediaWiki's `mw-reverted` change tag."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import DATA, ROOT

REVERT_TAG = "mw-reverted"
AGE_BINS_H = [0, 1, 3, 6, 12, 24, 48, 72, 120, 168, 336, 504, 744]


def _tags(v) -> list[str] | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return json.loads(v) if isinstance(v, str) else list(v)


def observed_age_h(df: pd.DataFrame) -> pd.Series:
    """Hours between the edit and the moment we observed its tags."""
    edit = pd.to_datetime(df["timestamp_utc"], utc=True)
    seen = pd.to_datetime(df["collection_timestamp_utc"], utc=True)
    return (seen - edit).dt.total_seconds() / 3600


def label(df: pd.DataFrame, window_h: float) -> pd.DataFrame:
    """Return a copy of `df` with label_observed_at, label_status, reverted (nullable Int)."""
    out = df.copy()
    tags = out["tags"].map(_tags)
    age = observed_age_h(out)
    out["label_observed_at"] = out["collection_timestamp_utc"]
    status = np.where(tags.isna(), "unavailable", np.where(age >= window_h, "labeled", "pending"))
    out["label_status"] = status
    rev = tags.map(lambda t: int(REVERT_TAG in t) if t is not None else None)
    out["reverted"] = pd.array(np.where(status == "labeled", rev, None), dtype="Int64")
    out["label_window_h"] = window_h
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson confidence interval for a proportion k/n."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def revert_rate_by_age(df: pd.DataFrame, bins: list[float] = AGE_BINS_H) -> pd.DataFrame:
    """Revert-tag rate per observation-age bucket, overall and for logged-out editors."""
    d = df.copy()
    d["age_h"] = observed_age_h(d)
    d["is_rev"] = d["tags"].map(lambda v: REVERT_TAG in (_tags(v) or []))
    d["age_bin"] = pd.cut(d["age_h"], bins=bins, right=False)
    rows = []
    for b, g in d.groupby("age_bin", observed=True):
        for who, gg in (("all", g), ("logged_out", g[g["is_anonymous"]]), ("registered", g[~g["is_anonymous"]])):
            k, n = int(gg["is_rev"].sum()), len(gg)
            lo, hi = wilson(k, n)
            rows.append({"age_bin": str(b), "age_lo_h": b.left, "age_hi_h": b.right, "group": who,
                         "n": n, "reverted": k, "rate": k / n if n else float("nan"), "ci_lo": lo, "ci_hi": hi})
    return pd.DataFrame(rows)


def plot_curve(curve: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.5))
    for who, color in (("all", "#1f77b4"), ("logged_out", "#d62728"), ("registered", "#2ca02c")):
        c = curve[curve["group"] == who]
        x = (c["age_lo_h"].astype(float) * c["age_hi_h"].astype(float)).pow(0.5).clip(lower=0.5)
        ax.errorbar(x, c["rate"] * 100, yerr=[(c["rate"] - c["ci_lo"]) * 100, (c["ci_hi"] - c["rate"]) * 100],
                    marker="o", capsize=3, label=who, color=color)
    ax.set_xscale("log")
    ax.set_xlabel("edit age when observed (hours, log scale)")
    ax.set_ylabel("% of edits tagged mw-reverted")
    ax.set_title("Revert tag rate vs. edit age (non-bot ns0 edits, 95% Wilson CI)")
    for h, lab in ((24, "24h"), (48, "48h"), (72, "72h"), (168, "7d")):
        ax.axvline(h, color="grey", ls=":", lw=1)
        ax.text(h, ax.get_ylim()[1] * 0.97, lab, ha="center", va="top", fontsize=8, color="grey")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("normalized", type=Path)
    ap.add_argument("--window-h", type=float, default=72)
    args = ap.parse_args()
    df = pd.read_csv(args.normalized)
    curve = revert_rate_by_age(df)
    curve_path = ROOT / "experiments" / "metrics" / f"{args.normalized.stem}.revert_by_age.csv"
    curve_path.parent.mkdir(parents=True, exist_ok=True)
    curve.to_csv(curve_path, index=False)
    plot_curve(curve, ROOT / "reports" / "figures" / "revert_rate_vs_age.png")
    print(curve.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    lab = label(df, args.window_h)
    out = DATA / "labeled" / f"{args.normalized.stem}.w{int(args.window_h)}h.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    lab.to_csv(out, index=False)
    counts = lab["label_status"].value_counts().to_dict()
    labeled = lab[lab["label_status"] == "labeled"]
    print(f"\nwindow={args.window_h}h status counts={counts} "
          f"reverted rate among labeled={labeled['reverted'].mean():.4f} -> {out}")


if __name__ == "__main__":
    main()
