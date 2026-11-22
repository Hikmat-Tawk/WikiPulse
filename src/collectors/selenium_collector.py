"""Method A - Selenium driving headless Chrome on Special:RecentChanges."""
from __future__ import annotations

import argparse
import gzip
import json
import random
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.collectors.bs4_collector import page_params
from src.collectors.rc_html_parser import parse_recentchanges
from src.config import DATA, EXPERIMENTS, load_settings, user_agent


def make_driver() -> webdriver.Chrome:
    """Headless Chrome with a fresh profile (cold cache) and network logging on."""
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")              # needed in WSL / containers
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument(f"--user-agent={user_agent()} Selenium/HeadlessChrome")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


class NetworkLog:
    """Accumulates DevTools Network.* events into one record per request."""

    def __init__(self) -> None:
        self.reqs: dict[str, dict] = {}
        self.last_event = time.monotonic()

    def drain(self, driver) -> int:
        """Read new performance-log entries (each read empties Chrome's buffer). Returns #network events."""
        n = 0
        for entry in driver.get_log("performance"):
            msg = json.loads(entry["message"])["message"]
            method, p = msg["method"], msg.get("params", {})
            if not method.startswith("Network."):
                continue
            n += 1
            rid = p.get("requestId")
            r = self.reqs.setdefault(rid, {"decoded_bytes": 0})
            if method == "Network.requestWillBeSent":
                r.update(url=p["request"]["url"], t_sent=p["timestamp"], wall=p.get("wallTime"),
                         resource_type=p.get("type"))
            elif method == "Network.responseReceived":
                resp = p["response"]
                r.update(status=resp.get("status"), t_headers=p["timestamp"], protocol=resp.get("protocol"),
                         connection_id=resp.get("connectionId"), connection_reused=resp.get("connectionReused"),
                         remote_ip=resp.get("remoteIPAddress"), from_cache=resp.get("fromDiskCache"),
                         retry_after=(resp.get("headers") or {}).get("retry-after"),
                         resource_type=p.get("type", r.get("resource_type")))
            elif method == "Network.dataReceived":
                r["decoded_bytes"] += p.get("dataLength", 0)
            elif method == "Network.loadingFinished":
                r.update(t_done=p["timestamp"], wire_bytes=p.get("encodedDataLength", 0))
            elif method == "Network.loadingFailed":
                r.update(t_done=p["timestamp"], error=p.get("errorText"), wire_bytes=0)
        if n:
            self.last_event = time.monotonic()
        return n

    def records(self, main_url_prefix: str) -> list[dict]:
        """Flatten to records comparable with PoliteClient.RequestRecord (+ browser-only extras)."""
        out = []
        for r in self.reqs.values():
            if "url" not in r or r["url"].startswith("data:"):
                continue  # data: URLs never touch the network
            sent = r.get("t_sent")
            out.append({
                "label": "selenium",
                "url": r["url"],
                "status": r.get("status"),
                "started_utc": datetime.fromtimestamp(r["wall"], timezone.utc).isoformat() if r.get("wall") else None,
                "latency_ms": round((r["t_headers"] - sent) * 1000, 1) if sent and r.get("t_headers") else None,
                "total_ms": round((r["t_done"] - sent) * 1000, 1) if sent and r.get("t_done") else None,
                "body_bytes_wire": r.get("wire_bytes", 0),
                "body_bytes_decoded": r["decoded_bytes"],
                "header_bytes_approx": 0,
                "attempt": 0,
                "retry_wait_s": 0.0,
                "error": r.get("error"),
                "resource_type": r.get("resource_type"),
                "is_main_document": r.get("resource_type") == "Document" and r["url"].startswith(main_url_prefix),
                "protocol": r.get("protocol"),
                "connection_id": r.get("connection_id"),
                "connection_reused": r.get("connection_reused"),
                "remote_ip": r.get("remote_ip"),
                "from_cache": r.get("from_cache"),
                "retry_after": r.get("retry_after"),
            })
        return out


def load_once(driver, url: str, log: NetworkLog, timeout_s: int, idle_s: float = 1.5, max_idle_wait_s: float = 15):
    """Navigate, wait until rows are visible, then until the network is quiet for `idle_s`."""
    t0 = time.perf_counter()
    driver.get(url)
    WebDriverWait(driver, timeout_s).until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.mw-changeslist-line")))
    t_rows = time.perf_counter() - t0
    deadline = time.monotonic() + max_idle_wait_s
    log.drain(driver)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        log.drain(driver)
        if time.monotonic() - log.last_event >= idle_s:
            break
    t_idle = time.perf_counter() - t0 - (time.monotonic() - log.last_event)  # when the last event arrived
    return t_rows, max(t_idle, t_rows)


def collect(n: int, experiment_id: str, max_retries: int = 3, timeout_s: float | None = None) -> dict:
    """Start Chrome, load the page (with backoff on failure/429), parse, write raw files, return metrics."""
    settings = load_settings()
    base = settings["endpoints"]["recentchanges_html"]
    url = f"{base}?{urlencode(page_params(n))}"
    timeout_s = timeout_s or settings["http"].get("timeout_s", 30) * 2

    t0 = time.perf_counter()
    driver = make_driver()
    driver.set_page_load_timeout(timeout_s)
    startup_s = time.perf_counter() - t0
    try:
        for attempt in range(max_retries + 1):
            log = NetworkLog()
            error, wait = None, 0.0
            try:
                t_rows, t_idle = load_once(driver, url, log, timeout_s)
                main = [r for r in log.records(base) if r["is_main_document"]]
                status = main[0]["status"] if main else None
                if status in (429, 500, 502, 503, 504):
                    error = f"HTTP {status}"
                    ra = main[0].get("retry_after") if main else None
                    wait = float(ra) if ra and str(ra).isdigit() else 2 ** attempt + random.random()
            except (TimeoutException, WebDriverException) as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                wait = 2 ** attempt + random.random()
            if not error:
                break
            if attempt == max_retries:
                raise RuntimeError(f"selenium gave up after {attempt + 1} attempts: {error}")
            print(f"  attempt {attempt} failed ({error}); backing off {wait:.1f}s")
            time.sleep(wait)
        t_parse0 = time.perf_counter()
        html = driver.page_source
        rows, stats = parse_recentchanges(html)
        parse_s = time.perf_counter() - t_parse0
        browser = f"{driver.capabilities.get('browserName')} {driver.capabilities.get('browserVersion')}"
    finally:
        driver.quit()
    collected = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = log.records(base)

    raw_dir = DATA / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{experiment_id}.html.gz").write_bytes(gzip.compress(html.encode("utf-8")))
    line = {"experiment_id": experiment_id, "source_method": "selenium", "window_start": None,
            "collection_timestamp_utc": collected, "n": len(rows), "rows": rows}
    (raw_dir / f"{experiment_id}.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    metrics_path = EXPERIMENTS / "metrics" / f"{experiment_id}.requests.jsonl"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    lat = [r["latency_ms"] for r in records if r["latency_ms"] is not None]
    main = [r for r in records if r["is_main_document"]]
    return {
        "experiment_id": experiment_id, "browser": browser, "rows": len(rows), **stats,
        "startup_s": round(startup_s, 3), "rows_visible_s": round(t_rows, 3), "network_idle_s": round(t_idle, 3),
        "parse_s": round(parse_s, 3), "attempts": attempt + 1,
        "requests": len(records),
        "requests_main_document": len(main),
        "kb_wire": round(sum(r["body_bytes_wire"] for r in records) / 1024, 1),
        "kb_wire_main_document": round(sum(r["body_bytes_wire"] for r in main) / 1024, 1),
        "kb_decoded": round(sum(r["body_bytes_decoded"] for r in records) / 1024, 1),
        "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
        "protocols": sorted({str(r["protocol"]) for r in records}),
        "distinct_connections": len({r["connection_id"] for r in records if r["connection_id"]}),
        "failed_requests": sum(bool(r["error"]) for r in records),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--experiment-id", default=None)
    args = ap.parse_args()
    exp = args.experiment_id or f"selenium_latest_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    print(json.dumps(collect(args.n, exp), indent=1))
    from src.processing.normalizer import export_csv
    csv_path, stats = export_csv(DATA / "raw" / f"{exp}.jsonl")
    print(f"CSV ({stats['final_rows']} edits, common schema) -> {csv_path}")


if __name__ == "__main__":
    main()
