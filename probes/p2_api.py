"""Probe 2 + 4: MediaWiki Action API recentchanges."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from common import OUT, load_settings, session

API = "https://en.wikipedia.org/w/api.php"
PROPS = "title|ids|sizes|flags|user|userid|timestamp|comment|tags|loginfo"
BASE = {"action": "query", "list": "recentchanges", "format": "json", "formatversion": 2,
        "rcnamespace": 0, "rctype": "edit|new", "rcprop": PROPS, "rclimit": 500, "maxlag": 5}

s = session()
stats = {"requests": 0, "bytes_on_wire_approx": 0}


def get(params: dict) -> dict:
    """One polite API GET; returns parsed JSON."""
    time.sleep(1.0)
    t0 = time.perf_counter()
    r = s.get(API, params=params, timeout=30)
    stats["requests"] += 1
    stats["bytes_on_wire_approx"] += int(r.headers.get("content-length", len(r.content)))
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    print(f"    [{r.status_code}] {len(r.content):>9,} B decoded, {time.perf_counter() - t0:5.2f}s")
    return data


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"UA: {s.headers['User-Agent'][:40]}...  now={ts(now)}")

    print("\n(a) latest 500 ns0 edits, field coverage")
    rcs = get(BASE)["query"]["recentchanges"]
    keys = Counter(k for rc in rcs for k in rc)
    for k, c in sorted(keys.items()):
        print(f"    {k:14s} {c}/{len(rcs)}")
    print("    example:", json.dumps(rcs[0], ensure_ascii=False)[:600])
    (OUT / "p2_latest.json").write_text(json.dumps(rcs, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n(b) oldest available entry (rcdir=newer, rclimit=1)")
    oldest = get({**BASE, "rcdir": "newer", "rclimit": 1})["query"]["recentchanges"][0]["timestamp"]
    oldest_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
    print(f"    oldest = {oldest}  -> {(now - oldest_dt).total_seconds() / 86400:.1f} days of history")

    print("\n(c)+(d) samples of 500 edits starting at given ages (rcdir=older)")
    ages_h = [1, 3, 6, 12, 24, 36, 48, 72, 7 * 24, 14 * 24, 21 * 24]
    max_age_h = (now - oldest_dt).total_seconds() / 3600 - 12
    ages_h = [a for a in ages_h if a < max_age_h] + [int(max_age_h)]
    rows = []
    for age in ages_h:
        start = now - timedelta(hours=age)
        print(f"  age {age:>4}h  rcstart={ts(start)}")
        batch = get({**BASE, "rcstart": ts(start), "rcdir": "older"})["query"]["recentchanges"]
        t_new = datetime.fromisoformat(batch[0]["timestamp"].replace("Z", "+00:00"))
        t_old = datetime.fromisoformat(batch[-1]["timestamp"].replace("Z", "+00:00"))
        span_min = (t_new - t_old).total_seconds() / 60
        nonbot = [rc for rc in batch if not rc.get("bot")]
        rev = sum("mw-reverted" in rc.get("tags", []) for rc in nonbot)
        temp = [rc for rc in nonbot if rc.get("temp")]
        anon = [rc for rc in nonbot if rc.get("anon")]
        rev_temp = sum("mw-reverted" in rc.get("tags", []) for rc in temp)
        rows.append({"age_h": age, "n": len(batch), "span_min": round(span_min, 1),
                     "edits_per_hour": round(len(batch) / span_min * 60) if span_min else None,
                     "nonbot": len(nonbot), "reverted": rev,
                     "revert_rate_nonbot": round(rev / len(nonbot), 4) if nonbot else None,
                     "temp_users": len(temp), "anon_ip": len(anon),
                     "revert_rate_temp": round(rev_temp / len(temp), 4) if temp else None})
    print("\n  age_h   n  span_min  edits/h  nonbot  reverted  rate   temp  ip  rate_temp")
    for r in rows:
        print(f"  {r['age_h']:>5} {r['n']:>4} {r['span_min']:>8} {r['edits_per_hour']:>8} {r['nonbot']:>7} "
              f"{r['reverted']:>8}  {r['revert_rate_nonbot']:.3f} {r['temp_users']:>5} {r['anon_ip']:>3}  "
              f"{r['revert_rate_temp']}")
    (OUT / "p2_age_samples.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    eph = [r["edits_per_hour"] for r in rows if r["edits_per_hour"]]
    days = (now - oldest_dt).total_seconds() / 86400
    print(f"\n  mean edits/hour across samples = {sum(eph) / len(eph):,.0f}"
          f"  -> ~{sum(eph) / len(eph) * 24 * days:,.0f} ns0 edits reachable over {days:.1f} days (rough)")
    print(f"\n  API requests={stats['requests']}  approx bytes on wire={stats['bytes_on_wire_approx']:,}")


if __name__ == "__main__":
    main()
