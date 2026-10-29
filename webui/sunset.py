"""Sunset glow outlook for Stratford from the newest GOES scans.

Looks along the sunset bearing (a fan of +-20 degrees) out to ~650 km. Cold (high or middle) cloud
there catches the low sun and glows; low cloud near the horizon blocks it. The score is a rough
satellite-only guess: C13 brightness temperature tells high cloud from low, and C02 daylight
brightness picks out low cloud that is too warm to stand out in the infrared.
"""
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo
import datetime as dt

import numpy as np

GOES_DIR = Path(__file__).resolve().parent.parent
LOG = GOES_DIR / "sunset-log.jsonl"
LOCAL_TZ = ZoneInfo("America/New_York")

BEARING_OFFSETS = (-20, -10, 0, 10, 20)
DISTANCES_KM = (40, 80, 120, 180, 250, 330, 420, 520, 640)
NEAR_KM = 250                   # low cloud inside this distance blocks the horizon
HIGH_K = 262.0                  # colder than this = high or middle cloud
THICK_K = 230.0                 # deep, thick tops: partly dark underneath
LOW_VIS = 0.15                  # C02 brightness (0..1) above this with a warm top = low cloud
MAX_SCAN_AGE = 75 * 60

_cache = {"t": 0.0, "d": None}


def _mod(key, path):
    m = sys.modules.get(key)
    if m is None:
        spec = importlib.util.spec_from_file_location(key, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[key] = m
        spec.loader.exec_module(m)
    return m


def _views():
    return _mod("goes_views", GOES_DIR / "views.py")


def _local():
    return _mod("goes_local", GOES_DIR / "webui" / "local.py")


def sunset(now):
    """(UNIX time of the next sunset, azimuth in degrees) after `now`, minus a 10 minute grace period."""
    loc = _local()
    day = dt.datetime.fromtimestamp(now, LOCAL_TZ).date()
    for add in (0, 1):
        noon = dt.datetime.combine(day + dt.timedelta(days=add), dt.time(12), LOCAL_TZ).timestamp()
        prev = noon
        for i in range(1, 13 * 60):          # minute steps from noon to 01:00
            t = noon + i * 60
            if loc.sun_pos(t)[0] <= -0.833:
                if t + 600 > now:
                    return t, loc.sun_pos(prev)[1]
                break
            prev = t
    return None, None


def _point(lat0, lon0, az, km):
    la, lo, b, d = math.radians(lat0), math.radians(lon0), math.radians(az), km / 6371.0
    la2 = math.asin(math.sin(la) * math.cos(d) + math.cos(la) * math.sin(d) * math.cos(b))
    lo2 = lo + math.atan2(math.sin(b) * math.sin(d) * math.cos(la), math.cos(d) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def _canvas(high):
    """How well a given share of high cloud lights up: none or total overcast is poor, patchy to half is best."""
    if high < 0.10:
        return high / 0.10 * 0.2
    if high < 0.30:
        return 0.2 + (high - 0.10) / 0.20 * 0.8
    if high <= 0.60:
        return 1.0
    return max(0.5, 1.0 - (high - 0.60) / 0.40 * 0.5)


def _label(score):
    return "Great" if score >= 75 else "Good" if score >= 55 else "Maybe" if score >= 35 else "Unlikely"


def evaluate(now=None):
    now = now or time.time()
    ss, az = sunset(now)
    if ss is None:
        return {"error": "Could not work out the sunset time"}
    v = _views()
    out = {"sunset": ss, "azimuth": round(az), "minutes_to": round((ss - now) / 60),
           "best_from": ss - 600, "best_to": ss + 1200}
    files = v.scan_files()
    if not files.get("13"):
        return {**out, "error": "No infrared scan yet"}
    t13, p13 = files["13"][-1]
    out["scan"] = t13.timestamp()
    table = v.bt_table(p13)
    if now - t13.timestamp() > MAX_SCAN_AGE or table is None:
        return {**out, "error": "No fresh infrared scan"}
    a13 = v.load_full(p13)
    p02 = v.nearest(files, "02", t13)
    a02 = v.load_full(p02) if p02 else None
    # the C02 scene is only trustworthy while the sun is well up where we look
    daylight = a02 is not None and _local().sun_pos(t13.timestamp())[0] > 12
    lat0, lon0 = v.HOME
    n = high = thick = low_near = low_far = n_near = n_far = 0
    for off in BEARING_OFFSETS:
        for km in DISTANCES_KM:
            la, lo = _point(lat0, lon0, az + off, km)
            c, r, vis = v.to_pixels(np.array(la), np.array(lo))
            if not bool(vis):
                continue
            c, r = int(c), int(r)
            if not (2 <= c < v.NATIVE - 2 and 2 <= r < v.NATIVE - 2):
                continue
            bt = float(table[a13[r - 1:r + 2, c - 1:c + 2]].mean())
            bright = float(a02[r - 2:r + 3, c - 2:c + 3].mean()) / 255.0 if daylight else 0.0
            n += 1
            is_high, is_thick = bt < HIGH_K, bt < THICK_K
            is_low = (not is_high) and bright >= LOW_VIS
            high += 1 if is_high and not is_thick else 0
            thick += 1 if is_thick else 0
            near = km <= NEAR_KM
            n_near += near
            n_far += not near
            if is_low:
                low_near += near
                low_far += not near
    if n < 10:
        return {**out, "error": "Too little of the western sky is in view"}
    high_frac = (high + 0.5 * thick) / n
    block = (low_near / max(n_near, 1)) + 0.4 * (low_far / max(n_far, 1)) + 0.5 * (thick / n)
    score = round(100 * _canvas(high_frac) * max(0.0, 1.0 - 1.3 * min(block, 1.0)))
    out.update(score=score, label=_label(score), high=round(high_frac, 2), low_near=round(low_near / max(n_near, 1), 2),
               thick=round(thick / n, 2), low_seen=daylight, samples=n)
    out["why"] = _why(out)
    return out


def _why(d):
    h, ln = d["high"], d["low_near"]
    bits = []
    if h < 0.05:
        bits.append("almost no high cloud to the west, so little to light up")
    elif h < 0.10:
        bits.append("only a little high cloud to the west")
    elif h <= 0.60:
        bits.append(f"high cloud over about {round(h * 100)}% of the sky to the west, a good amount to catch the light")
    else:
        bits.append(f"high cloud over about {round(h * 100)}% of the sky to the west, thick enough to dull it")
    if d["low_seen"]:
        bits.append("low cloud is blocking the horizon" if ln >= 0.2 else "the horizon looks mostly clear" if ln < 0.08 else "some low cloud near the horizon")
    else:
        bits.append("low cloud near the horizon can't be seen this late in the day")
    if d["thick"] >= 0.15:
        bits.append("thick storm cloud in the west")
    return "; ".join(bits)


def outlook(max_age=300):
    """Cached evaluate() for the panel."""
    if _cache["d"] is None or time.time() - _cache["t"] > max_age:
        try:
            _cache["d"] = evaluate()
        except Exception as e:
            _cache["d"] = {"error": f"Sunset outlook failed: {type(e).__name__}"}
        _cache["t"] = time.time()
    return _cache["d"]


def _compass(az):
    return ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")[round(az / 45) % 8]


def clock(t):
    return dt.datetime.fromtimestamp(t, LOCAL_TZ).strftime("%-I:%M %p")


def check(push, now=None):
    """Called every ~10 minutes. Logs each look in the 3 hours before sunset and pushes once a day when
    the outlook first reaches Good with 20 to 150 minutes to go."""
    now = now or time.time()
    d = evaluate(now)
    _cache.update(t=time.time(), d=d)
    if d.get("error") or not (0 <= d["minutes_to"] <= 180):
        return d
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"t": round(now), **{k: d[k] for k in ("sunset", "score", "high", "low_near", "thick", "scan")}}) + "\n")
    except OSError:
        pass
    if d["score"] >= 55 and 20 <= d["minutes_to"] <= 150:
        push("GOES: Sunset glow possible tonight" if d["score"] < 75 else "GOES: Good sunset glow tonight",
             f"Sunset {clock(d['sunset'])}, toward the {_compass(d['azimuth'])} ({d['azimuth']}°).Outlook {d['label']} ({d['score']}/100): {d['why']}. "
             f"Best light {clock(d['best_from'])} to {clock(d['best_to'])}. A satellite-only guess, so treat it as a hint.")
    return d
