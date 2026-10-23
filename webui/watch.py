"""Background monitor for the GOES panel: signal history, watchdog and email alerts.

Started from app.py with start(procs). Everything here runs in daemon threads.
  - signal history: one averaged goesrecv sample per minute -> ~/goes/signal-history.jsonl
  - watchdog: restarts goesrecv/goesproc that died or stalled while the user wants them running
  - alerts: emails for no-signal, restarts, low disk / high temperature, and NWS warnings from EMWIN
"""
import datetime as dt
import json
import re
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


def start(procs, external_pids):
    for fn, args in ((_sampler, (procs["goesrecv"],)), (_watchdog, (procs, external_pids)), (_wx_loop, ())):
        threading.Thread(target=fn, args=args, daemon=True).start()
