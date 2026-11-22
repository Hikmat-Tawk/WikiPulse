"""Parse the Special:RecentChanges HTML list (shared by the BS4 and Selenium collectors)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

_NS = re.compile(r"^mw-changeslist-ns-(\d+)$")
_DIGITS = re.compile(r"[\d,]+")


def _int_or_none(text: str | None) -> int | None:
    """'−1,234' / '+56' / '0' -> int; None if no number."""
    if not text:
        return None
    t = text.strip().replace("−", "-").replace(",", "")
    m = re.search(r"[-+]?\d+", t)
    return int(m.group()) if m else None


def _query_param(a, name: str) -> str | None:
    if a is None or not a.get("href"):
        return None
    return parse_qs(urlparse(a["href"]).query).get(name, [None])[0]


def parse_row(li) -> dict | None:
    """One <li> -> dict of raw HTML fields, or None for non-edit rows (log entries, categorization)."""
    classes = li.get("class", [])
    if "mw-changeslist-edit" not in classes and "mw-changeslist-src-mw-new" not in classes:
        return None
    ns = next((int(m.group(1)) for c in classes if (m := _NS.match(c))), None)
    title_a = li.select_one("a.mw-changeslist-title")
    diff_a = li.select_one("a.mw-changeslist-diff")
    hist_a = li.select_one("a.mw-changeslist-history")
    user_a = li.select_one("a.mw-userlink")
    bytes_el = li.select_one(".mw-diff-bytes")
    comment = li.select_one("span.comment")
    user_cls = user_a.get("class", []) if user_a else []

    summary = None
    if comment is not None and "history-deleted" not in comment.get("class", []):
        summary = " ".join(comment.get_text().split())
        if summary.startswith("(") and summary.endswith(")"):
            summary = summary[1:-1].strip()

    new_size = None
    if bytes_el is not None and bytes_el.get("title"):
        m = _DIGITS.search(bytes_el["title"])
        new_size = int(m.group().replace(",", "")) if m else None

    return {
        "rev_id": li.get("data-mw-revid"),
        "timestamp": li.get("data-mw-ts"),
        "namespace": ns,
        "page_title": title_a.get_text() if title_a else None,
        "page_id": _query_param(hist_a, "curid") or _query_param(diff_a, "curid"),
        "parent_rev_id": _query_param(diff_a, "oldid"),
        "editor": user_a.get_text() if user_a else None,
        "is_logged_out": ("mw-changeslist-user-unregistered" in classes
                          or "mw-anonuserlink" in user_cls or "mw-tempuserlink" in user_cls),
        "byte_change": _int_or_none(bytes_el.get_text()) if bytes_el else None,
        "new_size": new_size,
        "edit_summary": summary,
        "is_minor": li.select_one("abbr.minoredit") is not None,
        "is_bot": li.select_one("abbr.botedit") is not None,
        "is_new_page": li.select_one("abbr.newpage") is not None or "mw-changeslist-src-mw-new" in classes,
        "tag_classes": [c[len("mw-tag-"):] for c in classes if c.startswith("mw-tag-")],
    }


def parse_recentchanges(html: str | bytes) -> tuple[list[dict], dict]:
    """Parse the whole page. Returns (edit rows, stats about skipped rows)."""
    soup = BeautifulSoup(html, "lxml")
    lis = soup.select("li.mw-changeslist-line")
    rows = [r for r in (parse_row(li) for li in lis) if r is not None]
    return rows, {"html_rows": len(lis), "edit_rows": len(rows), "skipped_non_edit": len(lis) - len(rows)}
