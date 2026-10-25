#!/usr/bin/env python3
"""Keep ~/goes/storm.json pointed at the newest NHC tropical cyclone advisory in the EMWIN feed.

Reads TCM (forecast/advisory) bulletins, takes the centre position, and writes storm.json for views.py's
'storm' view. A hand-written storm.json (without "auto": true) is never touched. When no advisory has arrived for
12 hours, an auto-written file is removed.   Run from cron every 30 min:  storm.py [--dir DIR] [--out FILE]
"""
import argparse
import json
import re
import time
from pathlib import Path

EMWIN = Path("/home/pi/goes/data/emwin")
OUT = Path("/home/pi/goes/storm.json")
MAX_AGE = 12 * 3600
HEAD = re.compile(r"^\s*((?:POST-TROPICAL CYCLONE|TROPICAL DEPRESSION|TROPICAL STORM|SUBTROPICAL STORM|HURRICANE|"
                   r"POTENTIAL TROPICAL CYCLONE|TYPHOON|SUPER TYPHOON|TROPICAL CYCLONE)\s+([A-Z0-9-]+))\s+"
                   r"(?:FORECAST/ADVISORY|ADVISORY|DISCUSSION)", re.M)
CENTER = re.compile(r"CENTER LOCATED NEAR\s+(\d+(?:\.\d+)?)([NS])\s+(\d+(?:\.\d+)?)([EW])")


def parse(text):
    """(label, lat, lon) from a TCM bulletin, or None."""
    text = text.replace("\r", "")
    h, c = HEAD.search(text), CENTER.search(text)
    if not (h and c):
        return None
    lat = float(c.group(1)) * (1 if c.group(2) == "N" else -1)
    lon = float(c.group(3)) * (1 if c.group(4) == "E" else -1)
    return h.group(1).title(), lat, lon


def newest(directory, now):
    best = None
    for p in directory.glob("*TCM*"):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if now - mt > MAX_AGE or (best and mt <= best[0]):
            continue
        got = parse(p.read_text(errors="replace"))
        if got:
            best = (mt, got)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=EMWIN)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    cur = None
    if a.out.exists():
        try:
            cur = json.loads(a.out.read_text())
        except ValueError:
            cur = {}
        if not cur.get("auto"):
            return 0                      # hand-written: leave alone
    hit = newest(a.dir, time.time())
    if not hit:
        if cur is not None:
            a.out.unlink(missing_ok=True)
        return 0
    label, lat, lon = hit[1]
    new = {"name": label, "lat": lat, "lon": lon, "half": 5.0, "auto": True}
    if cur != new:
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(new))
        tmp.replace(a.out)
        print(f"storm: {label} {lat:+.1f} {lon:+.1f}")
    elif cur is not None:
        a.out.touch()                     # keep the file fresh while advisories keep coming
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
