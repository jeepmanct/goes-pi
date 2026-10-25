#!/usr/bin/env python3
"""Month-in-review time-lapse videos from the rendered views, saved to ~/goes/data/archive/<view>-YYYY-MM.mp4.

The panel's watch thread runs this once a month (on the 1st, after 03:00 US Eastern) for the previous month.
By hand:  monthly.py --month 2026-10 --view northeast [--out DIR] [--max-frames N]
"""
import argparse
import datetime as dt
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

GOES = Path.home() / "goes"
ARCHIVE = GOES / "data" / "archive"
LOCAL = ZoneInfo("America/New_York")
VIEWS = ("northeast", "globe")      # what the monthly run builds
MIN_FRAMES = 24


def _digest():
    spec = importlib.util.spec_from_file_location("goes_digest", GOES / "digest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def previous_month(now=None):
    now = now or dt.datetime.now(LOCAL)
    first = now.replace(day=1)
    last = first - dt.timedelta(days=1)
    return last.year, last.month


def build(view, year, month, out_dir=ARCHIVE, width=1280, fps=15, max_frames=720, digest=None):
    """Write the month's video and return its path, or None if there is too little footage or it already exists."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{view}-{year:04d}-{month:02d}.mp4"
    if dest.exists():
        return None
    d = digest or _digest()
    lo = dt.datetime(year, month, 1, tzinfo=LOCAL)
    hi = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=LOCAL)
    fr = [(t, p) for t, p in d.frames(view) if lo <= t < hi]
    if len(fr) < MIN_FRAMES:
        return None
    pick = fr[:: max(1, -(-len(fr) // max_frames))]
    with tempfile.TemporaryDirectory(prefix="goes-month-") as td:
        lst, tmp = Path(td) / "frames.txt", dest.with_name(dest.name + ".part.mp4")
        last = pick[-1][1]
        lst.write_text("".join(f"file '{p}'\nduration {1 / fps:.4f}\n" for _t, p in pick) + f"file '{last}'\nduration 2\nfile '{last}'\n")
        r = subprocess.run(["nice", "-n", "15", "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-vf", f"scale={width}:-2:flags=lanczos,format=yuv420p", "-r", str(fps),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-movflags", "+faststart", str(tmp)],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode:
            tmp.unlink(missing_ok=True)
            print("monthly video failed:", r.stderr.strip()[-300:], file=sys.stderr)
            return None
        tmp.replace(dest)
    print(f"wrote {dest} ({len(pick)} frames)")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM (default: the previous month)")
    ap.add_argument("--view", action="append", help="view name (repeatable; default northeast and globe)")
    ap.add_argument("--out", type=Path, default=ARCHIVE)
    ap.add_argument("--max-frames", type=int, default=720)
    a = ap.parse_args()
    y, m = map(int, a.month.split("-")) if a.month else previous_month()
    d = _digest()
    for v in a.view or VIEWS:
        build(v, y, m, a.out, max_frames=a.max_frames, digest=d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
