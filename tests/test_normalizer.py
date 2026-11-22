"""Tests for API normalisation and de-duplication."""
import json

import pandas as pd

from src.processing.deduplicator import deduplicate
from src.processing.normalizer import COLUMNS, freshness_lag_ms, normalize_api_row

META = {"experiment_id": "t", "collection_timestamp_utc": "2026-11-14T12:00:00Z"}


def api_row(**kw):
    row = {"type": "edit", "ns": 0, "title": "Paris", "pageid": 1, "revid": 100, "old_revid": 99, "rcid": 7,
           "user": "Bob", "userid": 5, "bot": False, "new": False, "minor": True, "oldlen": 1000, "newlen": 1120,
           "timestamp": "2026-11-14T11:59:58Z", "comment": "fix", "tags": ["visualeditor"]}
    row.update(kw)
    return row


def test_api_row_maps_all_columns():
    rec = normalize_api_row(api_row(), META)
    assert set(rec) == set(COLUMNS)
    assert rec["rev_id"] == 100 and rec["rc_id"] == 7 and rec["parent_rev_id"] == 99
    assert rec["byte_change"] == 120  # new_size - old_size
    assert rec["is_minor"] is True and rec["is_bot"] is False and rec["is_anonymous"] is False
    assert json.loads(rec["tags"]) == ["visualeditor"]
    assert rec["freshness_lag_ms"] == 2000


def test_temp_account_and_ip_count_as_anonymous():
    assert normalize_api_row(api_row(temp=True, user="~2026-123"), META)["is_anonymous"] is True
    assert normalize_api_row(api_row(anon=True, user="1.2.3.4"), META)["is_anonymous"] is True


def test_non_edits_and_other_namespaces_are_filtered():
    assert normalize_api_row(api_row(type="log"), META) is None
    assert normalize_api_row(api_row(ns=1), META) is None


def test_hidden_fields_become_null_not_guessed():
    rec = normalize_api_row(api_row(userhidden=True, commenthidden=True), META)
    assert rec["editor"] is None and rec["user_id"] is None and rec["edit_summary"] is None


def test_new_page_has_no_parent():
    rec = normalize_api_row(api_row(type="new", old_revid=0, oldlen=0), META)
    assert rec["is_new_page"] is True and rec["parent_rev_id"] is None and rec["byte_change"] == 1120


def test_freshness_lag_accepts_both_timestamp_formats():
    assert freshness_lag_ms("20261114115958", "2026-11-14T12:00:00Z") == 2000


def test_sse_event_normalisation():
    from src.processing.normalizer import normalize_sse_row
    ev = {"id": 2090268774, "type": "edit", "namespace": 0, "title": "X", "user": "~2026-123", "bot": False,
          "minor": False, "length": {"old": 1796, "new": 1816}, "revision": {"old": 10, "new": 11},
          "timestamp": 1794657183, "comment": "", "wiki": "enwiki"}
    rec = normalize_sse_row({"_received_epoch": 1794657183.75, "event": ev}, META)
    assert set(rec) == set(COLUMNS)
    assert rec["rev_id"] == 11 and rec["rc_id"] == 2090268774 and rec["byte_change"] == 20
    assert rec["is_anonymous"] is True and rec["page_id"] is None and rec["tags"] is None
    assert rec["freshness_lag_ms"] == 750 and rec["edit_summary"] is None
    assert normalize_sse_row({"_received_epoch": 0, "event": {**ev, "wiki": "dewiki"}}, META) is None
    ip = normalize_sse_row({"_received_epoch": 1794657184, "event": {**ev, "user": "2001:db8::1"}}, META)
    assert ip["is_anonymous"] is True


def test_deduplicate_keeps_earliest_collection():
    df = pd.DataFrame({"rev_id": [1, 1, 2], "timestamp_utc": ["a", "a", "b"],
                       "collection_timestamp_utc": ["2026-11-14T12:05:00Z", "2026-11-14T12:00:00Z", "x"],
                       "experiment_id": ["late", "early", "z"]})
    out, removed = deduplicate(df)
    assert removed == 1 and len(out) == 2
    assert out.loc[out["rev_id"] == 1, "experiment_id"].item() == "early"
