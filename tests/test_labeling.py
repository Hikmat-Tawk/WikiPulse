"""Tests for revert labeling (observation window, pending, unavailable)."""
import json

import pandas as pd

from src.ml.labeling import label, revert_rate_by_age, wilson


def frame():
    return pd.DataFrame({
        "timestamp_utc": ["2026-11-10T00:00:00Z", "2026-11-10T00:00:00Z", "2026-11-14T11:00:00Z", "2026-11-10T00:00:00Z"],
        "collection_timestamp_utc": ["2026-11-14T12:00:00Z"] * 4,
        "tags": [json.dumps(["mw-reverted", "mobile edit"]), json.dumps([]), json.dumps(["mw-reverted"]), None],
        "is_anonymous": [True, False, True, False],
    })


def test_old_edits_are_labeled_from_revert_tag():
    out = label(frame(), window_h=72)
    assert list(out["label_status"]) == ["labeled", "labeled", "pending", "unavailable"]
    assert out["reverted"].iloc[0] == 1 and out["reverted"].iloc[1] == 0


def test_pending_edits_have_no_label_even_if_already_tagged():
    out = label(frame(), window_h=72)
    assert pd.isna(out["reverted"].iloc[2])  # 1 h old: too young even though tag is present


def test_window_changes_status():
    assert label(frame(), window_h=0.5)["label_status"].iloc[2] == "labeled"


def test_curve_counts_and_wilson():
    curve = revert_rate_by_age(frame().iloc[:3])
    allrows = curve[curve["group"] == "all"]
    assert allrows["n"].sum() == 3 and allrows["reverted"].sum() == 2
    lo, hi = wilson(5, 100)
    assert 0.02 < lo < 0.05 < hi < 0.12
