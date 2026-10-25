"""Background monitor for the GOES panel: signal history, watchdog and email alerts.

Started from app.py with start(procs). Everything here runs in daemon threads.
  - signal history: one averaged goesrecv sample per minute -> ~/goes/signal-history.jsonl
  - watchdog: restarts goesrecv/goesproc that died or stalled while the user wants them running
  - alerts: emails for no-signal, restarts, low disk / high temperature, and NWS warnings from EMWIN
"""
import datetime as dt
import json
import re
import statistics
import sys
import shutil
import smtplib
import threading
import time
import tomllib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

GOES_DIR = Path.home() / "goes"
HISTORY = GOES_DIR / "signal-history.jsonl"
MAIL_CONF = GOES_DIR / "mail.conf"
DIGEST = GOES_DIR / "digest.py"
EMWIN = GOES_DIR / "data" / "emwin"
SEEN = GOES_DIR / ".alerts-seen"
KEEP_DAYS = 14

NO_SIGNAL_MIN = 10        # zero decoded packets this long while goesrecv runs -> alert
STALL_SECS = 120          # no monitor line this long -> goesrecv is hung, restart it
MAX_RESTARTS_PER_HOUR = 4
DISK_MIN_FRAC = 0.05
TEMP_MAX_C = 80.0
WX_MAX_PER_HOUR = 10

_lock = threading.Lock()
_alerts = {}              # key -> last time sent (monotonic)
_wx_sent = []             # monotonic times of weather emails
_state = {"signal": "unknown", "last_restart": None, "last_alert": None, "restarts": []}
_digest_mod = {}


# ---- email ---------------------------------------------------------------------

def _digest():
    if "m" not in _digest_mod:
        import importlib.util
        spec = importlib.util.spec_from_file_location("goes_digest", DIGEST)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _digest_mod["m"] = mod
    return _digest_mod["m"]


def _config():
    cfg = tomllib.loads(MAIL_CONF.read_text())
    if not Path(cfg["password_file"]).exists():
        raise RuntimeError("password file missing")
    return cfg


def notify(subject, body, key=None, every=6 * 3600):
    """Email the alert recipients. key+every rate-limits repeats of the same alert."""
    now = time.monotonic()
    with _lock:
        if key and key in _alerts and now - _alerts[key] < every:
            return False
        if key:
            _alerts[key] = now
    try:
        cfg = _config()
        to = cfg.get("alert_recipients") or cfg["recipients"]
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender']}>"
        msg["To"] = ", ".join(to)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="goes.local")
        msg.set_content(body)
        _digest().send(cfg, msg)
        _state["last_alert"] = {"t": time.time(), "subject": subject}
        try:   # phone push too, when [alerts] ntfy_topic is set
            _digest().push(cfg, subject, body[:900],
                           "urgent" if subject.startswith("NWS:") and "Warning" in subject else "high",
                           "rotating_light" if subject.startswith("NWS:") else "warning")
        except Exception as e:
            print("push error:", e, flush=True)
        print("alert sent:", subject, flush=True)
        return True
    except Exception as e:  # never let mail trouble kill the monitor
        print("alert failed:", subject, "-", e, flush=True)
        return False


# ---- signal history ------------------------------------------------------------

def _avg(pts, k):
    v = [p[k] for p in pts if p.get(k) is not None]
    return round(sum(v) / len(v), 2) if v else None


def _trim_history():
    if not HISTORY.exists():
        return
    cut = time.time() - KEEP_DAYS * 86400
    keep = []
    for ln in HISTORY.read_text().splitlines():
        try:
            if json.loads(ln)["t"] >= cut:
                keep.append(ln)
        except (ValueError, KeyError):
            pass
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""))
    tmp.replace(HISTORY)


def history(hours=24, buckets=288):
    """Signal history for the last `hours`, averaged into at most `buckets` points."""
    cut = time.time() - hours * 3600
    rows = []
    if HISTORY.exists():
        for ln in HISTORY.read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r["t"] >= cut:
                rows.append(r)
    width = max(60, hours * 3600 / buckets)
    out, cur, edge = [], [], None
    for r in rows:
        b = int(r["t"] // width)
        if edge is not None and b != edge:
            out.append(_bucket(cur))
            cur = []
        edge = b
        cur.append(r)
    if cur:
        out.append(_bucket(cur))
    return out


def _bucket(rows):
    return {"t": rows[len(rows) // 2]["t"], **{k: _avg(rows, k) for k in ("vit", "ok", "drops", "gain", "freq")}}


def _sampler(recv):
    """Write one averaged sample per minute from goesrecv's monitor lines."""
    _trim_history()
    last_t, acc, minute, trimmed = 0.0, [], None, time.time()
    while True:
        time.sleep(5)
        pts = [p for p in list(recv.history) if p["t"] > last_t]
        if pts:
            last_t = pts[-1]["t"]
        for p in pts:
            m = int(p["t"] // 60)
            if minute is not None and m != minute and acc:
                _write_sample(acc, minute)
                acc = []
            minute = m
            acc.append(p)
        if time.time() - trimmed > 86400:
            _trim_history()
            trimmed = time.time()


def _write_sample(acc, minute):
    row = {"t": minute * 60 + 30}
    for k in ("vit", "ok", "drops", "gain", "freq"):
        row[k] = _avg(acc, k)
    with HISTORY.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---- watchdog ------------------------------------------------------------------

def _restart_allowed():
    now = time.time()
    _state["restarts"] = [t for t in _state["restarts"] if now - t < 3600]
    return len(_state["restarts"]) < MAX_RESTARTS_PER_HOUR


def _bounce(procs, name, why):
    """Restart one process, at most MAX_RESTARTS_PER_HOUR times an hour."""
    if not _restart_allowed():
        notify(f"GOES: {name} keeps failing", f"{name} needed a restart ({why}) but {MAX_RESTARTS_PER_HOUR} "
               "restarts already happened in the last hour, so the watchdog is leaving it alone.",
               key=f"giveup-{name}", every=3 * 3600)
        return
    _state["restarts"].append(time.time())
    print(f"watchdog: restarting {name} ({why})", flush=True)
    pr = procs[name]
    pr.stop()
    time.sleep(2)
    res = pr.start()
    _state["last_restart"] = {"t": time.time(), "name": name, "why": why, "result": res}
    notify(f"GOES: {name} restarted", f"The watchdog restarted {name}.\nReason: {why}\nResult: {res}",
           key=f"restart-{name}", every=1800)


def _watchdog(procs, external_pids):
    time.sleep(60)  # let the autostart finish
    zero_since = None
    while True:
        time.sleep(15)
        try:
            recv, proc = procs["goesrecv"], procs["goesproc"]
            if not recv.wanted:
                zero_since = None
                _state["signal"] = "stopped"
                continue
            now = time.time()
            if not recv.running() and not external_pids("goesrecv"):
                _bounce(procs, "goesrecv", "it exited unexpectedly")
                zero_since = None
                continue
            if recv.running() and recv.started and now - recv.started > STALL_SECS:
                last = recv.history[-1]["t"] if recv.history else 0
                if now - max(last, recv.started) > STALL_SECS:
                    _bounce(procs, "goesrecv", f"no output for {STALL_SECS} s (hung SDR?)")
                    zero_since = None
                    continue
            if recv.running() and recv.history:
                recent = [p for p in recv.history if now - p["t"] < 60]
                ok = _avg(recent, "ok") if recent else None
                if ok is not None and ok > 0:
                    if _state["signal"] == "lost":
                        notify("GOES: signal is back", f"goesrecv is decoding again (~{ok:.0f} packets/s).",
                               key="signal-back", every=0)
                    _state["signal"] = "locked"
                    zero_since = None
                elif ok == 0:
                    zero_since = zero_since or now
                    if now - zero_since > NO_SIGNAL_MIN * 60 and _state["signal"] != "lost":
                        _state["signal"] = "lost"
                        notify("GOES: no signal", f"goesrecv has decoded 0 packets for {NO_SIGNAL_MIN}+ minutes.\n"
                               "The SDR is running, so check the dish pointing (wind or something bumped it), "
                               "the cable, and the SAWbird+ LED.", key="no-signal", every=3 * 3600)
            if proc.wanted and not proc.running() and not external_pids("goesproc") and recv.running() \
                    and recv.started and now - recv.started > 40:
                _bounce(procs, "goesproc", "it exited unexpectedly")
            _health()
        except Exception as e:
            print("watchdog error:", e, flush=True)


def _health():
    du = shutil.disk_usage(GOES_DIR)
    if du.free / du.total < DISK_MIN_FRAC:
        notify("GOES: disk almost full", f"Only {du.free / 1e9:.1f} GB of {du.total / 1e9:.0f} GB is free. "
               "The prune script may not be keeping up.", key="disk", every=12 * 3600)
    try:
        c = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return
    if c > TEMP_MAX_C:
        notify("GOES: Pi is overheating", f"CPU temperature is {c:.0f} C.", key="temp", every=6 * 3600)


def status():
    return dict(_state, restarts=len(_state["restarts"]))


# ---- NWS warnings from EMWIN ---------------------------------------------------

PHEN = {"TO": "Tornado", "SV": "Severe Thunderstorm", "FF": "Flash Flood", "FL": "Flood", "FA": "Areal Flood",
        "SM": "Special Marine", "EW": "Extreme Wind", "WS": "Winter Storm", "BZ": "Blizzard", "IS": "Ice Storm",
        "HU": "Hurricane", "TR": "Tropical Storm", "SS": "Storm Surge", "HW": "High Wind", "CF": "Coastal Flood",
        "SQ": "Snow Squall", "LE": "Lake Effect Snow", "DS": "Dust Storm", "EH": "Excessive Heat", "HT": "Heat",
        "WW": "Winter Weather", "WC": "Wind Chill", "FZ": "Freeze", "FR": "Frost", "GL": "Gale", "SE": "Hazardous Seas",
        "SC": "Small Craft", "SR": "Storm", "TS": "Tsunami", "FW": "Red Flag"}
SIG = {"W": "Warning", "A": "Watch", "Y": "Advisory", "S": "Statement"}
VTEC = re.compile(r"/[OTEX]\.(NEW|EXA|EXB|UPG)\.(K[A-Z]{3})\.([A-Z]{2})\.([WAYS])\.(\d{4})\.")
NAME_RE = re.compile(r"-\d-([A-Z]{3})([A-Z0-9]{3})([A-Z]{2})\.TXT$")
ZONE_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}[->\dA-Z]*", re.M)


def _wx_cfg():
    try:
        a = tomllib.loads(MAIL_CONF.read_text()).get("alerts", {})
    except (OSError, ValueError):
        a = {}
    return {"on": a.get("weather", True), "offices": a.get("wx_offices", ["OKX"]),
            "levels": a.get("wx_levels", ["W", "A"]), "zones": a.get("wx_zones", [])}


def _text(p):
    return p.read_bytes().decode("utf-8", errors="replace").replace("\r\r\n", "\n").replace("\r\n", "\n")


def wx_check(first_run=False, dry=False):
    """Look at new EMWIN warning bulletins. first_run only records them (no email). Returns matches."""
    cfg = _wx_cfg()
    if not cfg["on"]:
        return []
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    new, hits = [], []
    for p in EMWIN.glob("*.TXT"):
        if p.name in seen:
            continue
        m = NAME_RE.search(p.name)
        if not m or f"K{m.group(2)}" not in [("K" + o[-3:]) for o in cfg["offices"]]:
            continue
        new.append(p.name)
        if first_run:
            continue
        txt = _text(p)
        for v in VTEC.finditer(txt):
            action, office, phen, sig, num = v.groups()
            if sig not in cfg["levels"]:
                continue
            if cfg["zones"] and not any(z in txt for z in cfg["zones"]):
                continue
            hits.append((p, action, office, phen, sig, num, txt))
            break
    if not dry:
        SEEN.write_text("\n".join(sorted(seen | set(new))[-3000:]) + "\n")
    out = []
    for p, action, office, phen, sig, num, txt in hits:
        name = f"{PHEN.get(phen, phen)} {SIG.get(sig, sig)}"
        if action in ("EXA", "EXB"):
            name += " (expanded)"
        elif action == "UPG":
            name += " (upgrade)"
        lines = txt.strip().splitlines()
        body = "\n".join(lines[:60]) + ("\n[...]" if len(lines) > 60 else "")
        out.append((name, office, body))
        if not dry:
            now = time.monotonic()
            _wx_sent[:] = [t for t in _wx_sent if now - t < 3600]
            if len(_wx_sent) < WX_MAX_PER_HOUR:
                _wx_sent.append(now)
                notify(f"NWS: {name} - {office}", body)
    return out


def _wx_loop():
    time.sleep(30)
    wx_check(first_run=True)
    while True:
        time.sleep(120)
        try:
            wx_check()
        except Exception as e:
            print("weather alert error:", e, flush=True)


# ---- long-term trend and daily health (phone push only; not emailed to the briefing list) --------------

HEALTH_SENT = GOES_DIR / ".health-sent"
HEALTH_TIME = (7, 30)     # local (US Eastern) time of the daily health push
LOCAL = None


def _stats(rows):
    locked = [r for r in rows if (r.get("ok") or 0) > 20 and r.get("vit") is not None]
    return {"n": len(rows), "lock": len(locked) / len(rows) if rows else None,
            "vit": statistics.median(r["vit"] for r in locked) if len(locked) >= 30 else None}


def trend(now=None):
    """Last 24 h of goesrecv samples against the 6 days before them (locked minutes only for Viterbi errors)."""
    now = now or time.time()
    rows = []
    try:
        for ln in HISTORY.read_text().splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return {"recent": _stats([r for r in rows if r["t"] > now - 86400]),
            "base": _stats([r for r in rows if now - 7 * 86400 < r["t"] <= now - 86400])}


def trend_problem(tr):
    """A sentence if the last day is clearly worse than the week before, else None."""
    rec, base = tr["recent"], tr["base"]
    if rec["n"] < 600 or base["n"] < 3 * 600:       # need ~10 h now and ~2.5 days of baseline running time
        return None
    if rec["vit"] and base["vit"] and rec["vit"] > base["vit"] * 1.35 and rec["vit"] - base["vit"] > 15:
        return (f"Signal quality has dropped: median Viterbi errors {rec['vit']:.0f} over the last 24 h against "
                f"{base['vit']:.0f} the previous six days. Check whether the dish was knocked, is wet, snowy or "
                "blocked by foliage, and check the cable and SAWbird+ connections.")
    if rec["lock"] is not None and base["lock"] is not None and rec["lock"] < min(0.8, base["lock"] - 0.25):
        return (f"The receiver was locked only {rec['lock'] * 100:.0f}% of the last 24 h against "
                f"{base['lock'] * 100:.0f}% the previous six days.")
    return None


def push_only(title, body, key=None, every=24 * 3600, priority="default", tags="bar_chart"):
    """ntfy push to the owner's phone, rate-limited like notify(). Returns True if sent."""
    now = time.monotonic()
    with _lock:
        if key and key in _alerts and now - _alerts[key] < every:
            return False
        if key:
            _alerts[key] = now
    try:
        cfg = tomllib.loads(MAIL_CONF.read_text())
        if not cfg.get("alerts", {}).get("ntfy_topic"):
            return False
        _digest().push(cfg, title, body[:900], priority, tags)
        print("push sent:", title, flush=True)
        return True
    except Exception as e:
        print("push failed:", title, "-", e, flush=True)
        return False


def health_text(now=None):
    now = now or time.time()
    tr = trend(now)
    rec, base = tr["recent"], tr["base"]
    bits = []
    if rec["lock"] is not None:
        bits.append(f"Locked {rec['lock'] * 100:.0f}% of the last 24 h" + (f" (week before {base['lock'] * 100:.0f}%)" if base["lock"] is not None else ""))
    if rec["vit"]:
        bits.append(f"Viterbi errors {rec['vit']:.0f}" + (f" (week before {base['vit']:.0f}; lower is better)" if base["vit"] else ""))
    try:
        frames = [t for t, _p in _digest().frames("globe") if now - t.timestamp() <= 86400]
        bits.append(f"{len(frames)} full-disk frames in 24 h")
    except Exception:
        pass
    du = shutil.disk_usage(GOES_DIR)
    bits.append(f"disk {100 - du.free / du.total * 100:.0f}% used, {du.free / 1e9:.0f} GB free")
    try:
        bits.append(f"CPU {int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000:.0f} C")
    except OSError:
        pass
    restarts = len(_state["restarts"])
    if restarts:
        bits.append(f"{restarts} restart{'s' if restarts > 1 else ''} in the last hour")
    return ". ".join(bits) + "."


def _trend_loop():
    from zoneinfo import ZoneInfo
    local = ZoneInfo("America/New_York")
    time.sleep(120)
    while True:
        try:
            problem = trend_problem(trend())
            if problem:
                push_only("GOES: signal trend", problem, key="trend", every=24 * 3600, priority="high",
                          tags="chart_with_downwards_trend")
            now = dt.datetime.now(local)
            today = f"{now:%Y-%m-%d}"
            done = HEALTH_SENT.read_text().strip() if HEALTH_SENT.exists() else ""
            if done != today and (now.hour, now.minute) >= HEALTH_TIME and now.hour < 12:
                if push_only("GOES daily health", health_text()):
                    HEALTH_SENT.write_text(today)
        except Exception as e:
            print("trend error:", e, flush=True)
        time.sleep(1800)


# ---- outside heartbeat, new-storm push, cold-cloud-tops push ------------------------------------------

STORM_FILE = GOES_DIR / "storm.json"
STORM_SEEN = GOES_DIR / ".storm-announced"
VIEWS = GOES_DIR / "views.py"
NYC_BOX = (40.2, 42.4, -75.0, -71.2)      # lat0, lat1, lon0, lon1 around Stratford, CT and the Sound
COLD_TOP_K = 213.0                          # about -60 C: deep convection
COLD_PIXELS = 25                            # 2 km pixels colder than 220 K needed so one noisy pixel never alerts
_views_mod = {}


def _alerts_cfg():
    try:
        return tomllib.loads(MAIL_CONF.read_text()).get("alerts", {})
    except (OSError, ValueError):
        return {}


def heartbeat_once(url=None):
    """GET the dead-man-switch URL ([alerts] heartbeat_url, e.g. a healthchecks.io ping). Adds /fail while the
    satellite signal is lost, so that service can also warn about that. Returns True if it answered."""
    url = url or str(_alerts_cfg().get("heartbeat_url", "")).strip()
    if not url.startswith("https://"):
        return False
    if _state["signal"] == "lost":
        url = url.rstrip("/") + "/fail"
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "goes-panel"}), timeout=15).read(64)
        return True
    except Exception as e:
        print("heartbeat failed:", type(e).__name__, flush=True)    # never print the URL: it can be a secret
        return False


def _heartbeat_loop():
    time.sleep(60)
    while True:
        try:
            heartbeat_once()
        except Exception as e:
            print("heartbeat error:", type(e).__name__, flush=True)
        time.sleep(300)


def storm_check():
    """Push once when storm.json names a storm we have not announced yet. Returns the message sent, or None."""
    try:
        j = json.loads(STORM_FILE.read_text())
        name = str(j["name"])[:40]
        lat, lon = float(j["lat"]), float(j["lon"])
    except (OSError, ValueError, KeyError):
        return None
    last = STORM_SEEN.read_text().strip() if STORM_SEEN.exists() else ""
    if last == name:
        return None
    msg = f"{name}: centre near {abs(lat):.1f}{'N' if lat >= 0 else 'S'} {abs(lon):.1f}{'W' if lon < 0 else 'E'}. The Storm view in the panel now follows it."
    if push_only(f"GOES: {name}", msg, tags="cyclone", priority="high"):
        STORM_SEEN.write_text(name)
        return msg
    return None


def _views():
    if "m" not in _views_mod:
        import importlib.util
        spec = importlib.util.spec_from_file_location("goes_views", VIEWS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _views_mod["m"] = mod
    return _views_mod["m"]


def cold_tops(now=None):
    """(min brightness temperature K, pixels colder than 220 K, scan time) near New York from the newest C13 scan,
    or None if there is no fresh scan."""
    v = _views()
    files = v.scan_files()
    if not files.get("13"):
        return None
    t, p = files["13"][-1]
    if (now or time.time()) - t.timestamp() > 90 * 60:
        return None
    table = v.bt_table(p)
    if table is None:
        return None
    la0, la1, lo0, lo1 = NYC_BOX
    box = v.region_box((la0, la1, lo0, lo1), 1)
    w = v.window_bt(v.load_full(p), table, box, 1)
    return float(w.min()), int((w < 220.0).sum()), t


def _convect_loop():
    time.sleep(300)
    while True:
        try:
            got = cold_tops()
            if got and got[0] < COLD_TOP_K and got[1] >= COLD_PIXELS:
                mn, n, t = got
                push_only("GOES: cold cloud tops near Stratford",
                          f"Cloud tops as cold as {mn:.0f} K ({mn - 273.15:.0f} C) over {n * 4} sq km near Stratford at "
                          f"{t:%H:%M} UTC. That can mean strong thunderstorms. Open Views, Cloud tops: Stratford & Sound.",
                          key="coldtops", every=3 * 3600, priority="high", tags="cloud_with_lightning")
        except Exception as e:
            print("cold-tops error:", e, flush=True)
        try:
            storm_check()
        except Exception as e:
            print("storm check error:", e, flush=True)
        time.sleep(900)


def _monthly_loop():
    """Build last month's time-lapse videos in the first days of each month (a skipped run is retried)."""
    import importlib.util
    from zoneinfo import ZoneInfo
    time.sleep(600)
    while True:
        try:
            now = dt.datetime.now(ZoneInfo("America/New_York"))
            if now.day <= 3 and now.hour >= 3:
                spec = importlib.util.spec_from_file_location("goes_monthly", GOES_DIR / "monthly.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                y, m = mod.previous_month(now)
                made = [p for v in mod.VIEWS if (p := mod.build(v, y, m))]
                if made:
                    push_only(f"GOES: {dt.date(y, m, 1):%B %Y} time-lapse ready",
                              "Saved in the Files tab under archive: " + ", ".join(p.name for p in made),
                              tags="movie_camera")
        except Exception as e:
            print("monthly error:", e, flush=True)
        time.sleep(3600)


# ---- Stratford extras: snow/ice on the dish, outage log, space weather, power, best image ----------------

BESTIMG_SENT = GOES_DIR / ".bestimage-sent"


def _local():
    m = sys.modules.get("goes_local")
    if m is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("goes_local", Path(__file__).resolve().parent / "local.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["goes_local"] = m
        spec.loader.exec_module(m)
    return m


def _history_rows(since):
    rows = []
    try:
        for ln in HISTORY.read_text().splitlines()[-30000:]:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r["t"] >= since:
                rows.append(r)
    except OSError:
        pass
    return rows


def dish_problem(now=None):
    """Sentence if the signal fell off in the last 30 min while it is cold and has been precipitating, else None."""
    now = now or time.time()
    rows = _history_rows(now - 8 * 3600)
    rec = [r for r in rows if r["t"] > now - 1800]
    base = [r for r in rows if r["t"] <= now - 1800 and (r.get("ok") or 0) > 20 and r.get("vit") is not None]
    if len(rec) < 10 or len(base) < 120:
        return None
    base_vit = statistics.median(r["vit"] for r in base)
    rec_ok = statistics.median((r.get("ok") or 0) for r in rec)
    rec_vit = statistics.median(r["vit"] for r in rec if r.get("vit") is not None) if any(r.get("vit") is not None for r in rec) else None
    worse = rec_ok < 40 or (rec_vit is not None and rec_vit > base_vit * 1.4 and rec_vit - base_vit > 25)
    if not worse:
        return None
    sep = _local().sun_separation(now)
    if sep is not None and sep < 4:
        return None                                          # that is the sun, not the dish
    obs = _local().metars(hours=4)
    if not obs or now - obs[0]["t"] > 2.5 * 3600:
        return None
    cold = obs[0].get("temp_f") is not None and obs[0]["temp_f"] <= 34
    wet = [o for o in obs if o["precip"]]
    if cold and wet:
        w = wet[0]
        return (f"The signal has dropped and it is {obs[0]['temp_f']}F at Bridgeport after {w['wx'][0] if w['wx'] else 'precipitation'} "
                f"reported there. Snow or ice may be on the dish: brush or rinse it off.")
    return None


def outages(days=14, now=None):
    """Episodes where goesrecv decoded almost nothing (or was not running) for 3+ minutes, newest first, with a guess at why."""
    now = now or time.time()
    rows = _history_rows(now - days * 86400)
    if not rows:
        return []
    loc = _local()
    try:
        obs = loc.metars(hours=days * 24)
    except Exception:
        obs = []
    eps = []

    def add(t0, t1, kind):
        if t1 - t0 >= 180:
            eps.append({"start": t0, "end": t1, "kind": kind})
    cur = None
    prev = None
    for r in rows:
        if prev is not None and r["t"] - prev["t"] > 240:
            if cur:
                add(cur[0], prev["t"], "lost")
                cur = None
            add(prev["t"] + 60, r["t"], "off")
        low = (r.get("ok") or 0) < 10
        if low and cur is None:
            cur = [r["t"], r["t"]]
        elif low:
            cur[1] = r["t"]
        elif cur:
            add(cur[0], cur[1] + 60, "lost")
            cur = None
        prev = r
    if cur:
        add(cur[0], prev["t"] + 60, "lost")
    out = []
    for e in sorted(eps, key=lambda e: -e["start"])[:60]:
        mid = (e["start"] + e["end"]) / 2
        if e["kind"] == "off":
            why = "Receiver stopped or the Pi was off or restarting"
        else:
            sep = loc.sun_separation(mid)
            near = [o for o in obs if abs(o["t"] - mid) <= 5400]
            if sep is not None and sep < 3:
                why = f"Sun outage: the sun was {sep:.1f} degrees from the dish direction"
            elif any(o["precip"] and o.get("temp_f") is not None and o["temp_f"] <= 34 for o in near):
                why = "Snow or ice likely: freezing precipitation reported at Bridgeport"
            elif any(o["precip"] for o in near):
                why = "Precipitation reported at Bridgeport"
            else:
                why = "Signal lost, cause unknown"
        out.append({"start": e["start"], "end": e["end"], "minutes": round((e["end"] - e["start"]) / 60), "why": why,
                    "kind": e["kind"]})
    return out


def _undervoltage():
    import subprocess
    try:
        v = int(subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout.split("=")[1], 16)
    except Exception:
        return None
    return v


def _extras_loop():
    """Every 5 minutes: snow/ice check; every pass: geomagnetic and power pushes."""
    time.sleep(240)
    while True:
        try:
            msg = dish_problem()
            if msg:
                push_only("GOES: check the dish", msg, key="dish-ice", every=6 * 3600, priority="high", tags="snowflake")
        except Exception as e:
            print("dish check error:", e, flush=True)
        try:
            sp = _local().space().get("alert")
            if sp and sp.get("k") and sp["k"] >= 6 and time.time() - sp["t"] < 4 * 3600:
                push_only(f"Geomagnetic storm: K-index {sp['k']}",
                          f"{sp['text']}. {sp.get('aurora', '')} Aurora can reach Connecticut on a clear night at K 7 or more.",
                          key=f"kidx{sp['k']}", every=12 * 3600, priority="high" if sp["k"] >= 7 else "default", tags="sparkles")
        except Exception as e:
            print("space check error:", e, flush=True)
        try:
            v = _undervoltage()
            if v is not None and v & 0x10001:
                push_only("GOES: Pi power problem",
                          "The Pi reported under-voltage" + (" right now" if v & 1 else " since it booted") +
                          ". Check the power supply and cable; low voltage can corrupt the SD card and drop the SDR.",
                          key="undervolt", every=7 * 86400, priority="high", tags="zap")
        except Exception as e:
            print("power check error:", e, flush=True)
        time.sleep(300)


def _cloudiness(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((160, 160))
    px = list(im.getdata())
    return sum(1 for r, g, b in px if min(r, g, b) > 170) / max(len(px), 1)


def best_image(now_local):
    """Clearest Stratford-view frame from 10:00-14:30 local today, or None."""
    d = _digest()
    best = None
    for t, p in d.frames("stratford"):
        lt = t.astimezone(now_local.tzinfo)
        if lt.date() != now_local.date() or not ((10, 0) <= (lt.hour, lt.minute) <= (14, 30)):
            continue
        try:
            c = _cloudiness(p)
        except OSError:
            continue
        if best is None or c < best[0]:
            best = (c, t, p)
    return best


def push_image(title, caption, path):
    """ntfy attachment push (public satellite imagery to the owner's own topic). Returns True if sent."""
    try:
        cfg = tomllib.loads(MAIL_CONF.read_text())
        a = cfg.get("alerts", {})
        topic = str(a.get("ntfy_topic", "")).strip()
        server = str(a.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic) or not server.startswith("https://"):
            return False
        import urllib.request
        data = Path(path).read_bytes()
        req = urllib.request.Request(f"{server}/{topic}", data=data, method="PUT",
                                     headers={"Filename": Path(path).name, "Title": title.encode("ascii", "replace").decode()[:120],
                                              "Message": caption[:300], "Tags": "earth_americas"})
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as e:
        print("image push failed:", e, flush=True)
        return False


def _bestimage_loop():
    from zoneinfo import ZoneInfo
    local = ZoneInfo("America/New_York")
    time.sleep(900)
    while True:
        try:
            now = dt.datetime.now(local)
            today = f"{now:%Y-%m-%d}"
            done = BESTIMG_SENT.read_text().strip() if BESTIMG_SENT.exists() else ""
            if done != today and (now.hour, now.minute) >= (14, 40) and now.hour < 18:
                got = best_image(now)
                if got and push_image("Stratford from space today",
                                      f"GOES-19 at {got[1].astimezone(local):%-I:%M %p}, the clearest view of the day ({got[0] * 100:.0f}% cloud).", got[2]):
                    BESTIMG_SENT.write_text(today)
        except Exception as e:
            print("best image error:", e, flush=True)
        time.sleep(1800)


def start(procs, external_pids):
    for fn, args in ((_sampler, (procs["goesrecv"],)), (_watchdog, (procs, external_pids)), (_wx_loop, ()), (_trend_loop, ()), (_heartbeat_loop, ()), (_convect_loop, ()), (_monthly_loop, ()), (_extras_loop, ()), (_bestimage_loop, ())):
        threading.Thread(target=fn, args=args, daemon=True).start()
