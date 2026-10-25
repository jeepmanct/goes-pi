"""SKYWARN / spotter view for Connecticut, built from NWS bulletins already received over the satellite.

- spotter statements from the Hazardous Weather Outlooks of the offices that cover Connecticut
- active warnings and watches that include any Connecticut zone (VTEC codes, per bulletin segment)
- Local Storm Reports within a radius of Stratford
- the part of the SPC convective outlooks that mentions Connecticut or New England
- tropical outlook, an active storm (storm.json) and coastal alerts for Stratford
Read-only. Shares the product index with the panel through sys.modules["goes_products"].
"""
import datetime as dt
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path

HOME = (40.0000, -75.0000)        # Stratford, CT
OFFICES = ["OKX", "BOX", "ALY"]   # weather offices covering Connecticut
RADIUS_KM = 200                   # local storm reports within this distance
CT_ZONES = {"CTZ001": "Litchfield", "CTZ002": "Hartford", "CTZ003": "Tolland", "CTZ004": "Windham",
            "CTZ005": "Northern Fairfield", "CTZ006": "Northern New Haven", "CTZ007": "Northern Middlesex",
            "CTZ008": "Northern New London", "CTZ009": "Southern Fairfield", "CTZ010": "Southern New Haven",
            "CTZ011": "Southern Middlesex", "CTZ012": "Southern New London"}
STRATFORD_ZONES = {"CTZ009", "ANZ335"}
ALERT_CATS = {"TOR", "SVR", "SVS", "FFW", "FFS", "FFA", "FLW", "FLS", "WSW", "NPW", "RFW", "CFW", "MWW", "SMW", "HLS", "WCN"}
SKY_PHEN = {"TO", "SV", "FF", "FA", "FL", "HU", "TR", "SS", "CF", "EW", "SM", "SQ", "BZ", "WS", "IS", "HW", "LE", "DS"}
COASTAL_PHEN = {"HU", "TR", "SS", "CF", "SU", "RP", "SC", "GL", "SE", "SR", "HF", "SM", "MA"}
UTC_OFFSET = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8, "AKDT": -8, "AKST": -9,
              "HST": -10, "ADT": -3, "AST": -4}
LEVELS = {"none": 0, "unknown": 1, "possible": 2, "requested": 3}
_cache = {}


def _p():
    m = sys.modules.get("goes_products")
    if m is None:
        spec = importlib.util.spec_from_file_location("goes_products", Path(__file__).resolve().parent / "products.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["goes_products"] = m
        spec.loader.exec_module(m)
    return m


def _rows(cat, hours, office=None):
    p = _p()
    cut = time.time() - hours * 3600
    out = []
    for r in p._rows():
        if r["t"] < cut:
            break
        if r["cat"] == cat and (office is None or r["office"] == office):
            out.append(r)
    return out


# ---- spotter statements -----------------------------------------------------------------------------------

def _section(text, name):
    """Text of a '.NAME...' section of a Hazardous Weather Outlook, up to the next section or '&&'."""
    m = re.search(rf"^\.{name}[^\n]*\n(.*?)(?=^\.[A-Z]|^&&|\Z)", text, re.M | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def spotter_level(statement):
    s = statement.lower()
    if not s:
        return "unknown"
    if "not expected" in s or "no spotter activation" in s:
        return "none"
    if re.search(r"(activation|spotters?)[^.]*\b(is|are|will be) (requested|needed|encouraged)|please activate|activation (is )?requested", s):
        return "requested"
    if re.search(r"may be needed|might be needed|could be needed|possible|likely|consider|encouraged|be prepared", s):
        return "possible"
    return "unknown"


def spotter_status():
    out = []
    for office in OFFICES:
        rows = _rows("HWO", 36, office)
        if not rows:
            continue
        r = rows[0]
        try:
            text = _p()._read(r["name"])
        except OSError:
            continue
        st = _section(text, "SPOTTER INFORMATION STATEMENT")
        out.append({"office": office, "t": r["t"], "level": spotter_level(st), "statement": st,
                    "day1": _section(text, "DAY ONE")[:900]})
    return out


# ---- warnings and watches that include Connecticut ----------------------------------------------------------

def _vtec_end(s):
    return _p()._vtec_time(s)


_vtec_cache = {}      # product name -> parsed VTEC lines (a bulletin never changes once received)


def _vtec_events(name):
    hit = _vtec_cache.get(name)
    if hit is None:
        p = _p()
        hit = []
        try:
            text = p._read(name)
        except OSError:
            return hit
        for seg in text.split("$$"):
            zones = {z for z in p.ugc_zones(seg) if z.startswith("CT") or z in STRATFORD_ZONES}
            if zones:
                for m in p.VTEC_RE.finditer(seg):
                    _st, action, office, phen, sig, etn, _b, end = m.groups()
                    hit.append((zones, action, office, phen, sig, etn, end))
        _vtec_cache[name] = hit
    return hit


def ct_alerts(now=None):
    """Warnings, watches, advisories in force for any Connecticut zone, from the last 72 h of bulletins."""
    p = _p()
    now = now or time.time()
    cut = now - 72 * 3600
    events = {}
    for r in sorted(p._rows(), key=lambda r: r["t"]):
        if r["t"] < cut or r["cat"] not in ALERT_CATS:
            continue
        for zones, action, office, phen, sig, etn, end in _vtec_events(r["name"]):
            key = (office, phen, sig, etn)
            if action in ("CAN", "EXP"):
                events.pop(key, None)
                continue
            prev = events.get(key, {"zones": set()})
            events[key] = {"office": office[1:], "phen": phen, "sig": sig, "etn": etn,
                           "name": f"{p.PHEN.get(phen, phen)} {p.SIG.get(sig, sig)}", "ends": _vtec_end(end), "t": r["t"],
                           "zones": (prev["zones"] | zones) if action in ("CON", "EXT", "EXA", "EXB", "COR", "ROU") else zones,
                           "file": r["name"], "action": action}
    for gone in set(_vtec_cache) - {r["name"] for r in p._rows()}:
        _vtec_cache.pop(gone, None)
    out = []
    for key, e in events.items():
        if e["ends"] is not None and e["ends"] <= now:
            continue
        ct = sorted(z for z in e["zones"] if z.startswith("CT"))
        out.append({"key": "/".join(key), "name": e["name"], "phen": e["phen"], "level": e["sig"], "office": e["office"],
                    "ends": e["ends"], "t": e["t"], "stratford": bool(e["zones"] & STRATFORD_ZONES),
                    "counties": [CT_ZONES.get(z, z) for z in ct], "zones": ct,
                    "sky": e["phen"] in SKY_PHEN})
    out.sort(key=lambda a: (not a["stratford"], "WAYS".index(a["level"]), -a["t"]))
    return out


# ---- local storm reports ---------------------------------------------------------------------------------------

LSR1 = re.compile(r"^(\d{3,4} [AP]M)\s{2,}(.+?)\s{2,}(.+?)\s+(\d+\.\d+)([NS])\s+(\d+\.\d+)([EW])\s*$")
LSR2 = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*?)\s+([A-Z]{2})\s{2,}(\S.*?)\s*$")


def km_between(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def parse_lsr(text, issued_ts):
    """List of report dicts from one Local Storm Report bulletin."""
    lines = text.replace("\r", "").splitlines()
    tz = next((UTC_OFFSET.get(m.group(1)) for ln in lines[:12] for m in [re.search(r"\b([A-Z]{3,4}) \w{3} \w{3} \d+ \d{4}", ln)] if m), None)
    if tz is None:
        tz = -4
    out, i = [], 0
    while i < len(lines) - 1:
        a = LSR1.match(lines[i])
        b = LSR2.match(lines[i + 1]) if a else None
        if not (a and b):
            i += 1
            continue
        hhmm, ap = a.group(1).split()
        hh, mm = int(hhmm[:-2]), int(hhmm[-2:])
        hh = hh % 12 + (12 if ap == "PM" else 0)
        try:
            d = dt.datetime.strptime(b.group(1), "%m/%d/%Y")
            t = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=dt.timezone.utc).timestamp() - tz * 3600
        except ValueError:
            t = issued_ts
        lat = float(a.group(4)) * (1 if a.group(5) == "N" else -1)
        lon = float(a.group(6)) * (1 if a.group(7) == "E" else -1)
        parts = re.split(r"\s{2,}", b.group(2).strip())
        mag = parts[0] if len(parts) > 1 else ""
        county = parts[-1] if parts and parts[-1] else ""
        i += 2
        rem = []
        while i < len(lines) and not LSR1.match(lines[i]) and not lines[i].startswith("$$"):
            if lines[i].strip():
                rem.append(lines[i].strip())
            i += 1
        out.append({"t": t, "event": a.group(2).strip(), "mag": mag, "where": a.group(3).strip(), "county": county,
                    "state": b.group(3), "source": b.group(4), "lat": lat, "lon": lon, "remarks": " ".join(rem)[:400]})
    return out


def storm_reports(hours=48):
    out = []
    for r in _rows("LSR", hours):
        try:
            text = _p()._read(r["name"])
        except OSError:
            continue
        for rep in parse_lsr(text, r["t"]):
            km = km_between(HOME, (rep["lat"], rep["lon"]))
            if km <= RADIUS_KM:
                rep["miles"] = round(km * 0.621)
                rep["office"] = r["office"]
                out.append(rep)
    seen, uniq = set(), []
    for rep in sorted(out, key=lambda x: -x["t"]):          # rebroadcasts repeat reports
        k = (round(rep["t"]), rep["event"], rep["where"], rep["mag"])
        if k not in seen:
            seen.add(k)
            uniq.append(rep)
    return uniq[:60]


# ---- SPC convective outlook, for Connecticut --------------------------------------------------------------------

REGION = re.compile(r"\b(CONNECTICUT|NEW ENGLAND|LONG ISLAND|MID[- ]ATLANTIC|NORTHEAST(?:ERN)? (?:U\.?S\.?|STATES|CORRIDOR|COAST)|"
                    r"(?:SOUTHERN|COASTAL) NEW ENGLAND)\b|\.\.\.[A-Z/ ]*\bCT\b[A-Z/ ]*\.\.\.", re.I)
RISK = re.compile(r"\.\.\.(THERE IS|THERE ARE)[^.]*?RISK[^.]*?\.\.\.\.?|\.\.\.NO SEVERE THUNDERSTORM AREAS FORECAST\.\.\.|"
                  r"\.\.\.GENERAL THUNDERSTORMS?[^.]*\.\.\.", re.I | re.S)


def outlook():
    out = []
    for day in (1, 2, 3):
        rows = [r for r in _rows("SWO", 36) if r["site"].startswith(f"DY{day}")]
        if not rows:
            continue
        r = rows[0]
        try:
            text = _p()._read(r["name"])
        except OSError:
            continue
        body = text.replace("\r", "")
        head = " ".join(m.group(0).strip(". ").replace("\n", " ") for m in RISK.finditer(body))[:500]
        head = re.sub(r"\s+", " ", head)
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", body)]
        rel = [p for p in paras if REGION.search(p)][:3]
        valid = re.search(r"Valid (\d{6}Z - \d{6}Z)", body)
        out.append({"day": day, "t": r["t"], "valid": valid.group(1) if valid else "", "headline": head,
                    "relevant": [x[:700] for x in rel]})
    return out


# ---- tropical and coastal ---------------------------------------------------------------------------------------

def tropical():
    p = _p()
    outlook_txt, t = "", 0
    cut = time.time() - 48 * 3600
    for r in p._rows():                       # the Atlantic outlook is bulletin heading ABNT20 (product code HAWT20US)
        if r["t"] < cut:
            break
        if not r["head"].startswith("ABNT20"):
            continue
        try:
            text = p._read(r["name"]).replace("\r", "")
        except OSError:
            continue
        m = re.search(r"For the North Atlantic.*?(?=\n\$\$)", text, re.S)
        if m:
            outlook_txt, t = re.sub(r"\s+", " ", m.group(0)).strip(), r["t"]
            break
    storm = None
    try:
        sf = Path.home() / "goes" / "storm.json"
        if sf.exists() and time.time() - sf.stat().st_mtime < 36 * 3600:
            storm = json.loads(sf.read_text())
    except (OSError, ValueError):
        storm = None
    coastal = [a for a in p.active_alerts(zones=list(STRATFORD_ZONES)) if a.get("phen") in COASTAL_PHEN]
    return {"outlook": outlook_txt, "outlook_t": t, "storm": storm, "coastal": coastal}


# ---- everything for the panel ------------------------------------------------------------------------------------

def summary():
    hit = _cache.get("s")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    spotters = spotter_status()
    worst = max((s["level"] for s in spotters), key=lambda l: LEVELS[l], default="unknown")
    d = {"spotter": {"level": worst, "offices": spotters}, "alerts": ct_alerts(), "reports": storm_reports(),
         "outlook": outlook(), "tropical": tropical(), "radius_miles": round(RADIUS_KM * 0.621)}
    _cache["s"] = (time.time(), d)
    return d
