#!/usr/bin/env python3
"""Numbers derived from everything the Pi gathers, written to ~/goes/insights.json for the panel's Insights tab.

  surface water temperature  checked against the water temperatures NWS Mount Holly reports (TIDACY bulletins);
                             the median miss becomes the calibration offset in sst-bias.json that views.py applies
  radar                      NWS Northeast radar mosaic sampled around Stratford (radar-history.jsonl)
  cloud cover                satellite cloud fraction near Stratford per scan (insights-scan.jsonl, written by
                             views.py) compared with what the KBDR METARs say
  forecast accuracy          each point forecast is stored when issued and scored against KBDR observations
  dish vs weather            signal-history.jsonl by weather, radar echoes and sun position

Run from cron every 15 minutes:  insights.py [--out FILE]
"""
import argparse
import bisect
import datetime as dt
import fcntl
import importlib.util
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

GOES = Path("/home/pi/goes")
EMWIN = GOES / "data" / "emwin"
DATA = GOES / "data" / "goes19"
OUT = GOES / "insights.json"
SCAN_LOG = GOES / "insights-scan.jsonl"
RADAR_LOG = GOES / "radar-history.jsonl"
SST_PAIRS = GOES / "sst-validation.jsonl"
FCST_LOG = GOES / "forecast-log.jsonl"
VERIF_LOG = GOES / "verification.jsonl"
SIGNAL = GOES / "signal-history.jsonl"
DISH_DAILY = GOES / "dish-daily.json"
LOCAL = ZoneInfo("America/New_York")
HOME = (40.0000, -75.0000)

SKY_PCT = {"CLR": 0, "SKC": 0, "FEW": 15, "SCT": 40, "BKN": 75, "OVC": 100, "VV": 100}
FC_SKY_PCT = {"Clear": 0, "Few clouds": 15, "Scattered": 40, "Broken": 75, "Overcast": 100}
LEADS = [(0, 6, "0-6 h"), (6, 12, "6-12 h"), (12, 24, "12-24 h"), (24, 48, "1-2 days"), (48, 200, "2+ days")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


surface = load("surface", GOES / "surface.py")
products = load("goes_products", GOES / "webui" / "products.py")
local = load("goes_local", GOES / "webui" / "local.py")


def read_jsonl(path):
    out = []
    try:
        for ln in path.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def append_jsonl(path, rows):
    if rows:
        with path.open("a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


def median(xs):
    return statistics.median(xs) if xs else None


def r1(x, n=1):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


# ---- observations ---------------------------------------------------------------------------------------------

def metar_list():
    """[(t, decoded)] oldest first, from the rolling KBDR log that the panel keeps."""
    out = []
    for j in read_jsonl(local.METAR_LOG):
        try:
            d = local.decode_metar(j["raw"])
            d["t"] = float(j["t"])
            out.append((d["t"], d))
        except (KeyError, ValueError, IndexError):
            pass
    out.sort(key=lambda x: x[0])
    return out


def nearest(items, t, tol):
    """Item (t, value) closest to t within tol seconds from a sorted list, or None."""
    ks = [x[0] for x in items]
    i = bisect.bisect_left(ks, t)
    best = None
    for k in (i - 1, i):
        if 0 <= k < len(items) and abs(items[k][0] - t) <= tol and (best is None or abs(items[k][0] - t) < abs(best[0] - t)):
            best = items[k]
    return best


def obs_sky_pct(d):
    codes = [s.get("code") for s in d.get("sky", [])]
    vals = [SKY_PCT[c] for c in codes if c in SKY_PCT]
    return max(vals) if vals else (0 if codes == [] and "CLR" in d.get("raw", "") else None)


def wx_kind(d):
    words = " ".join(d.get("wx", []))
    if re.search(r"snow|ice pellets|snow grains", words):
        return "snow"
    if re.search(r"rain|drizzle|precip", words) or d.get("precip"):
        return "rain"
    if "fog" in words:
        return "fog"
    return None


# ---- surface temperature ----------------------------------------------------------------------------------------

COAST_RE = re.compile(r"For (?:the )?(.+?) at [^:]+:\s*(.*?)(?=\nFor |\Z)", re.S)
COAST_STATIONS = {"Sandy Hook": "Sandy Hook", "Manasquan": "Manasquan", "Atlantic City Steel Pier": "Atlantic City"}


def coastal_obs():
    """{(local date, point): water temperature F} from the NWS coastal observation bulletins, first report of each day."""
    out = {}
    for p in sorted(EMWIN.glob("*TIDACYPA.TXT")):
        m = re.search(r"_KWIN_(\d{14})_", p.name)
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        day = t.astimezone(LOCAL).date().isoformat()
        try:
            text = p.read_text(errors="replace").replace("\r", "")
        except OSError:
            continue
        for st, body in COAST_RE.findall(text):
            name = COAST_STATIONS.get(st.strip())
            w = re.search(r"water temperature was (\d+)", body)
            if name and w:
                out.setdefault((day, name), int(w.group(1)))
    return out


def sst_section(scans, now):
    obs = coastal_obs()
    by_day = {}
    for r in scans:
        if r.get("view") != "sst_eastus":
            continue
        day = dt.datetime.fromtimestamp(ts(r["t"]), dt.timezone.utc).astimezone(LOCAL).date().isoformat()
        for name, f in r.get("points", {}).items():
            by_day.setdefault((day, name), []).append(f)
    pairs = {(p["date"], p["point"]): p for p in read_jsonl(SST_PAIRS)}
    new = []
    for (day, name), ob in obs.items():
        sat = by_day.get((day, name), [])
        if len(sat) >= 3:
            row = {"date": day, "point": name, "obs_f": ob, "sat_f": round(statistics.mean(sat), 1), "n": len(sat)}
            if pairs.get((day, name)) != row:
                pairs[(day, name)] = row
                new.append(row)
    if new:
        SST_PAIRS.write_text("".join(json.dumps(v) + "\n" for v in sorted(pairs.values(), key=lambda p: (p["date"], p["point"]))))
    rows = sorted(pairs.values(), key=lambda p: (p["date"], p["point"]))[-60:]
    resid = [p["sat_f"] - p["obs_f"] for p in rows]
    days = len({p["date"] for p in rows})
    out = {"pairs": len(rows), "days": days, "mean_error_f": r1(statistics.mean(resid)) if resid else None,
           "median_error_f": r1(median(resid)), "recent": rows[-9:]}
    if len(rows) >= 8 and days >= 3:
        off = max(-3.0, min(3.0, -median(resid) / 1.8))
        surface.BIAS_FILE.write_text(json.dumps({"offset_k": round(off, 2), "pairs": len(rows), "days": days,
                                                 "median_error_f": r1(median(resid)), "updated": iso(now)}))
        out["calibrated"] = True
        out["offset_k"] = round(off, 2)
    else:
        out["calibrated"] = False
        out["offset_k"] = surface.offset_k()
    off_f = out["offset_k"] * 1.8
    lastS = [r for r in scans if r.get("view") == "sst_stratford" and r.get("sound_f") is not None]
    if lastS:
        r = lastS[-1]
        pts = next((x.get("points") for x in reversed(scans) if x.get("view") == "sst_eastus" and x.get("points")), {})
        out["latest"] = {"t": r["t"], "sound_f": r1(r["sound_f"] + off_f), "sound_clear": r.get("sound_clear"),
                         "points": {k: r1(v + off_f) for k, v in pts.items()}}
        day = [x["sound_f"] + off_f for x in lastS if now - ts(x["t"]) < 86400]
        week = {}
        for x in lastS:
            d = dt.datetime.fromtimestamp(ts(x["t"]), dt.timezone.utc).astimezone(LOCAL).date().isoformat()
            week.setdefault(d, []).append(x["sound_f"] + off_f)
        out["sound_days"] = [{"date": d, "f": r1(statistics.median(v)), "n": len(v)} for d, v in sorted(week.items())[-14:]]
        out["sound_24h"] = {"min": r1(min(day)), "max": r1(max(day))} if day else None
    return out


# ---- radar --------------------------------------------------------------------------------------------------------

def radar_section(now):
    seen = {r["t"] for r in read_jsonl(RADAR_LOG)}
    new = []
    for t, p in surface.radar_files():
        key = t.isoformat()
        if key in seen or now - t.timestamp() > 3 * 86400:
            continue
        try:
            dbz = surface.radar_dbz(p)
        except Exception:
            continue
        c, m10 = surface.radar_sample(dbz, *HOME, radius_km=10)
        _c, m25 = surface.radar_sample(dbz, *HOME, radius_km=25)
        x, y = surface.radar_pixels(HOME[0], HOME[1])
        xi, yi = int(round(float(x))), int(round(float(y)))
        win = dbz[yi - 10:yi + 11, xi - 10:xi + 11]
        frac = float((win >= 20).mean()) if win.size else None
        new.append({"t": key, "center": r1(c), "max10": r1(m10), "max25": r1(m25), "frac20": r1(frac, 3)})
    append_jsonl(RADAR_LOG, new)
    rows = sorted(read_jsonl(RADAR_LOG), key=lambda r: r["t"])
    day = [r for r in rows if now - ts(r["t"]) < 86400]
    out = {"frames_24h": len(day), "latest": rows[-1] if rows else None}
    if day:
        wet = [r for r in day if (r.get("max25") or 0) >= 20]
        out["wet_frames_24h"] = len(wet)
        out["max_dbz_24h"] = max((r.get("max25") or -99) for r in day) if wet else None
        out["last_echo"] = wet[-1]["t"] if wet else None
    return out


# ---- clouds -----------------------------------------------------------------------------------------------------

def clouds_section(scans, metars, now):
    home = {}
    for r in scans:
        if r.get("view") == "sst_stratford" and r.get("clear_home") is not None:
            home[r["t"]] = r
    recs = sorted(home.values(), key=lambda r: r["t"])
    days, hours = {}, {}
    agree = {"both_cloudy": 0, "both_clear": 0, "sat_cloudy_only": 0, "obs_cloudy_only": 0}
    for r in recs:
        t = ts(r["t"])
        cloud = 1.0 - r["clear_home"]
        lt = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(LOCAL)
        days.setdefault(lt.date().isoformat(), []).append(cloud)
        hours.setdefault(lt.hour, []).append(cloud)
        m = nearest(metars, t, 35 * 60)
        if m:
            pct = obs_sky_pct(m[1])
            if pct is not None:
                sat_c, obs_c = cloud >= 0.5, pct >= 75
                agree["both_cloudy" if sat_c and obs_c else "both_clear" if not sat_c and not obs_c else
                      "sat_cloudy_only" if sat_c else "obs_cloudy_only"] += 1
    n = sum(agree.values())
    out = {"scans": len(recs), "days": [{"date": d, "cloud": r1(100 * statistics.mean(v), 0), "n": len(v)} for d, v in sorted(days.items())[-30:]],
           "by_hour": [{"hour": h, "cloud": r1(100 * statistics.mean(v), 0)} for h, v in sorted(hours.items())],
           "agreement": dict(agree, n=n, pct=r1(100 * (agree["both_cloudy"] + agree["both_clear"]) / n, 0) if n else None)}
    if recs:
        out["latest"] = {"t": recs[-1]["t"], "cloud": r1(100 * (1 - recs[-1]["clear_home"]), 0)}
        ok = [statistics.mean(v) for v in days.values()]
        out["mean_cloud"] = r1(100 * statistics.mean(ok), 0)
        out["clear_days"] = sum(1 for v in days.values() if statistics.mean(v) < 0.2)
        out["cloudy_days"] = sum(1 for v in days.values() if statistics.mean(v) > 0.7)
        out["first"] = recs[0]["t"]
    return out


# ---- forecast verification -----------------------------------------------------------------------------------

def forecast_section(metars, now):
    logged = {r["issued"]: r for r in read_jsonl(FCST_LOG)}
    new = []
    rows = local._newest("PFM", local.HOME["office"], n=200, hours=24 * 14)
    for r in rows:
        if r["t"] in logged:
            continue
        pf = local.point_forecast(rows=[r], keep_past=True)
        if pf and pf["points"]:
            rec = {"issued": r["t"], "name": pf["name"],
                   "points": [{k: p.get(k) for k in ("t", "temp", "wind_mph", "sky", "pop", "step_h")} for p in pf["points"]]}
            logged[r["t"]] = rec
            new.append(rec)
    append_jsonl(FCST_LOG, new)
    done = {(v["issued"], v["valid"]) for v in read_jsonl(VERIF_LOG)}
    fresh = []
    for issued, rec in logged.items():
        if now - issued > 20 * 86400:
            continue
        for p in rec["points"]:
            if (issued, p["t"]) in done or p["t"] > now - 40 * 60 or p["t"] <= issued:
                continue
            o = nearest(metars, p["t"], 35 * 60)
            if not o or o[1].get("temp_f") is None:
                continue
            pers = nearest([m for m in metars if m[0] <= issued], issued, 90 * 60)
            wnd = [m for m in metars if abs(m[0] - p["t"]) <= 6 * 3600]
            fresh.append({"issued": issued, "valid": p["t"], "lead_h": round((p["t"] - issued) / 3600, 1),
                          "fc_temp": p["temp"], "ob_temp": o[1]["temp_f"], "pers_temp": pers[1].get("temp_f") if pers else None,
                          "fc_wind": p["wind_mph"], "ob_wind": round((o[1].get("wind_kt") or 0) * 1.15078, 1),
                          "fc_sky": FC_SKY_PCT.get(p["sky"]), "ob_sky": obs_sky_pct(o[1]),
                          "fc_pop": p["pop"], "ob_precip": any(wx_kind(m[1]) in ("rain", "snow") for m in wnd) if len(wnd) >= 6 else None})
            done.add((issued, p["t"]))
    append_jsonl(VERIF_LOG, fresh)
    ver = [v for v in read_jsonl(VERIF_LOG) if now - v["valid"] < 30 * 86400]
    out = {"issued": len(logged), "scored": len(ver), "buckets": []}
    for lo, hi, label in LEADS:
        b = [v for v in ver if lo <= v["lead_h"] < hi]
        if not b:
            continue
        te = [v["fc_temp"] - v["ob_temp"] for v in b if v["fc_temp"] is not None]
        pe = [v["pers_temp"] - v["ob_temp"] for v in b if v.get("pers_temp") is not None and v["fc_temp"] is not None]
        we = [abs(v["fc_wind"] - v["ob_wind"]) for v in b if v["fc_wind"] is not None]
        se = [abs(v["fc_sky"] - v["ob_sky"]) for v in b if v["fc_sky"] is not None and v["ob_sky"] is not None]
        out["buckets"].append({"label": label, "n": len(b), "temp_mae": r1(statistics.mean(abs(x) for x in te)) if te else None,
                               "temp_bias": r1(statistics.mean(te)) if te else None,
                               "persistence_mae": r1(statistics.mean(abs(x) for x in pe)) if pe else None,
                               "wind_mae": r1(statistics.mean(we)) if we else None, "sky_mae": r1(statistics.mean(se), 0) if se else None})
    pp = [(v["fc_pop"] / 100.0, 1.0 if v["ob_precip"] else 0.0) for v in ver if v.get("fc_pop") is not None and v.get("ob_precip") is not None]
    if pp:
        base = statistics.mean(o for _p, o in pp)
        brier = statistics.mean((p - o) ** 2 for p, o in pp)
        clim = statistics.mean((base - o) ** 2 for _p, o in pp)
        out["pop"] = {"n": len(pp), "brier": r1(brier, 3), "climatology_brier": r1(clim, 3), "rain_rate": r1(100 * base, 0)}
    out["recent"] = [{k: v[k] for k in ("valid", "lead_h", "fc_temp", "ob_temp")}
                     for v in sorted((v for v in ver if v["fc_temp"] is not None), key=lambda v: (v["valid"], v["lead_h"]))[-8:]]
    t24 = [v for v in ver if v["lead_h"] <= 24 and now - v["valid"] < 7 * 86400 and v["fc_temp"] is not None]
    if t24:
        out["headline"] = {"temp_mae_f": r1(statistics.mean(abs(v["fc_temp"] - v["ob_temp"]) for v in t24)), "n": len(t24), "days": 7}
    return out


# ---- dish vs weather -----------------------------------------------------------------------------------------------

SUN_BINS = [(2, "under 2 deg"), (5, "2-5 deg"), (10, "5-10 deg"), (999, "10+ deg")]
CATS = ["clear", "cloudy", "fog", "rain", "snow"]


def dish_section(metars, now):
    sig = read_jsonl(SIGNAL)
    radar = sorted((ts(r["t"]), r) for r in read_jsonl(RADAR_LOG))
    daily = {}
    try:
        daily = json.loads(DISH_DAILY.read_text())
    except (OSError, ValueError):
        pass
    fresh = {}
    for r in sig:
        if r.get("vit") is None or r.get("ok") is None:
            continue
        t = float(r["t"])
        day = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(LOCAL).date().isoformat()
        m = nearest(metars, t, 70 * 60)
        rd = nearest(radar, t, 20 * 60)
        cat = "clear"
        if m:
            d = m[1]
            k = wx_kind(d)
            pct = obs_sky_pct(d)
            cat = k or ("cloudy" if (pct or 0) >= 75 else "clear")
        if rd and (rd[1].get("max10") or -99) >= 15 and cat not in ("snow",):
            cat = "rain"
        sep = local.sun_separation(t)
        sb = next((lab for lim, lab in SUN_BINS if sep is not None and sep < lim), "sun down") if sep is not None else "sun down"
        locked = 1 if r["ok"] >= 40 else 0
        e = fresh.setdefault(day, {"cat": {}, "sun": {}})
        for grp, key in (("cat", cat), ("sun", sb)):
            a = e[grp].setdefault(key, [0, 0.0, 0])
            a[0] += 1
            a[1] += float(r["vit"])
            a[2] += locked
    daily.update(fresh)
    cut = (dt.datetime.now(LOCAL) - dt.timedelta(days=365)).date().isoformat()
    daily = {d: v for d, v in daily.items() if d >= cut}
    DISH_DAILY.write_text(json.dumps(daily))
    tot = {"cat": {}, "sun": {}}
    for v in daily.values():
        for grp in ("cat", "sun"):
            for k, a in v[grp].items():
                s = tot[grp].setdefault(k, [0, 0.0, 0])
                for i in range(3):
                    s[i] += a[i]

    def rows(grp, order):
        return [{"name": k, "minutes": tot[grp][k][0], "vit": r1(tot[grp][k][1] / tot[grp][k][0]),
                 "locked_pct": r1(100 * tot[grp][k][2] / tot[grp][k][0])} for k in order if k in tot[grp] and tot[grp][k][0] >= 5]
    cats = rows("cat", CATS)
    sun = rows("sun", [lab for _l, lab in SUN_BINS] + ["sun down"])
    base = next((c["vit"] for c in cats if c["name"] == "clear"), None)
    for c in cats:
        c["delta"] = r1(c["vit"] - base) if base is not None else None
    perday = []
    for d, v in sorted(daily.items())[-14:]:
        n = sum(a[0] for a in v["cat"].values())
        if n:
            perday.append({"date": d, "minutes": n, "vit": r1(sum(a[1] for a in v["cat"].values()) / n),
                           "locked_pct": r1(100 * sum(a[2] for a in v["cat"].values()) / n)})
    return {"days": len(daily), "minutes": sum(c["minutes"] for c in cats), "categories": cats, "sun": sun, "per_day": perday}


# ---- newest pictures -----------------------------------------------------------------------------------------------

def newest_image(view):
    d = DATA / view
    try:
        days = sorted(p for p in d.iterdir() if p.is_dir())
        for day in reversed(days[-2:]):
            files = sorted(day.glob("*.jpg"))
            if files:
                return str(files[-1].relative_to(GOES / "data"))
    except OSError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    lock = open("/tmp/goes-insights.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    now = time.time()
    scans = read_jsonl(SCAN_LOG)
    metars = metar_list()
    res, errors = {"updated": iso(now)}, {}
    for name, fn in (("sst", lambda: sst_section(scans, now)), ("radar", lambda: radar_section(now)),
                     ("clouds", lambda: clouds_section(scans, metars, now)), ("forecast", lambda: forecast_section(metars, now)),
                     ("dish", lambda: dish_section(metars, now))):
        try:
            res[name] = fn()
        except Exception as e:
            errors[name] = repr(e)
            print(f"{name}: {e!r}", file=sys.stderr)
    res["images"] = {v: newest_image(v) for v in ("sst_stratford", "rad_stratford", "sst_eastus", "rad_northeast")}
    if errors:
        res["errors"] = errors
    tmp = Path(a.out + ".tmp")
    tmp.write_text(json.dumps(res))
    tmp.replace(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
