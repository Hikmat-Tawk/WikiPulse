# WikiPulse

**Analyzing Website Data Collection Efficiency and Network Behavior Using Multiple Web Scraping Techniques**
— Network Programming Lab project, Antonine University, Faculty of Engineering and Technology.

Authors: Ghina Younes, Hikmat Tawk · Instructor: Dr. Christian Salim

We collect the same data — English Wikipedia's *Recent Changes* — with three methods (plus one bonus), measure what
each costs on the network with application instrumentation **and** Linux packet-level tools, test how they behave when
HTTPS is blocked, and use the collected edits to predict which ones will later be reverted.

| Method | How it collects | Script |
|---|---|---|
| A. Selenium | headless Chrome loads `Special:RecentChanges` and reads the rendered page | `src/collectors/selenium_collector.py` |
| B. BeautifulSoup | `requests` downloads the same page's HTML; BS4 parses it | `src/collectors/bs4_collector.py` |
| C. API | MediaWiki Action API `list=recentchanges` (JSON) | `src/collectors/api_collector.py` |
| D. SSE (bonus) | Wikimedia EventStreams pushes every change live | `src/collectors/sse_collector.py` |

## Headline results (all from real runs — details in the report and the notebooks)

**Same workload: newest 500 main-namespace edits, 5 runs per method (mean ± std)**

| Method | Bandwidth (KB) | Time (s) | #Requests | Avg latency (ms) | Main-page latency (ms) |
|---|---|---|---|---|---|
| API | **36.8 ± 0.5** | **9.42 ± 1.35** | **1.0** | 9,254 ± 1,347 | 9,254 |
| BS4 | 123.6 ± 0.4 | 11.41 ± 0.31 | 1.0 | 9,487 ± 328 | 9,487 |
| Selenium | 656.2 ± 4.3 | 31.15 ± 18.16 | 16.8 ± 0.4 | 1,357 ± 495 | 10,208 |

(Screenshot 03 shows "± 0.5" for these two cells: the runner's console table rounded twice, 0.449 → 0.45 → 0.5;
fixed in `benchmark_runner.py`.)

- Packet capture: API 48 KB / 70 packets / 1 TCP connection per run; Selenium 756 KB to Wikimedia **plus ~9.4 MB of
  Chrome background traffic to Google** (mostly QUIC) that the page's DevTools log never shows.
- All structural fields agree 100 % across methods on shared edits.
- Firewall (ufw, outbound 443 blocked 60 s): `deny` costs 5.03 s per failed attempt (connect timeout, sockets in
  SYN-SENT), `reject` 0.02 s; every method recovered on its next collection (10–17 s after restore).
- Threads 1 → 4 under a shared 1 request/s limit: 34.6 s → 18.7 s, request starts still ≥ 1 s apart, 0 HTTP 429.
- Push vs pull (bonus): SSE saw edits after a median 3.3 s vs 18.5 s for 30-second polling, but used 38.7 KB vs 0.24 KB
  per useful edit (145 wikis, uncompressed). A forced 20 s outage lost 0 of 23 edits thanks to `Last-Event-ID`.
- **Recommendation: the MediaWiki API** — fewest bytes, one request, fastest, all fields.

**Revert prediction** (74,658 labeled non-bot article edits, 7.53 % reverted, chronological 80/20 split):
Random Forest PR-AUC **0.333** vs one-line rule 0.170 vs random 0.077; among its top 1 % most suspicious edits,
62 % were reverted. Presented as a moderation-assistance ranking, not a vandalism detector.

## Repository map (assignment deliverable → where)
| Deliverable | Location |
|---|---|
| Three scraper scripts (+ bonus) | `src/collectors/` |
| Same fields, saved to CSV | every scraper run ends by writing `data/normalized/<run>.csv` (common schema, `src/processing/normalizer.py`); committed samples from one run: `data/samples/{api,bs4,selenium}_latest500.csv` |
| Metrics table (bandwidth, time, #requests, avg latency) | `src/benchmarking/`, `experiments/metrics/bench_main.*` |
| `ss`, `iftop`, `nload`, `tcpdump`, Wireshark evidence | `experiments/screenshots/` (01–22), `src/networking/` (pcap parser, guides) |
| ufw block + recovery, multithreading, rate limiting | `src/networking/phase4_auto.sh`, `src/benchmarking/{resilience_runner,threads_experiment,backoff_demo}.py` |
| Networking analysis notebook (with screenshots) | `notebooks/network_analysis.ipynb` |
| EDA (pandas, matplotlib, seaborn) | `notebooks/exploratory_analysis.ipynb` |
| ML notebook | `notebooks/machine_learning.ipynb`, code in `src/ml/` |
| **Final PDF report** | `reports/WikiPulse_Report.pdf` |
| Figures | generated into `reports/figures/` by the scripts and notebooks |
| Record of measurement corrections | report, Appendix C |

## Setup (WSL2 Ubuntu 24.04 — the Linux tools need Linux)
```bash
sudo apt install -y python3.12-venv python3-pip tcpdump iftop nload ufw
python3 -m venv ~/.venvs/wikipulse
~/.venvs/wikipulse/bin/pip install -r requirements-lock.txt      # exact versions used (or requirements.txt)
cp config/settings.example.yaml config/settings.yaml               # then put your contact in http.contact
```
Selenium also needs Google Chrome (`google-chrome-stable` .deb). Wikimedia asks every automated client for a
descriptive User-Agent with contact details; the collectors refuse to run without one.

## Reproduce
Run everything from the repo root with `PY=~/.venvs/wikipulse/bin/python`.
```bash
$PY -m pytest -q                                                    # 22 tests (normalisation, labeling, features, pcap)

# 1. Dataset for EDA/ML (~13 min, 171 polite API requests). Data are git-ignored, and Wikipedia keeps only
#    30 days of recent changes, so a new pull gets DIFFERENT edits and slightly different numbers.
$PY -m src.collectors.api_collector sample --windows 170 --seed 42
$PY -m src.processing.normalizer data/raw/api_sample_seed42.jsonl
$PY -m src.ml.labeling data/normalized/api_sample_seed42.csv --window-h 72

# 2. Benchmark (5 rounds x 3 methods). In other terminals, run alongside it:
#    sudo tcpdump -i any -Z "$USER" -w experiments/captures/scraper_trace.pcap port 80 or 443
#    ss -tanp | grep -E "python|chrome"     (and iftop -i eth0 -P -n, nload eth0)
$PY -m src.benchmarking.benchmark_runner --runs 5 --n 500 --bench-id bench_main
$PY -m src.networking.pcap_parser experiments/captures/scraper_trace.pcap --runs experiments/metrics/bench_main.runs.csv
$PY -m src.networking.traffic_split experiments/captures/scraper_trace.pcap --runs experiments/metrics/bench_main.runs.csv
$PY -m src.benchmarking.plots

# 3. Resilience
$PY -m src.benchmarking.backoff_demo                                # local mock server only
bash src/networking/run_threads_with_ss.sh                          # threads 1 vs 4 on the real API
bash src/networking/phase4_auto.sh                                  # ufw tests: asks for sudo once, always restores

# 4. Bonus: push vs pull (10 min)
$PY -m src.benchmarking.sse_vs_polling --duration 600 --outage 20

# 5. Notebooks (open the printed link in a browser)
~/.venvs/wikipulse/bin/jupyter lab --no-browser notebooks/
```
The network notebook runs from a clean clone as-is (it reads the committed result files). The EDA and ML notebooks
need step 1 first.

## Responsible scraping
Read-only; descriptive User-Agent with contact; ≥ 1 s between requests (shared limiter across threads, max 4 threads);
`maxlag`, HTTP 429/5xx with `Retry-After` and exponential backoff + jitter; 0 HTTP 429 received in all runs.
429 handling is demonstrated against a local mock server, never by provoking Wikimedia. Firewall changes only inside
WSL, always auto-restored. Secrets, raw data and packet captures are git-ignored (`config/settings.yaml`,
`data/raw|normalized|labeled`, `experiments/captures`, TLS key logs).
