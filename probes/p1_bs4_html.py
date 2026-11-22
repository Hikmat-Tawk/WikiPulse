"""Probe 1: which fields can requests + BeautifulSoup extract from Special:RecentChanges?"""
from __future__ import annotations

import re
import time
from collections import Counter
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from common import OUT, session

URL = "https://en.wikipedia.org/wiki/Special:RecentChanges"
PARAMS = {"limit": 500, "days": 30, "namespace": 0, "enhanced": 0, "hidebots": 0}


def main() -> None:
    s = session()
    t0 = time.perf_counter()
    r = s.get(URL, params=PARAMS, timeout=30)
    dt = time.perf_counter() - t0
    print(f"GET {r.url}\n  status={r.status_code} bytes(body,decoded)={len(r.content):,} time={dt:.2f}s "
          f"content-encoding={r.headers.get('content-encoding')}")
    (OUT / "p1_recentchanges.html").write_bytes(r.content)

    soup = BeautifulSoup(r.content, "lxml")
    rows = soup.select("li.mw-changeslist-line")
    print(f"  rows (li.mw-changeslist-line) = {len(rows)}")
    if not rows:
        print("  NO ROWS - list may be rendered client-side; inspect probes/output/p1_recentchanges.html")
        return

    found: Counter[str] = Counter()
    row_types: Counter[str] = Counter()
    tag_names: Counter[str] = Counter()
    examples = []
    for li in rows:
        cls = li.get("class", [])
        row_types.update(c for c in cls if c in ("mw-changeslist-edit", "mw-changeslist-new", "mw-changeslist-log",
                                                 "mw-changeslist-src-mw-categorize"))
        rec = {}
        rec["rev_id"] = li.get("data-mw-revid")
        rec["timestamp(data-mw-ts)"] = li.get("data-mw-ts")
        title_a = li.select_one("a.mw-changeslist-title")
        rec["page_title"] = title_a.get_text() if title_a else None
        diff_a = li.select_one("a.mw-changeslist-diff") or li.select_one("a[href*='diff=']")
        q = parse_qs(urlparse(diff_a["href"]).query) if diff_a else {}
        rec["page_id(curid)"] = q.get("curid", [None])[0]
        rec["parent_rev(oldid)"] = q.get("oldid", [None])[0]
        hist_a = li.select_one("a.mw-changeslist-history")
        hq = parse_qs(urlparse(hist_a["href"]).query) if hist_a else {}
        rec["page_id(history curid)"] = hq.get("curid", [None])[0]
        user_a = li.select_one("a.mw-userlink")
        rec["editor"] = user_a.get_text() if user_a else None
        rec["is_anonymous(mw-anonuserlink)"] = bool(user_a and "mw-anonuserlink" in user_a.get("class", [])) if user_a else None
        rec["temp_account(mw-tempuserlink)"] = bool(user_a and "mw-tempuserlink" in user_a.get("class", [])) if user_a else None
        bytes_el = li.select_one(".mw-plusminus-pos, .mw-plusminus-neg, .mw-plusminus-null")
        rec["byte_change"] = bytes_el.get_text(strip=True) if bytes_el else None
        rec["new_size(title attr)"] = bytes_el.get("title") if bytes_el else None
        comment = li.select_one(".comment")
        rec["edit_summary"] = comment.get_text(" ", strip=True) if comment else None
        rec["is_minor"] = li.select_one("abbr.minoredit") is not None
        rec["is_bot"] = li.select_one("abbr.botedit") is not None
        rec["is_new_page"] = li.select_one("abbr.newpage") is not None
        tags = [re.sub(r"^mw-tag-marker-", "", c) for t in li.select(".mw-tag-marker")
                for c in t.get("class", []) if c.startswith("mw-tag-marker-")]
        tag_classes = [c[len("mw-tag-"):] for c in cls if c.startswith("mw-tag-")]
        rec["tags(markers)"] = tags or None
        rec["tags(li classes)"] = tag_classes or None
        tag_names.update(tag_classes)
        rec["time_text"] = (li.select_one(".mw-changeslist-date") or li.select_one(".mw-changeslist-time"))
        rec["time_text"] = rec["time_text"].get_text() if rec["time_text"] else None
        rec["rc_id(any attr)"] = li.get("data-mw-rcid") or li.get("data-rcid")
        for k, v in rec.items():
            if v not in (None, "", False):
                found[k] += 1
        if len(examples) < 3:
            examples.append(rec)

    n = len(rows)
    print(f"  row types: {dict(row_types)}")
    print("\n  Field coverage (rows where value present / non-false):")
    keys = list(examples[0].keys())
    for k in keys:
        print(f"    {k:32s} {found[k]:4d}/{n}")
    print("\n  Example rows:")
    for e in examples:
        print("   ", e)
    print("\n  Top tag classes on <li>:", tag_names.most_common(15))
    ts = [li.get("data-mw-ts") for li in rows if li.get("data-mw-ts")]
    if ts:
        print(f"  time span on page: {min(ts)} .. {max(ts)}")


if __name__ == "__main__":
    main()
