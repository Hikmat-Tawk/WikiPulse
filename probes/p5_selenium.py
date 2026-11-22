"""Probe 5: does headless Chrome + Selenium work here, and what does it download?"""
from __future__ import annotations

import json
import time

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common import load_settings, user_agent

URL = ("https://en.wikipedia.org/wiki/Special:RecentChanges"
       "?limit=500&days=30&namespace=0&enhanced=0&hidebots=0")


def main() -> None:
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")            # needed in WSL/containers
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-agent={user_agent(load_settings())} Selenium-HeadlessChrome")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    t0 = time.perf_counter()
    driver = webdriver.Chrome(options=opts)      # Selenium Manager finds/downloads chromedriver
    t_start = time.perf_counter() - t0
    print(f"browser: {driver.capabilities.get('browserName')} {driver.capabilities.get('browserVersion')} "
          f"(startup {t_start:.2f}s)")
    try:
        t1 = time.perf_counter()
        driver.get(URL)
        WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.mw-changeslist-line")))
        t_load = time.perf_counter() - t1
        html = driver.page_source
        rows = BeautifulSoup(html, "lxml").select("li.mw-changeslist-line")
        revids = [li.get("data-mw-revid") for li in rows if li.get("data-mw-revid")]
        print(f"page load + rows visible: {t_load:.2f}s, rows={len(rows)}, rows with rev_id={len(revids)}")

        time.sleep(2)
        reqs: dict[str, dict] = {}
        for entry in driver.get_log("performance"):
            msg = json.loads(entry["message"])["message"]
            m, p = msg["method"], msg.get("params", {})
            if m == "Network.requestWillBeSent":
                reqs.setdefault(p["requestId"], {})["url"] = p["request"]["url"]
            elif m == "Network.responseReceived":
                reqs.setdefault(p["requestId"], {})["type"] = p.get("type")
                reqs[p["requestId"]]["status"] = p["response"].get("status")
            elif m == "Network.loadingFinished":
                reqs.setdefault(p["requestId"], {})["bytes"] = p.get("encodedDataLength", 0)
        total = sum(r.get("bytes", 0) for r in reqs.values())
        by_type: dict[str, list[int]] = {}
        for r in reqs.values():
            t = r.get("type") or "?"
            by_type.setdefault(t, [0, 0])
            by_type[t][0] += 1
            by_type[t][1] += r.get("bytes", 0)
        print(f"DevTools: requests={len(reqs)} encoded bytes on wire={total:,}")
        for t, (n, b) in sorted(by_type.items(), key=lambda x: -x[1][1]):
            print(f"   {t:12s} {n:4d} req {b:>12,} B")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
