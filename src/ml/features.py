"""Edit-time features for revert prediction — with explicit leakage protection."""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

LABEL = "reverted"
FORBIDDEN_TAGS = {"mw-reverted"}  # must never influence a feature

TAG_GROUPS: dict[str, set[str]] = {
    "tag_mobile": {"mobile edit", "mobile web edit", "advanced mobile edit", "mobile app edit",
                   "android app edit", "ios app edit", "app-section-source", "app-full-source"},
    "tag_visualeditor": {"visualeditor", "visualeditor-wikitext", "visualeditor-switched"},
    "tag_wikieditor": {"wikieditor"},
    "tag_tool": {"AWB", "JWB", "twinkle", "WPCleaner", "ProveIt edit", "shortdesc helper", "RedWarn",
                 "WikiShield script"},
    "tag_newcomer_task": None,   # any tag starting with "newcomer task"
    "tag_editcheck": None,       # any tag starting with "editcheck-"
    "tag_revert_action": {"mw-undo", "mw-rollback", "mw-manual-revert"},  # THIS edit undoes another (known at save)
    "tag_redirect": {"mw-new-redirect", "mw-changed-redirect-target", "mw-removed-redirect"},
}
_PREFIX_GROUPS = {"tag_newcomer_task": "newcomer task", "tag_editcheck": "editcheck-"}

NUMERIC = ["log_abs_byte_change", "byte_sign", "log_old_size", "rel_change", "summary_len"]
BINARY = ["is_anonymous", "is_minor", "summary_empty", "summary_section_only", *TAG_GROUPS.keys()]
TIME = ["hour", "weekday"]
FEATURES = NUMERIC + BINARY + TIME

_SECTION_ONLY = re.compile(r"^\s*/\*.*\*/\s*$")


def _tag_flags(tags: list[str]) -> dict[str, int]:
    tags = [t for t in tags if t not in FORBIDDEN_TAGS]
    out = {}
    for group, members in TAG_GROUPS.items():
        if members is None:
            prefix = _PREFIX_GROUPS[group]
            out[group] = int(any(t.startswith(prefix) for t in tags))
        else:
            out[group] = int(any(t in members for t in tags))
    return out


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame with FEATURES (+ timestamp_utc, rev_id and the label if present)."""
    d = pd.DataFrame(index=df.index)
    bc = df["byte_change"].astype(float)
    old = df["old_size"].astype(float).clip(lower=0)
    d["log_abs_byte_change"] = np.log1p(bc.abs())
    d["byte_sign"] = np.sign(bc).astype(int)
    d["log_old_size"] = np.log1p(old)
    d["rel_change"] = (bc / old.clip(lower=1)).clip(-1, 5)  # removing everything = -1; cap big growth
    summary = df["edit_summary"].fillna("").astype(str)
    d["summary_len"] = summary.str.len()
    d["summary_empty"] = (summary.str.strip() == "").astype(int)
    d["summary_section_only"] = summary.str.match(_SECTION_ONLY).astype(int)  # auto "/* Section */" only
    for c in ("is_anonymous", "is_minor", "is_new_page"):
        d[c] = df[c].astype(bool).astype(int)
    flags = df["tags"].map(lambda v: _tag_flags(json.loads(v) if isinstance(v, str) else []))
    d = d.join(pd.DataFrame(list(flags), index=df.index))
    ts = pd.to_datetime(df["timestamp_utc"], utc=True)
    d["hour"] = ts.dt.hour
    d["weekday"] = ts.dt.day_of_week  # 0 = Monday
    d["timestamp_utc"] = ts
    d["rev_id"] = df["rev_id"]
    if LABEL in df:
        d[LABEL] = df[LABEL]
    return d


def assert_no_leakage(features: list[str]) -> None:
    """Fail loudly if a feature name hints at label information."""
    bad = [f for f in features if "revert" in f and f != "tag_revert_action" or f in (LABEL, "tags", "label_status")]
    if bad:
        raise ValueError(f"leaky features: {bad}")


def load_labeled(path, exclude_new_pages: bool = True) -> pd.DataFrame:
    """Labeled CSV -> only rows with label_status == 'labeled', with features, sorted by time."""
    df = pd.read_csv(path)
    df = df[df["label_status"] == "labeled"].copy()
    if exclude_new_pages:
        df = df[~df["is_new_page"].astype(bool)]
    df[LABEL] = df[LABEL].astype(int)
    feats = build_features(df)
    return feats.sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
