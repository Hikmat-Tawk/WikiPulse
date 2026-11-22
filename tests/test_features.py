"""Leakage tests: the revert label must never reach the features."""
import json

import pandas as pd
import pytest

from src.ml.features import FEATURES, assert_no_leakage, build_features


def edits(tags_a, tags_b):
    return pd.DataFrame({
        "rev_id": [1, 2], "byte_change": [-500, 20], "old_size": [1000, 0],
        "edit_summary": ["/* History */", None], "is_anonymous": [True, False], "is_minor": [False, True],
        "is_new_page": [False, True], "tags": [json.dumps(tags_a), json.dumps(tags_b)],
        "timestamp_utc": ["2026-11-02T23:10:00Z", "2026-11-03T08:00:00Z"],
    })


def test_mw_reverted_tag_does_not_change_any_feature():
    a = build_features(edits(["mobile edit", "mw-reverted"], []))[FEATURES]
    b = build_features(edits(["mobile edit"], []))[FEATURES]
    pd.testing.assert_frame_equal(a, b)


def test_feature_values():
    f = build_features(edits(["mobile web edit", "newcomer task add link", "mw-undo"], ["visualeditor"]))
    r0, r1 = f.iloc[0], f.iloc[1]
    assert r0.byte_sign == -1 and r0.rel_change == -0.5 and r0.summary_section_only == 1
    assert r0.tag_mobile == 1 and r0.tag_newcomer_task == 1 and r0.tag_revert_action == 1
    assert r1.summary_empty == 1 and r1.tag_visualeditor == 1 and r1.is_new_page == 1
    assert (r0.hour, r0.weekday) == (23, 0)  # Monday 23:00 UTC


def test_unknown_and_abusefilter_tags_are_ignored():
    f = build_features(edits(["possible vandalism", "references removed"], []))
    assert f.filter(like="tag_").iloc[0].sum() == 0


def test_leakage_guard():
    assert_no_leakage(FEATURES)
    with pytest.raises(ValueError):
        assert_no_leakage(FEATURES + ["tags"])
