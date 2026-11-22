"""Load project settings and build the Wikimedia-compliant User-Agent."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXPERIMENTS = ROOT / "experiments"


@lru_cache(maxsize=1)
def load_settings() -> dict:
    """Read config/settings.yaml. Exit with a clear message if it is missing."""
    path = ROOT / "config" / "settings.yaml"
    if not path.exists():
        raise SystemExit("Missing config/settings.yaml - copy config/settings.example.yaml and set http.contact.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def user_agent(settings: dict | None = None) -> str:
    """Return e.g. 'WikiPulse/0.1 (lab project; contact) python-requests/2.32.3'."""
    http = (settings or load_settings())["http"]
    if "YOUR_CONTACT" in http["contact"]:
        raise SystemExit("Set http.contact in config/settings.yaml before contacting Wikimedia.")
    return f'{http["project"]} ({http["description"]}; {http["contact"]}) python-requests/{requests.__version__}'
