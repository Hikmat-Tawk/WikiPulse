"""A polite, instrumented HTTP client shared by the requests-based collectors."""
from __future__ import annotations

import random
import threading
import time
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import requests
import urllib3

from src.config import load_settings, user_agent

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class RequestRecord:
    """One HTTP attempt as seen by our application."""
    label: str                 # which collector made it (api, bs4, ...)
    url: str
    status: int | None         # None if the request never got a response
    started_utc: str
    latency_ms: float
    total_ms: float            # until the full body was read
    body_bytes_wire: int
    body_bytes_decoded: int    # body after decompression
    header_bytes_approx: int
    attempt: int               # 0 = first try, 1 = first retry, ...
    retry_wait_s: float
    error: str | None = None


class TransientError(Exception):
    """Server asked us to slow down or failed temporarily."""


class PoliteClient:
    """requests.Session wrapper with rate limiting, backoff and per-request records."""

    def __init__(self, label: str, min_interval_s: float | None = None, max_retries: int | None = None,
                 settings: dict | None = None, timeout_s: float | None = None,
                 connect_timeout_s: float | None = None) -> None:
        """`timeout_s` = how long to wait for the server's answer (read timeout)."""
        self.settings = settings or load_settings()
        http = self.settings["http"]
        self.label = label
        read = http.get("timeout_s", 30) if timeout_s is None else timeout_s
        self.timeout = (connect_timeout_s, read) if connect_timeout_s else read
        self.min_interval = http.get("min_interval_s", 1.0) if min_interval_s is None else min_interval_s
        self.max_retries = http.get("max_retries", 5) if max_retries is None else max_retries
        self.backoff_base = http.get("backoff_base_s", 1.0)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent(self.settings)
        self.session.headers["Accept-Encoding"] = "gzip, deflate"
        self.records: list[RequestRecord] = []
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def _wait_turn(self) -> None:
        """Block until this thread may send (>= min_interval since the previous request)."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        if slot > now:
            time.sleep(slot - now)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        """Seconds to wait before the next attempt."""
        if retry_after:
            try:
                return max(float(retry_after), self.backoff_base)
            except ValueError:
                pass  # HTTP-date form; fall back to exponential
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base)

    def get(self, url: str, params: dict | None = None, check=None) -> requests.Response:
        """GET with rate limiting + retries. `check(response)` may raise TransientError"""
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            started = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            resp, error, retry_after, wire = None, None, None, 0
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, stream=True)
                wire = self._read_body(resp)
                if resp.status_code in RETRY_STATUSES:
                    retry_after = resp.headers.get("Retry-After")
                    raise TransientError(f"HTTP {resp.status_code}")
                if check:
                    check(resp)
            except (TransientError, requests.ConnectionError, requests.Timeout,
                    urllib3.exceptions.HTTPError, zlib.error) as exc:
                reason = getattr(exc.args[0], "reason", None) if exc.args else None
                error = f"{type(exc).__name__}: {reason or exc}"[:300]
                if resp is not None and retry_after is None:
                    retry_after = resp.headers.get("Retry-After")
            total_ms = (time.perf_counter() - t0) * 1000
            wait = self._backoff(attempt, retry_after) if error and attempt < self.max_retries else 0.0
            self._record(url, resp, started, total_ms, attempt, wait, error, wire)
            if not error:
                return resp
            if attempt == self.max_retries:
                raise RuntimeError(f"giving up after {attempt + 1} attempts: {error}")
            time.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    def _read_body(resp: requests.Response) -> int:
        """Read the body *still compressed*, count it, then decompress it ourselves."""
        raw = b"".join(resp.raw.stream(65536, decode_content=False))
        enc = resp.headers.get("Content-Encoding", "").lower()
        if enc == "gzip":
            body = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            try:
                body = zlib.decompress(raw)
            except zlib.error:
                body = zlib.decompress(raw, -zlib.MAX_WBITS)
        else:
            body = raw
        resp._content = body            # so resp.text / resp.json() work as usual
        resp._content_consumed = True
        return len(raw)

    def _record(self, url, resp, started, total_ms, attempt, wait, error, wire=0) -> None:
        if resp is not None:
            req_hdr = sum(len(k) + len(v) + 4 for k, v in resp.request.headers.items()) + len(resp.request.url) + 16
            resp_hdr = sum(len(k) + len(v) + 4 for k, v in resp.headers.items()) + 17
            rec = RequestRecord(self.label, resp.url, resp.status_code, started, resp.elapsed.total_seconds() * 1000,
                                total_ms, wire, len(resp.content), req_hdr + resp_hdr, attempt, wait, error)
        else:
            rec = RequestRecord(self.label, url, None, started, total_ms, total_ms, 0, 0, 0, attempt, wait, error)
        with self._lock:
            self.records.append(rec)

    def summary(self) -> dict:
        """Aggregate the records: the 4 required metrics + useful extras."""
        recs = self.records
        ok = [r for r in recs if r.status == 200]
        wire = sum(r.body_bytes_wire + r.header_bytes_approx for r in recs)
        return {
            "label": self.label,
            "requests": len(recs),
            "requests_ok": len(ok),
            "retries": sum(r.attempt > 0 for r in recs),
            "http_429": sum(r.status == 429 for r in recs),
            "maxlag_errors": sum(bool(r.error and "maxlag" in r.error) for r in recs),
            "kb_wire_approx": round(wire / 1024, 1),
            "kb_decoded": round(sum(r.body_bytes_decoded for r in recs) / 1024, 1),
            "avg_latency_ms": round(sum(r.latency_ms for r in recs) / len(recs), 1) if recs else None,
        }

    def records_as_dicts(self) -> list[dict]:
        return [asdict(r) for r in self.records]
