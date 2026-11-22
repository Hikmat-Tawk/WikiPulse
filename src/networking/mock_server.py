"""A tiny local HTTP server that misbehaves on purpose - to test our client's politeness."""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockState:
    """Shared, thread-safe server configuration + request log."""

    def __init__(self, fail_first: int = 0, fail_status: int = 429, retry_after: float | None = None,
                 delay_s: float = 0.0) -> None:
        self.fail_first, self.fail_status = fail_first, fail_status
        self.retry_after, self.delay_s = retry_after, delay_s
        self.count = 0
        self.log: list[dict] = []
        self.lock = threading.Lock()

    def reset(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.count, self.log = 0, []


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like a real server

        def do_GET(self):
            arrived = time.time()
            with state.lock:
                state.count += 1
                n = state.count
                fail = n <= state.fail_first
            if state.delay_s:
                time.sleep(state.delay_s)
            status = state.fail_status if fail else 200
            body = json.dumps({"ok": not fail, "request_no": n}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if fail and state.retry_after is not None:
                self.send_header("Retry-After", str(state.retry_after))
            self.end_headers()
            self.wfile.write(body)
            with state.lock:
                state.log.append({"request_no": n, "arrived": arrived, "status": status,
                                  "user_agent": self.headers.get("User-Agent", "")[:40]})

        def log_message(self, *args):  # keep the console quiet
            pass

    return Handler


def start(port: int = 8429, state: MockState | None = None) -> tuple[ThreadingHTTPServer, MockState]:
    """Start the server in a background thread (127.0.0.1 only). Returns (server, state)."""
    state = state or MockState()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8429)
    ap.add_argument("--fail-first", type=int, default=3)
    ap.add_argument("--fail-status", type=int, default=429)
    ap.add_argument("--retry-after", type=float, default=2)
    ap.add_argument("--delay", type=float, default=0.0)
    a = ap.parse_args()
    server, _ = start(a.port, MockState(a.fail_first, a.fail_status, a.retry_after, a.delay))
    print(f"mock server on http://127.0.0.1:{a.port}/ (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
