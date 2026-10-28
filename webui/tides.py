"""Tides and water conditions for Bridgeport (NOAA CO-OPS 8467150, the nearest tide station to Stratford)."""
import datetime as dt
import json
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

STATION = "8467150"
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
ET = ZoneInfo("America/New_York")
_cache = {"t": 0, "data": None}
_flood = {"t": 0, "levels": None}
# Level (ft above MLLW) where the NWS began issuing Coastal Flood Statements at Bridgeport: the 2026-10-28 statement
# came when the tide peaked at 8.76 ft. NOAA's own minor/moderate/major thresholds are far higher (10.9/12.4/13.4).
WATCH_FT = 8.5
_lock = threading.Lock()


def _get(product, **kw):
    q = {"product": product, "application": "goes-pi", "station": STATION, "datum": "MLLW",
         "time_zone": "gmt", "units": "english", "format": "json", **kw}
    with urllib.request.urlopen(API + "?" + urllib.parse.urlencode(q), timeout=15) as r:
        d = json.load(r)
    if "error" in d:
        raise ValueError(d["error"].get("message", "CO-OPS error"))
    return d


def _ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _latest(product):
    try:
        row = _get(product, date="latest")["data"][0]
    except Exception:
        return None
    return {**row, "t": _ts(row["t"])}


def flood_levels():
    """NOAA/NWS flood thresholds for the station, cached a day. {} if unavailable."""
    if _flood["levels"] is not None and time.time() - _flood["t"] < 86400:
        return _flood["levels"]
    try:
        url = f"https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{STATION}/floodlevels.json"
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.load(r)
        lv = {k: d[k] for k in ("nws_minor", "nws_moderate", "nws_major") if isinstance(d.get(k), (int, float))}
    except Exception:
        lv = _flood["levels"] or {}
    _flood["levels"], _flood["t"] = lv, time.time()
    return lv


def classify(ft, levels):
    """Flood category for a water level in ft MLLW."""
    for key, name in (("nws_major", "major"), ("nws_moderate", "moderate"), ("nws_minor", "minor")):
        if key in levels and ft >= levels[key]:
            return name
    return "statement" if ft >= WATCH_FT else None


def surge_and_highs(pred, obs, hilo, now=None):
    """Current surge (measured minus predicted) and each upcoming high tide with an estimated peak.
    The estimate adds the current surge, fading by 15% per 12 h; it is a rough guide, not a model."""
    now = now or time.time()
    if not obs or not pred:
        return None, []
    pt, pv = min(pred, key=lambda p: abs(p[0] - obs[-1][0]))
    surge = round(obs[-1][1] - pv, 2) if abs(pt - obs[-1][0]) < 1200 and now - obs[-1][0] < 3 * 3600 else None
    lv = flood_levels()
    out = []
    for h in hilo:
        if h["type"] != "H" or h["t"] < now - 1800 or h["t"] > now + 36 * 3600:
            continue
        est = h["v"] + (surge or 0) * 0.85 ** max(0, (h["t"] - now) / 43200)
        out.append({"t": h["t"], "predicted": h["v"], "estimated": round(est, 2), "category": classify(est, lv)})
    return surge, out


def fetch():
    now = dt.datetime.now(dt.timezone.utc)
    day = lambda d: d.strftime("%Y%m%d %H:%M")
    start, end = now - dt.timedelta(hours=24), now + dt.timedelta(hours=48)
    pred = _get("predictions", begin_date=day(start), end_date=day(end), interval="6")["predictions"]
    hilo = _get("predictions", begin_date=day(now - dt.timedelta(hours=6)), end_date=day(end), interval="hilo")["predictions"]
    try:
        obs = _get("water_level", begin_date=day(start), end_date=day(now))["data"]
    except Exception:
        obs = []
    level, temp, wind, pres = _latest("water_level"), _latest("water_temperature"), _latest("wind"), _latest("air_pressure")
    p_all = [[_ts(p["t"]), _num(p["v"])] for p in pred]
    o_all = [[_ts(o["t"]), _num(o["v"])] for o in obs if _num(o["v"]) is not None]
    h_all = [{"t": _ts(p["t"]), "v": _num(p["v"]), "type": p["type"]} for p in hilo]
    surge, highs = surge_and_highs(p_all, o_all, h_all)
    return {
        "station": "Bridgeport, CT", "fetched": time.time(),
        "surge_ft": surge, "highs": highs, "flood": {**flood_levels(), "watch": WATCH_FT},
        "predicted": [[_ts(p["t"]), _num(p["v"])] for p in pred[::2]],   # 12-minute points keep the payload small
        "observed": [[_ts(o["t"]), _num(o["v"])] for o in obs[::2] if _num(o["v"]) is not None],
        "hilo": [{"t": _ts(p["t"]), "v": _num(p["v"]), "type": p["type"]} for p in hilo],
        "level": {"t": level["t"], "ft": _num(level["v"])} if level else None,
        "water_temp_f": _num(temp["v"]) if temp else None,
        "wind": {"mph": _num(wind["s"]) * 1.150779, "gust_mph": (_num(wind.get("g")) or 0) * 1.150779 or None,
                 "dir": wind.get("dr"), "deg": _num(wind.get("d"))} if wind and _num(wind.get("s")) is not None else None,
        "pressure_hpa": _num(pres["v"]) if pres else None,
    }


def tides():
    """Cached for 5 minutes; serves the last good copy (marked stale) if CO-OPS is unreachable."""
    with _lock:
        if _cache["data"] and time.time() - _cache["t"] < 300:
            return _cache["data"]
        try:
            _cache["data"], _cache["t"] = fetch(), time.time()
        except Exception as e:
            if not _cache["data"]:
                return {"error": f"Tide data unavailable: {e}"}
            return {**_cache["data"], "stale": True}
        return _cache["data"]


def check(push, now=None):
    """Push once per high tide that is estimated to reach flood-statement level or higher in the next 24 h."""
    d = tides()
    if d.get("error") or d.get("stale"):
        return None
    now = now or time.time()
    for h in d.get("highs", []):
        if h["category"] and h["t"] < now + 24 * 3600:
            when = dt.datetime.fromtimestamp(h["t"], ET).strftime("%a %-I:%M %p")
            sg = d.get("surge_ft")
            body = (f"High tide {when} at Bridgeport: predicted {h['predicted']:.1f} ft, estimated peak {h['estimated']:.1f} ft"
                    + (f" (water is {sg:+.1f} ft vs. prediction now)" if sg is not None else "")
                    + f". Flood-statement level is {WATCH_FT:.1f} ft. Low-lying shore roads in Stratford can flood.")
            push("Coastal flooding possible: " + h["category"], body, key=f"tide-{int(h['t'] // 3600)}", every=12 * 3600,
                 priority="high" if h["category"] != "statement" else "default", tags="ocean")
    return d
