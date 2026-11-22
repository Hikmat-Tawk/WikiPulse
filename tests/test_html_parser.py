"""Tests for the Recent Changes HTML parser + HTML normalisation (row taken from a real page, shortened)."""
import json

from src.collectors.rc_html_parser import parse_recentchanges
from src.processing.normalizer import COLUMNS, normalize_html_row

ROW = """<ul><li class="mw-changeslist-line mw-changeslist-edit mw-changeslist-ns-0 mw-changeslist-user-unregistered
 mw-changeslist-minor mw-tag-mobile_edit mw-tag-visualeditor" data-mw-revid="1385639532" data-mw-ts="20261114115046">
<a class="mw-changeslist-diff" href="/w/index.php?title=Almaty_Region&amp;curid=3633775&amp;diff=1385639532&amp;oldid=1373646990">diff</a>
<a class="mw-changeslist-history" href="/w/index.php?title=Almaty_Region&amp;curid=3633775&amp;action=history">hist</a>
<a class="mw-changeslist-title" href="/wiki/Almaty_Region">Almaty Region</a>
<abbr class="minoredit">m</abbr>
<span class="mw-plusminus-neg mw-diff-bytes" title="63,608 bytes after change of this size">−1,189</span>
<a class="mw-userlink mw-tempuserlink" href="/wiki/User:~2026-123"><bdi>~2026-123</bdi></a>
<span class="comment">(added <a href="/wiki/Category:X">Category:X</a> using <a href="/wiki/WP:HC">HotCat</a>)</span>
</li>
<li class="mw-changeslist-line mw-changeslist-log mw-changeslist-ns-0" data-mw-ts="20261114115040">log entry</li></ul>"""

META = {"source_method": "bs4", "experiment_id": "t", "collection_timestamp_utc": "2026-11-14T11:51:46Z"}


def test_parser_extracts_fields_and_skips_log_rows():
    rows, stats = parse_recentchanges(ROW)
    assert stats == {"html_rows": 2, "edit_rows": 1, "skipped_non_edit": 1}
    r = rows[0]
    assert r["rev_id"] == "1385639532" and r["page_id"] == "3633775" and r["parent_rev_id"] == "1373646990"
    assert r["byte_change"] == -1189 and r["new_size"] == 63608  # Unicode minus + thousands separator
    assert r["is_logged_out"] and r["is_minor"] and not r["is_bot"]
    assert r["edit_summary"] == "added Category:X using HotCat"  # parentheses removed, spaces kept


def test_html_normalisation_matches_schema_and_derives_old_size():
    rec = normalize_html_row(parse_recentchanges(ROW)[0][0], META)
    assert set(rec) == set(COLUMNS)
    assert rec["rc_id"] is None and rec["user_id"] is None  # not visible in HTML -> null, never guessed
    assert rec["old_size"] == 63608 + 1189
    assert rec["timestamp_utc"] == "2026-11-14T11:50:46Z" and rec["freshness_lag_ms"] == 60000
    assert json.loads(rec["tags"]) == ["mobile edit", "visualeditor"]  # CSS '_' -> real tag name
