"""Local conditions for Stratford, CT from the EMWIN feed (no internet needed).

Current weather from the Bridgeport (KBDR) METAR, its airport forecast (TAF), the Southern Fairfield zone forecast,
the Long Island Sound West marine forecast, and space-weather bulletins. Read-only; everything is parsed from files
already on disk. Needs the products module (shared with the panel through sys.modules["goes_products"]).
"""
import datetime as dt
import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path

HOME = {"name": "Stratford, CT", "lat": 40.0000, "lon": -75.0000, "station": "KBDR", "zone": "CTZ009",
        "marine": "ANZ335", "office": "OKX"}
EMWIN = Path.home() / "goes" / "data" / "emwin"
WX_WORDS = {"RA": "rain", "SN": "snow", "DZ": "drizzle", "SG": "snow grains", "IC": "ice crystals", "PL": "ice pellets",
            "GR": "hail", "GS": "small hail", "UP": "precipitation", "BR": "mist", "FG": "fog", "FU": "smoke",
            "VA": "volcanic ash", "DU": "dust", "SA": "sand", "HZ": "haze", "SQ": "squalls", "FC": "funnel cloud",
            "SS": "sandstorm", "DS": "duststorm"}
PRECIP = {"RA", "SN", "DZ", "SG", "PL", "GR", "GS", "UP"}
SKY_WORDS = {"CLR": "clear", "SKC": "clear", "FEW": "few clouds", "SCT": "scattered clouds", "BKN": "broken clouds",
             "OVC": "overcast", "VV": "sky obscured"}
WX_TOKEN = re.compile(r"^(\+|-|VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+)$")


def _products():
    m = sys.modules.get("goes_products")
    if m is None:
        spec = importlib.util.spec_from_file_location("goes_products", Path(__file__).resolve().parent / "products.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["goes_products"] = m
        spec.loader.exec_module(m)
    return m


def _newest(cat, site_prefix="", n=1, hours=48):
    """Newest n product rows of a category (optionally for a site prefix), by issue time."""
    p = _products()
    cut = time.time() - hours * 3600
    out = []
    for r in p._rows():
        if r["t"] < cut:
            break
        if r["cat"] == cat and r["site"].startswith(site_prefix):
            out.append(r)
            if len(out) >= n:
                break
    return out


def _text(name):
    return _products()._read(name)


# ---- METAR -----------------------------------------------------------------------------------

def c_to_f(c):
    return round(c * 9 / 5 + 32)


def decode_metar(raw, issued=None):
    """Plain dict from a METAR line like 'KBDR 251752Z 19009KT 10SM CLR 16/06 A3051 RMK AO2 SLP331'."""
    out = {"raw": raw, "station": raw.split()[0], "wx": [], "sky": [], "precip": False}
    for tok in re.sub(r"\b(\d) (\d/\d+SM)", r"\1+\2", raw).split():
        if tok == "RMK":
            break
        m = re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT", tok)
        if m:
            out["wind_dir"] = None if m.group(1) == "VRB" else int(m.group(1))
            out["wind_kt"] = int(m.group(2))
            out["gust_kt"] = int(m.group(3)) if m.group(3) else None
            continue
        m = re.fullmatch(r"(?:(\d+)\+)?(\d+(?:/\d+)?)SM", tok)
        if m:
            try:
                out["vis_mi"] = eval_frac(tok[:-2].replace("+", " "))
            except (ValueError, ZeroDivisionError):
                pass
            continue
        if tok in ("P6SM",):
            out["vis_mi"] = 10.0
            continue
        m = re.fullmatch(r"(CLR|SKC|FEW|SCT|BKN|OVC|VV)(\d{3})?", tok)
        if m:
            out["sky"].append({"cover": SKY_WORDS[m.group(1)], "code": m.group(1),
                               "ft": int(m.group(2)) * 100 if m.group(2) else None})
            continue
        m = re.fullmatch(r"(M?\d{2})/(M?\d{2})?", tok)
        if m:
            out["temp_f"] = c_to_f(int(m.group(1).replace("M", "-")))
            if m.group(2):
                out["dew_f"] = c_to_f(int(m.group(2).replace("M", "-")))
            continue
        m = re.fullmatch(r"A(\d{4})", tok)
        if m:
            out["pressure_inhg"] = int(m.group(1)) / 100
            continue
        m = WX_TOKEN.match(tok)
        if m:
            inten, desc, kinds = m.groups()
            words = [WX_WORDS.get(kinds[i:i + 2], kinds[i:i + 2]) for i in range(0, len(kinds), 2)]
            text = " and ".join(words)
            if desc == "SH":
                text = "showers of " + text
            elif desc == "TS":
                text = "thunderstorm with " + text
            elif desc == "FZ":
                text = "freezing " + text
            prefix = {"-": "light ", "+": "heavy ", "VC": "nearby "}.get(inten or "", "")
            out["wx"].append(prefix + text)
            if any(kinds[i:i + 2] in PRECIP for i in range(0, len(kinds), 2)):
                out["precip"] = True
    out["summary"] = metar_summary(out)
    return out


def eval_frac(s):
    """'10' -> 10.0, '1/2' -> 0.5, '1 1/2' -> 1.5"""
    total = 0.0
    for part in s.split():
        if "/" in part:
            a, b = part.split("/")
            total += int(a) / int(b)
        else:
            total += float(part)
    return total


def metar_summary(m):
    bits = []
    if m["wx"]:
        bits.append(", ".join(m["wx"]).capitalize())
    elif m["sky"]:
        bits.append(m["sky"][-1]["cover"].capitalize() if m["sky"][-1]["code"] not in ("CLR", "SKC") else "Clear")
    if "temp_f" in m:
        bits.append(f"{m['temp_f']}°F" + (f", dew point {m['dew_f']}°" if "dew_f" in m else ""))
    if m.get("wind_kt") is not None:
        if m["wind_kt"] == 0:
            bits.append("calm")
        else:
            d = "variable" if m.get("wind_dir") is None else f"{m['wind_dir']:03d}°"
            bits.append(f"wind {d} at {m['wind_kt']} kt" + (f", gusts {m['gust_kt']}" if m.get("gust_kt") else ""))
    if "vis_mi" in m and m["vis_mi"] < 10:
        bits.append(f"visibility {m['vis_mi']:g} mi")
    return ". ".join(bits[:1]) + (". " if len(bits) > 1 else "") + ", ".join(bits[1:]) if bits else ""


METAR_LOG = Path.home() / "goes" / "metar-kbdr.jsonl"       # {"t": issue time, "raw": report}, kept 14 days
_mlog = {"loaded": False, "data": {}, "files": set()}
_mlock = threading.Lock()


def update_metars(hours=12):
    """Pull any not-yet-seen reports for HOME['station'] out of the hourly national bulletins into the rolling log."""
    p = _products()
    st = HOME["station"]
    pat = re.compile(rf"(?:^|\s)((?:METAR |SPECI )?{st} (\d{{6}})Z[^=]*)=")
    with _mlock:
        if not _mlog["loaded"]:
            try:
                for ln in METAR_LOG.read_text().splitlines():
                    j = json.loads(ln)
                    _mlog["data"][float(j["t"])] = j["raw"]
            except (OSError, ValueError, KeyError):
                pass
            _mlog["loaded"] = True
        new = []
        for r in _newest("SAH", "", n=1_000_000, hours=hours):
            if r["name"] in _mlog["files"]:
                continue
            _mlog["files"].add(r["name"])
            try:
                text = re.sub(r"\s+", " ", _text(r["name"]))
            except OSError:
                continue
            for m in pat.finditer(text):
                t = p.issued_ts(m.group(2), r["t"] + 3600)
                if t not in _mlog["data"]:
                    raw = m.group(1).replace("METAR ", "").replace("SPECI ", "").rstrip(" $")
                    _mlog["data"][t] = raw
                    new.append((t, raw))
        cut = time.time() - 14 * 86400
        for t in [t for t in _mlog["data"] if t < cut]:
            del _mlog["data"][t]
        if new:
            try:
                with METAR_LOG.open("a") as f:
                    for t, raw in sorted(new):
                        f.write(json.dumps({"t": t, "raw": raw}) + "\n")
            except OSError:
                pass


def metars(hours=12):
    """Decoded Stratford-area (KBDR) METARs from the last `hours` hours, newest first."""
    update_metars()
    cut = time.time() - hours * 3600
    with _mlock:
        items = sorted(((t, raw) for t, raw in _mlog["data"].items() if t >= cut), reverse=True)
    out = []
    for t, raw in items:
        d = decode_metar(raw)
        d["t"] = t
        out.append(d)
    return out


def metar():
    """Newest decoded METAR, or None."""
    got = metars(hours=12)
    return got[0] if got else None


# ---- TAF, zone and marine forecasts ------------------------------------------------------------

def taf(station=None):
    station = station or HOME["station"]
    for r in _newest("TAF", "", n=150, hours=30):
        try:
            text = _text(r["name"])
        except OSError:
            continue
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if re.match(rf"^(TAF )?(AMD |COR )?{station}\b", ln.strip()):
                block = []
                for ln2 in lines[i:i + 14]:
                    block.append(ln2.rstrip())
                    if ln2.rstrip().endswith("="):
                        break
                body = "\n".join(block).rstrip("=").strip()
                return {"t": r["t"], "text": body}
    return None


def _periods(lines):
    """[(name, text)] from '.TONIGHT...Clear. Lows around 40.' style forecast lines, plus headline lines."""
    heads, periods = [], []
    for ln in lines:
        s = ln.rstrip()
        m = re.match(r"^\.([A-Z][A-Z0-9 /]*?)\.\.\.(.*)$", s)
        if m:
            periods.append([m.group(1).title(), m.group(2).strip()])
        elif periods and s.strip() and not s.startswith("$$"):
            periods[-1][1] += " " + s.strip()
        elif not periods and s.startswith("..."):
            heads.append(s.strip(".").strip())
    return heads, [(n, re.sub(r"\s+", " ", t).strip()) for n, t in periods]


def zone_product(cat, site_prefix, zone):
    """Newest forecast block for one zone from products like ZFPOKXNY / CWFOKXNY."""
    p = _products()
    for r in _newest(cat, site_prefix, n=8, hours=36):
        try:
            text = _text(r["name"])
        except OSError:
            continue
        for block in text.split("$$"):
            if zone not in p.ugc_zones(block):
                continue
            lines = block.splitlines()
            start = next((i for i, ln in enumerate(lines) if p.UGC_RE.match(ln)), 0)
            while start < len(lines) and not re.search(r"-\d{6}-$", lines[start].strip()):
                start += 1
            body = lines[start + 1:]
            name = " ".join(ln.strip().rstrip("-") for ln in body[:2] if ln.strip() and not re.search(r"\d{4}", ln))
            issued = next((ln.strip() for ln in body if re.search(r"\d{3,4} [AP]M ", ln)), "")
            heads, periods = _periods(body)
            if periods:
                return {"t": r["t"], "name": name.strip(" -"), "issued": issued, "headlines": heads, "periods": periods}
    return None


# ---- point forecast (PFM) and weather history ------------------------------------------------------------

SKY_PFM = {"CL": "Clear", "FW": "Few clouds", "SC": "Scattered", "B1": "Broken", "B2": "Broken", "BK": "Broken", "OV": "Overcast",
           "MC": "Mostly cloudy", "PC": "Partly cloudy"}
ET = {"EDT": -4, "EST": -5}


def _pfm_block(text, zone):
    """Lines of the point-forecast block whose UGC line names the zone."""
    p = _products()
    for block in text.split("$$"):
        if zone in p.ugc_zones(block):
            return block.replace("\r", "").splitlines()
    return None


def _row_tokens(lines, label, n):
    for ln in lines:
        if ln.startswith(label):
            toks = ln[len(label):].split()
            return (toks + [""] * n)[:n] if toks else None
    return None


def _slots(line, label, n, first=13, width=3):
    """{slot: token} for sparse fixed-width rows (PoP, QPF, Snow), where slot = column index."""
    out = {}
    for m in re.finditer(r"\S+", line[len(label):]):
        pos = len(label) + m.start()
        i = (pos - first) // width
        if 0 <= i < n:
            out[i] = m.group(0)
    return out


def point_forecast(zone=None, rows=None, keep_past=False):
    """3-hourly (then 6-hourly) forecast for the home point from the PFM bulletin: list of dicts with t, temp, dew,
    wind_dir, wind_mph, sky, pop, plus the point's name and issue time. None if no bulletin."""
    zone = zone or HOME["zone"]
    for r in (rows if rows is not None else _newest("PFM", HOME["office"], n=6, hours=30)):
        try:
            text = _text(r["name"])
        except OSError:
            continue
        lines = _pfm_block(text, zone)
        if not lines:
            continue
        name = next((ln.strip() for ln in lines if re.search(r"-\w.* [A-Z]{2}$", ln.strip())), "")
        hdr = next((ln for ln in lines if re.search(r"\d{3,4} [AP]M [EC][SD]T", ln)), "")
        tz = ET.get(re.search(r"\b([EC][SD]T)\b", hdr).group(1), -4) if hdr else -4
        points = []
        for sect in ("3hrly", "6hrly"):
            idx = next((i for i, ln in enumerate(lines) if ln.startswith("EDT " + sect) or ln.startswith("EST " + sect)), None)
            if idx is None:
                continue
            hours = [int(h) for h in lines[idx].split()[2:]]
            n = len(hours)
            date_line = lines[idx - 1]
            md = re.findall(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}))?", date_line)
            if not md:
                continue
            mo, da, yy = md[0]
            year = 2000 + int(yy) if yy else dt.datetime.now().year
            t0 = dt.datetime(year, int(mo), int(da), hours[0], tzinfo=dt.timezone.utc).timestamp() - tz * 3600
            step = 3 * 3600 if sect == "3hrly" else 6 * 3600
            block = lines[idx:]
            temp, dew = _row_tokens(block, "Temp", n), _row_tokens(block, "Dewpt", n)
            wd, ws = _row_tokens(block, "Wind dir", n) or _row_tokens(block, "PWind dir", n), _row_tokens(block, "Wind spd", n)
            sky = _row_tokens(block, "Clouds", n) or _row_tokens(block, "Avg clouds", n)
            pop_line = next((ln for ln in block if ln.startswith("PoP 12hr")), "")
            pops = _slots(pop_line, "PoP 12hr", n)
            for i in range(n):
                try:
                    tv = int(temp[i]) if temp else None
                except ValueError:
                    tv = None
                points.append({"t": t0 + i * step, "temp": tv,
                               "dew": int(dew[i]) if dew and dew[i].lstrip("-").isdigit() else None,
                               "wind_dir": wd[i] or None if wd else None,
                               "wind_mph": int(ws[i]) if ws and ws[i].isdigit() else None,
                               "sky": SKY_PFM.get(sky[i], sky[i]) or None if sky else None,
                               "pop": int(pops[i]) if i in pops and pops[i].isdigit() else None, "step_h": step // 3600})
        if points:
            if not keep_past:
                points = [pt for pt in points if pt["t"] >= time.time() - 3 * 3600]
            seen, uniq = set(), []
            for pt in sorted(points, key=lambda x: x["t"]):
                if pt["t"] not in seen:
                    seen.add(pt["t"])
                    uniq.append(pt)
            return {"name": name, "issued": hdr.strip(), "t": r["t"], "points": uniq}
    return None


def weather_history(hours=72):
    """Observation series from the rolling KBDR log, oldest first, with a 3-hour pressure tendency."""
    obs = metars(hours=hours)
    pts = [{"t": o["t"], "temp": o.get("temp_f"), "dew": o.get("dew_f"), "wind": o.get("wind_kt"), "gust": o.get("gust_kt"),
            "press": o.get("pressure_inhg")} for o in reversed(obs)]
    tend = None
    pr = [(p["t"], p["press"]) for p in pts if p["press"] is not None]
    if len(pr) >= 2:
        now_t, now_p = pr[-1]
        past = [x for x in pr if abs(x[0] - (now_t - 3 * 3600)) <= 50 * 60]
        if past:
            d = now_p - min(past, key=lambda x: abs(x[0] - (now_t - 3 * 3600)))[1]
            a = abs(d)
            word = "steady" if a < 0.02 else ("slowly " if a < 0.06 else "quickly " if a >= 0.10 else "") + ("rising" if d > 0 else "falling")
            if a >= 0.02 and a < 0.10 and a >= 0.06:
                word = ("rising" if d > 0 else "falling")
            tend = {"change_inhg": round(d, 2), "text": word.strip()}
    return {"points": pts, "tendency": tend}


# ---- space weather -------------------------------------------------------------------------------

def space():
    """Latest geomagnetic K-index alert (last 48 h) and the daily sunspot/flux numbers."""
    out = {"alert": None, "sunspot": None, "flux": None}
    for r in _newest("ALT", "K", n=1, hours=48):
        try:
            text = _text(r["name"])
        except OSError:
            continue
        m = re.search(r"(ALERT|WARNING|WATCH|SUMMARY): (.+)", text)
        k = re.match(r"K0?(\d)", r["site"])
        impact = re.search(r"Aurora - (.+)", text)
        out["alert"] = {"t": r["t"], "k": int(k.group(1)) if k else None, "text": m.group(2).strip() if m else "",
                        "active": "Active Warning: YES" in text, "aurora": impact.group(1).strip() if impact else ""}
    for r in _newest("DAY", "IND", n=1, hours=72):
        try:
            text = _text(r["name"])
        except OSError:
            continue
        m = re.search(r"Stanford Solar\s*\n#.*\n\s*(\d+)\s+(\d+)\s+(\d+)", text)
        if m:
            out["sunspot"], out["flux"] = int(m.group(1)), int(m.group(2))
    return out


# ---- everything for the panel ---------------------------------------------------------------------

def summary():
    p = _products()
    zones = [HOME["zone"], HOME["marine"]]
    return {"place": HOME["name"], "now": metar(), "taf": taf(), "forecast": zone_product("ZFP", HOME["office"], HOME["zone"]),
            "marine": zone_product("CWF", HOME["office"], HOME["marine"]), "space": space(),
            "alerts": p.active_alerts(zones=zones)}


# ---- sun and dish geometry (same formulas as the Sky card on the Aim tab) ------------------------

def sun_pos(t):
    """(elevation, azimuth) degrees of the sun from Stratford at UNIX time t."""
    import math
    r = math.pi / 180
    n = t / 86400 + 2440587.5 - 2451545.0
    L = 280.460 + 0.9856474 * n
    g = (357.528 + 0.9856003 * n) * r
    lam = (L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) * r
    eps = (23.439 - 0.0000004 * n) * r
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    H = (280.46061837 + 360.98564736629 * n + HOME["lon"]) * r - ra
    p = HOME["lat"] * r
    el = math.asin(math.sin(p) * math.sin(dec) + math.cos(p) * math.cos(dec) * math.cos(H))
    az = math.atan2(math.sin(H), math.cos(H) * math.sin(p) - math.tan(dec) * math.cos(p)) + math.pi
    return el / r, (az / r + 360) % 360


def dish_dir(sat_lon=-75.0):
    import math
    r = math.pi / 180
    d, p = (sat_lon - HOME["lon"]) * r, HOME["lat"] * r
    c = math.cos(p) * math.cos(d)
    k = 6378.14 / 42164.17
    return math.atan((c - k) / math.sqrt(1 - c * c)) / r, (180 + math.atan(math.tan(d) / math.sin(p)) / r) % 360


def sun_separation(t):
    """Degrees between the sun and the dish direction, or None when the sun is below the horizon."""
    import math
    r = math.pi / 180
    el, az = sun_pos(t)
    if el <= 0:
        return None
    de, da = dish_dir()
    c = math.sin(el * r) * math.sin(de * r) + math.cos(el * r) * math.cos(de * r) * math.cos((az - da) * r)
    return math.acos(max(-1.0, min(1.0, c))) / r
