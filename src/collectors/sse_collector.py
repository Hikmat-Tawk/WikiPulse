"""Method D (bonus) - Wikimedia EventStreams over Server-Sent Events (push instead of pull)."""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from src.config import DATA, EXPERIMENTS, load_settings, user_agent

TARGET_WIKI = "enwiki"


def is_useful(ev: dict) -> bool:
    """English Wikipedia, main namespace, edit or page creation (same workload as A/B/C)."""
    return ev.get("wiki") == TARGET_WIKI and ev.get("namespace") == 0 and ev.get("type") in ("edit", "new")


@dataclass
class SSEStats:
    connections: int = 0
    reconnects: int = 0
    bytes_total: int = 0
    bytes_useful: int = 0         # decoded bytes of the events we kept
    events_total: int = 0
    events_useful: int = 0
    replayed_after_reconnect: int = 0
    wikis: Counter = field(default_factory=Counter)
    errors: list = field(default_factory=list)
    content_encoding: str | None = None
    disconnect_window: tuple | None = None  # (closed_at_epoch, reconnected_at_epoch)


class SSECollector:
    def __init__(self, on_event=None, backoff_base_s: float = 1.0, max_backoff_s: float = 60.0) -> None:
        settings = load_settings()
        self.url = settings["endpoints"]["eventstreams"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent(settings), "Accept": "text/event-stream",
                                     "Accept-Encoding": "gzip"})
        self.on_event = on_event
        self.backoff_base, self.max_backoff = backoff_base_s, max_backoff_s
        self.last_event_id: str | None = None
        self.retry_hint_ms: int | None = None
        self.stats = SSEStats()

    def _lines(self, resp):
        """Yield decoded text lines while counting raw (wire) body bytes."""
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        self.stats.content_encoding = enc or "identity"
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else None
        buf = b""
        for chunk in resp.raw.stream(8192, decode_content=False):
            self.stats.bytes_total += len(chunk)
            buf += dec.decompress(chunk) if dec else chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                yield line.rstrip(b"\r").decode("utf-8", "replace")

    def run(self, duration_s: float, stop: threading.Event | None = None,
            disconnect_at_s: float | None = None, outage_s: float = 0.0) -> SSEStats:
        """Listen for `duration_s` seconds (or until `stop` is set)."""
        stop = stop or threading.Event()
        t_end = time.time() + duration_s
        t_disconnect = time.time() + disconnect_at_s if disconnect_at_s else None
        reconnected_at = None
        attempt = 0
        while time.time() < t_end and not stop.is_set():
            headers = {"Last-Event-ID": self.last_event_id} if self.last_event_id else {}
            try:
                resp = self.session.get(self.url, headers=headers, stream=True, timeout=(10, 60))
                resp.raise_for_status()
                self.stats.connections += 1
                if self.stats.connections > 1:
                    self.stats.reconnects += 1
                attempt = 0
                data, ev_id, raw_len = [], None, 0
                for line in self._lines(resp):
                    if time.time() >= t_end or stop.is_set():
                        break
                    if t_disconnect and time.time() >= t_disconnect:   # scheduled outage (resume test)
                        closed = time.time()
                        resp.close()
                        t_disconnect = None
                        print(f"  [sse] forced disconnect, outage {outage_s:.0f}s, last id kept for resume", flush=True)
                        time.sleep(outage_s)
                        reconnected_at = time.time()
                        self.stats.disconnect_window = (closed, reconnected_at)
                        break
                    raw_len += len(line.encode()) + 1
                    if line == "":                     # blank line = end of one event
                        if data:
                            self._dispatch("\n".join(data), ev_id, raw_len, reconnected_at)
                        data, ev_id, raw_len = [], None, 0
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif line.startswith("id:"):
                        ev_id = line[3:].strip()
                    elif line.startswith("retry:"):
                        self.retry_hint_ms = int(line[6:].strip() or 0)
                resp.close()
            except (requests.RequestException, zlib.error) as exc:
                self.stats.errors.append(f"{datetime.now(timezone.utc):%H:%M:%S} {type(exc).__name__}: {str(exc)[:120]}")
                wait = (self.retry_hint_ms / 1000) if self.retry_hint_ms else \
                    min(self.max_backoff, self.backoff_base * 2 ** attempt) + random.uniform(0, 1)
                attempt += 1
                print(f"  [sse] error, reconnecting in {wait:.1f}s: {self.stats.errors[-1]}", flush=True)
                time.sleep(wait)
        return self.stats

    def _dispatch(self, data: str, ev_id: str | None, raw_len: int, reconnected_at: float | None) -> None:
        received = time.time()
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            return
        if ev_id:
            self.last_event_id = ev_id
        self.stats.events_total += 1
        self.stats.wikis[ev.get("wiki", "?")] += 1
        if not is_useful(ev):
            return
        self.stats.events_useful += 1
        self.stats.bytes_useful += raw_len
        if reconnected_at and ev.get("meta", {}).get("dt"):
            dt = datetime.fromisoformat(ev["meta"]["dt"].replace("Z", "+00:00")).timestamp()
            if dt < reconnected_at:
                self.stats.replayed_after_reconnect += 1
        if self.on_event:
            self.on_event(ev, received)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--experiment-id", default=None)
    a = ap.parse_args()
    exp = a.experiment_id or f"sse_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    rows: list[dict] = []
    col = SSECollector(on_event=lambda ev, t: rows.append({"_received_epoch": t, "event": ev}))
    stats = col.run(a.duration)
    out = DATA / "raw" / f"{exp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    line = {"experiment_id": exp, "source_method": "sse", "window_start": None,
            "collection_timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n": len(rows), "rows": rows}
    out.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    s = {k: v for k, v in stats.__dict__.items() if k != "wikis"}
    s["top_wikis"] = stats.wikis.most_common(5)
    (EXPERIMENTS / "metrics").mkdir(parents=True, exist_ok=True)
    (EXPERIMENTS / "metrics" / f"{exp}.sse_stats.json").write_text(json.dumps(s, indent=1, default=str))
    print(json.dumps(s, indent=1, default=str))
    print("->", out)


if __name__ == "__main__":
    main()
