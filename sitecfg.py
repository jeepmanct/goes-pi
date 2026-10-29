"""Where the dish is. Reads site.json next to this file (git-ignored; copy site.example.json and edit)."""
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load():
    for name in ("site.json", "site.example.json"):
        try:
            return json.loads((_DIR / name).read_text())
        except (OSError, ValueError):
            pass
    return {"name": "Home", "lat": 40.0, "lon": -75.0}
