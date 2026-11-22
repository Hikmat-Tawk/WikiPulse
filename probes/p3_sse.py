"""Probe 3: Wikimedia EventStreams (Server-Sent Events) for ~60 s."""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from common import session

URL = "https://stream.wikimedia.org/v2/stream/recentchange"


def listen(seconds: float, params: dict | None = None) -> None:
    s = session()
    s.headers["Accept"] = "text/event-stream"
    t0 = time.perf_counter()
    r = s.get(URL, params=params, stream=True, timeout=(10, 30))
    print(f"GET {r.url} -> {r.status_code}, transfer-encoding={r.headers.get('transfer-encoding')}, "
          f"content-type={r.headers.get('content-type')}, connect+headers={time.perf_counter() - t0:.2f}s")
    total_bytes = useful_bytes = 0
    events = useful = 0
    wikis: Counter[str] = Counter()
    lags_ms: list[float] = []
    buf: list[str] = []
    last_id = None
    for raw in r.iter_lines(decode_unicode=False):
        total_bytes += len(raw) + 1  # +1 for the newline
        line = raw.decode("utf-8", "replace")
        if line == "":  # end of one event
            data = "\n".join(l[5:].lstrip() for l in buf if l.startswith("data:"))
            ev_bytes = sum(len(l.encode()) + 1 for l in buf) + 1
            for l in buf:
                if l.startswith("id:"):
                    last_id = l[3:].strip()
            buf = []
            if not data:
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            events += 1
            wikis[ev.get("wiki", "?")] += 1
            if ev.get("wiki") == "enwiki" and ev.get("namespace") == 0 and ev.get("type") in ("edit", "new"):
                useful += 1
                useful_bytes += ev_bytes
                event_dt = datetime.fromisoformat(ev["meta"]["dt"].replace("Z", "+00:00"))
                lags_ms.append((datetime.now(timezone.utc) - event_dt).total_seconds() * 1000)
                if useful == 1:
                    print("  example useful event keys:", sorted(ev.keys()))
                    print("  example:", json.dumps({k: ev.get(k) for k in
                          ("id", "type", "namespace", "title", "user", "bot", "minor", "length", "revision",
                           "timestamp", "comment", "wiki")}, ensure_ascii=False)[:500])
        else:
            buf.append(line)
        if time.perf_counter() - t0 > seconds:
            break
    r.close()
    dur = time.perf_counter() - t0
    print(f"  duration={dur:.1f}s events={events} ({events / dur:.1f}/s) "
          f"enwiki-ns0-edits={useful} ({useful / dur:.2f}/s)")
    print(f"  bytes total={total_bytes:,} useful={useful_bytes:,} "
          f"useful share={useful_bytes / max(total_bytes, 1):.1%}")
    if lags_ms:
        lags_ms.sort()
        print(f"  freshness lag ms: min={lags_ms[0]:.0f} median={lags_ms[len(lags_ms) // 2]:.0f} "
              f"max={lags_ms[-1]:.0f}")
    print(f"  distinct wikis={len(wikis)} top={wikis.most_common(6)}")
    print(f"  last event id (for Last-Event-ID resume) = {str(last_id)[:160]}")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    print("== Run 1: no filter ==")
    listen(secs)
    print("\n== Run 2: try a server-side filter ?wiki=enwiki (does it change anything?) ==")
    listen(min(secs, 15), {"wiki": "enwiki"})
