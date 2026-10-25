"""Browse the NWS text products that arrive over EMWIN, and work out which warnings are still in force.

EMWIN file names look like  A_FXUS64KEPZ241735_C_KWIN_20261024173559_130176-2-AFDEPZTX.TXT :
heading FXUS64, office KEPZ, issued day 24 17:35 UTC, received 2026-10-24 17:35:59 UTC, product AFD for site EPZ in TX.
Read-only: nothing here writes to the data directory.
"""
import datetime as dt
import os
import re
import threading
import time
import tomllib
from pathlib import Path

EMWIN = Path.home() / "goes" / "data" / "emwin"
MAIL_CONF = Path.home() / "goes" / "mail.conf"
NAME_RE = re.compile(r"^[AZ]_([A-Z0-9]{6})([A-Z]{4})(\d{6})_C_KWIN_(\d{14})_\d+-\d-([A-Z0-9]{4,8})\.TXT$")
VTEC_RE = re.compile(r"/([OTEX])\.(NEW|CON|EXA|EXB|EXT|UPG|CAN|EXP|COR|ROU)\.(K[A-Z]{3})\.([A-Z]{2})\.([WAYS])\."
                     r"(\d{4})\.(\d{6}T\d{4}Z)-(\d{6}T\d{4}Z)/")
CATEGORIES = {
    "AFD": "Forecast discussion", "ZFP": "Zone forecast", "PFM": "Point forecast", "AFM": "Area forecast matrix",
    "NOW": "Short-term forecast", "HWO": "Hazardous weather outlook", "SPS": "Special weather statement",
    "TOR": "Tornado warning", "SVR": "Severe thunderstorm warning", "SVS": "Severe weather statement",
    "FFW": "Flash flood warning", "FFS": "Flash flood statement", "FFA": "Flood watch", "FLW": "Flood warning",
    "FLS": "Flood statement", "WSW": "Winter storm warning/watch", "NPW": "Non-precip warning/advisory",
    "RFW": "Red flag warning", "CFW": "Coastal flood warning", "MWW": "Marine warning", "SMW": "Special marine warning",
    "CWF": "Coastal waters forecast", "OFF": "Offshore forecast", "HSF": "High seas forecast", "NSH": "Nearshore forecast",
    "GLF": "Great Lakes forecast", "SRF": "Surf zone forecast", "TAF": "Airport forecast (TAF)", "FWF": "Fire weather",
    "RWR": "Regional weather roundup", "OBS": "Observations", "SFT": "State forecast table", "CLI": "Daily climate",
    "CF6": "Monthly climate", "RVA": "River summary", "RVF": "River forecast", "RVM": "River miscellaneous",
    "RRM": "Misc. hydrology", "HLS": "Hurricane local statement", "TCM": "Tropical cyclone advisory",
    "TCD": "Tropical cyclone discussion", "TCP": "Tropical cyclone public advisory", "TWO": "Tropical outlook",
    "MIS": "Misc. data", "SAH": "Hourly surface data", "REC": "Recreation forecast", "PNS": "Public information",
    "MWS": "Marine weather statement", "ESF": "Hydrologic outlook", "OPU": "Observation update",
    "TID": "Tide data", "TST": "Test message", "AWW": "Airport weather warning", "WCN": "Watch county notification",
    "SWO": "Convective outlook", "PTS": "Convective outlook points", "MCD": "Mesoscale discussion",
    "RTP": "Regional temperature/precip", "SPC": "Storm Prediction Center",
}
WARNING_CATS = {"TOR", "SVR", "SVS", "FFW", "FFS", "FFA", "FLW", "FLS", "WSW", "NPW", "RFW", "CFW", "MWW", "SMW",
                "SPS", "HLS", "AWW", "WCN", "MCD", "TCM", "TCP", "TCD", "TWO", "MWS"}
VTEC_CATS = {"TOR", "SVR", "SVS", "FFW", "FFS", "FFA", "FLW", "FLS", "WSW", "NPW", "RFW", "CFW", "MWW", "SMW", "HLS"}
PHEN = {"TO": "Tornado", "SV": "Severe Thunderstorm", "FF": "Flash Flood", "FA": "Areal Flood", "FL": "Flood",
        "SM": "Special Marine", "EW": "Extreme Wind", "WS": "Winter Storm", "BZ": "Blizzard", "IS": "Ice Storm",
        "HU": "Hurricane", "TR": "Tropical Storm", "SS": "Storm Surge", "HW": "High Wind", "CF": "Coastal Flood",
        "SQ": "Snow Squall", "LE": "Lake Effect Snow", "DS": "Dust Storm", "EH": "Excessive Heat", "HT": "Heat",
        "WW": "Winter Weather", "WC": "Wind Chill", "FZ": "Freeze", "FR": "Frost", "GL": "Gale", "SE": "Hazardous Seas",
        "SC": "Small Craft", "SR": "Storm", "TS": "Tsunami", "FW": "Red Flag", "FG": "Dense Fog", "SU": "High Surf",
        "RP": "Rip Current", "LO": "Low Water", "MA": "Special Marine", "BW": "Brisk Wind", "WI": "Wind", "LW": "Lake Wind"}
SIG = {"W": "Warning", "A": "Watch", "Y": "Advisory", "S": "Statement"}
ACTIVE_HOURS = 72

_lock = threading.Lock()
_index = {"t": 0.0, "rows": []}


def _conf():
    try:
        return tomllib.loads(MAIL_CONF.read_text())
    except (OSError, ValueError):
        return {}


def my_offices():
    c = _conf()
    offs = list(c.get("afd_offices", [])) + list(c.get("alerts", {}).get("wx_offices", ["OKX"]))
    out = []
    for o in offs:
        o = str(o).upper()[-3:]
        if o not in out:
            out.append(o)
    return out


GR_RE = re.compile(r"^[AZ]_[A-Z0-9]{6}[A-Z]{4}(\d{6})_C_KWIN_(\d{14})_\d+-\d-([A-Z0-9]{4,10})\.(GIF|PNG|JPG)$")
_gseen = {}     # graphics: file name -> {"name", "t", "pil"} or None
CHARTS = [      # (group, [(PIL prefix regex, label)])
    ("Radar", [("RADNTHES", "Northeast"), ("RADREFUS", "US reflectivity"), ("RADSTHES", "Southeast"),
               ("RADGRTLK", "Great Lakes"), ("RADSTHPL", "Southern Plains"), ("RADSMSVY", "Lower Mississippi Valley"),
               ("RADUMSVY", "Upper Mississippi Valley"), ("RADRCKNT", "Northern Rockies"), ("RADRCKST", "Southern Rockies"),
               ("RADPACNW", "Pacific Northwest"), ("RADPACSW", "Pacific Southwest"), ("RADALLAK", "Alaska"),
               ("RADALLHI", "Hawaii"), ("RADALLPR", "Puerto Rico"), ("RADALLGU", "Guam")]),
    ("Warnings and forecasts", [("IMGWWAUS", "Watches and warnings"), ("USHZTHRT", "Hazard threats"),
                                ("MODQP", "Precipitation forecast"), ("MODDY", "Forecast maps by day")]),
    ("Analysis", [("CSA", "Surface analysis"), ("IMGFNT", "Fronts")]),
    ("Satellite", [("G10CIR", "GOES infrared, US")]),
    ("Tropics", [("AL\\d", "Atlantic outlook"), ("EP\\d", "East Pacific outlook")]),
]
_seen = {}      # file name -> parsed row (or None if it does not look like a product); only new names are parsed


def issued_ts(ddhhmm, recv):
    """Issue time (UTC epoch) from the day/hour/minute in the WMO heading and the arrival time. EMWIN rebroadcasts
    old products, so arrival time alone says nothing about how current a product is."""
    day, hh, mm = int(ddhhmm[:2]), int(ddhhmm[2:4]), int(ddhhmm[4:])
    r = dt.datetime.fromtimestamp(recv, dt.timezone.utc)
    for back in (0, 1, 2):
        y, mo = r.year, r.month - back
        while mo < 1:
            mo, y = mo + 12, y - 1
        try:
            c = dt.datetime(y, mo, day, hh, mm, tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
        if c <= recv + 3600:
            return c
    return recv


def _rows():
    """Every product on disk as dicts, newest first. Re-listed at most once a minute; only new names are parsed."""
    with _lock:
        if time.time() - _index["t"] < 60:
            return _index["rows"]
        try:
            names = {e.name for e in os.scandir(EMWIN)}
        except OSError:
            names = set()
        for gone in set(_seen) - names:
            del _seen[gone]
        for gone in set(_gseen) - names:
            del _gseen[gone]
        for n in names - set(_seen) - set(_gseen):
            m = NAME_RE.match(n)
            row = None
            g = None if m else GR_RE.match(n)
            if g:
                try:
                    recv = dt.datetime.strptime(g.group(2), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
                    _gseen[n] = {"name": n, "pil": g.group(3), "t": issued_ts(g.group(1), recv)}
                except ValueError:
                    _gseen[n] = None
                continue
            if m:
                head, office, issued, recv, pil = m.groups()
                try:
                    t = issued_ts(issued, dt.datetime.strptime(recv, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp())
                    row = {"name": n, "t": t, "office": office[1:], "cat": pil[:3], "site": pil[3:], "head": head}
                except ValueError:
                    pass
            _seen[n] = row
        rows = sorted((r for r in _seen.values() if r), key=lambda r: -r["t"])
        _index["t"], _index["rows"] = time.time(), rows
        return rows


def _graphics(pattern):
    _rows()
    rx = re.compile(pattern)
    return sorted((g for g in list(_gseen.values()) if g and rx.match(g["pil"])), key=lambda g: g["t"])


def charts():
    """Chart groups with the products that have arrived, and the age of the newest one."""
    out = []
    for group, items in CHARTS:
        row = []
        for pat, label in items:
            gs = _graphics(pat)
            if gs:
                row.append({"code": pat, "label": label, "t": gs[-1]["t"], "name": gs[-1]["name"], "n": len(gs)})
        if row:
            out.append({"group": group, "items": row})
    return out


def chart_frames(pattern, n=12):
    """The newest n images of one chart product, oldest first (for a loop)."""
    allowed = {pat for _g, items in CHARTS for pat, _l in items}
    if pattern not in allowed:
        raise ValueError("unknown chart")
    out = {}
    for g in _graphics(pattern):          # oldest first; rebroadcast duplicates share a time, keep the last of each
        out[g["t"]] = {"name": g["name"], "t": g["t"]}
    return list(out.values())[-max(1, min(n, 40)):]


def summary(hours=48):
    """Counts per category and the list of offices, for the filter controls."""
    cut = time.time() - hours * 3600
    cats, offs = {}, {}
    for r in _rows():
        if r["t"] < cut:
            break
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
        offs[r["office"]] = offs.get(r["office"], 0) + 1
    return {"categories": [{"code": k, "label": CATEGORIES.get(k, k), "n": v}
                           for k, v in sorted(cats.items(), key=lambda kv: (-kv[1]))],
            "offices": sorted(offs), "mine": my_offices(), "warning_cats": sorted(WARNING_CATS)}


def _read(name):
    p = EMWIN / name
    return p.read_bytes().decode("utf-8", errors="replace").replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\x01", "")


def _title(text):
    for ln in text.splitlines():
        s = ln.strip()
        if (s and not s.startswith(("ZCZC", "NNNN", "WMO", "000")) and not re.fullmatch(r"[A-Z]{4}\d{2} [A-Z]{4} \d{6}.*", s)
                and not re.fullmatch(r"[A-Z0-9]{4,8}", s) and not re.fullmatch(r"[\d.\s]+", s)):
            return s[:100]
    return ""


def search(cat="", office="", q="", hours=48, limit=60, offset=0, warnings=False):
    """Matching products, newest first. cat/office may be comma lists; q is a case-insensitive text search."""
    cats = {c for c in cat.upper().split(",") if c}
    offs = {o[-3:] for o in office.upper().replace(" ", "").split(",") if o}
    cut = time.time() - hours * 3600
    needle = q.strip().lower()
    out, skipped = [], 0
    for r in _rows():
        if r["t"] < cut:
            break
        if cats and r["cat"] not in cats:
            continue
        if warnings and r["cat"] not in WARNING_CATS:
            continue
        if offs and r["office"] not in offs:
            continue
        text = None
        if needle:
            try:
                text = _read(r["name"])
            except OSError:
                continue
            if needle not in text.lower():
                continue
        if skipped < offset:
            skipped += 1
            continue
        if len(out) >= limit:
            return {"items": out, "more": True}
        if text is None:
            try:
                text = _read(r["name"])
            except OSError:
                continue
        out.append({"name": r["name"], "t": r["t"], "office": r["office"], "cat": r["cat"],
                    "label": CATEGORIES.get(r["cat"], r["cat"]), "site": r["site"], "title": _title(text)})
    return {"items": out, "more": False}


def product(name):
    if not NAME_RE.match(name):
        raise ValueError("bad product name")
    p = EMWIN / name
    if not p.is_file():
        raise FileNotFoundError(name)
    return {"name": name, "text": _read(name)}


def _vtec_time(s):
    if s.startswith("000000"):
        return None
    try:
        return dt.datetime.strptime(s, "%y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def active_alerts(offices=None, now=None):
    """Warnings, watches and advisories still in force, from VTEC codes in the last ACTIVE_HOURS hours of
    bulletins for the given offices (default: the ones in mail.conf). Cancelled/expired ones are dropped."""
    offices = {o.upper()[-3:] for o in (offices or my_offices())}
    now = now or time.time()
    cut = now - ACTIVE_HOURS * 3600
    events = {}
    for r in sorted(_rows(), key=lambda r: r["t"]):          # oldest first so later actions win
        if r["t"] < cut or r["cat"] not in VTEC_CATS or r["office"] not in offices:
            continue
        try:
            text = _read(r["name"])
        except OSError:
            continue
        for m in VTEC_RE.finditer(text):
            _status, action, office, phen, sig, etn, _begin, end = m.groups()
            key = (office, phen, sig, etn)
            if action in ("CAN", "EXP"):
                events.pop(key, None)
                continue
            events[key] = {"office": office[1:], "name": f"{PHEN.get(phen, phen)} {SIG.get(sig, sig)}",
                           "level": sig, "ends": _vtec_time(end), "file": r["name"], "t": r["t"]}
    out = [e for e in events.values() if e["ends"] is None or e["ends"] > now]
    out.sort(key=lambda e: ("WAYS".index(e["level"]), -e["t"]))
    return out
