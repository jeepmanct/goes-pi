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
    return {
        "station": "Bridgeport, CT", "fetched": time.time(),
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
