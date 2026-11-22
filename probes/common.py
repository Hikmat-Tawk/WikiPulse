"""Shared helpers for the Phase 0 feasibility probes (throwaway code)."""
from __future__ import annotations

from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "probes" / "output"
OUT.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    """Load config/settings.yaml (falls back to the example to give a clear error)."""
    path = ROOT / "config" / "settings.yaml"
    if not path.exists():
        raise SystemExit("Copy config/settings.example.yaml to config/settings.yaml and set http.contact")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def user_agent(settings: dict) -> str:
    """Return 'WikiPulse/0.1 (description; contact) python-requests/x.y'."""
    http = settings["http"]
    if "YOUR_CONTACT" in http["contact"]:
        raise SystemExit("Set http.contact in config/settings.yaml before contacting Wikimedia.")
    return f'{http["project"]} ({http["description"]}; {http["contact"]}) python-requests/{requests.__version__}'


def session() -> requests.Session:
    """A requests.Session with our User-Agent (Session = TCP/TLS connection reuse)."""
    s = requests.Session()
    s.headers["User-Agent"] = user_agent(load_settings())
    return s
