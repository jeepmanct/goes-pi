"""Winter-storm early warning for the home point, from bulletins already in the EMWIN feed.

Stages, highest wins: alert (winter watch/warning/advisory in force for the home zone), likely (point forecast carries
snow in the next 48 h), watch (forecaster text for the next days mentions a nor'easter / significant snow), none."""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

STATE = Path.home() / "goes" / "winter-state.json"
WINTER_PHEN = {"WS", "BZ", "IS", "WW", "LE", "SQ"}
RANK = {"none": 0, "watch": 1, "likely": 2, "alert": 3}
STRONG = re.compile(r"nor'?easter|coastal (?:storm|low)|winter storm|blizzard|snow ?storm|heavy snow|significant snow|"
                    r"accumulating snow|major snow|snow accumulation|ice storm|wintry mix|freezing rain", re.I)
NEGATION = re.compile(r"\b(no|not|unlikely|without|rule[sd]? out|low (?:chance|probability))\b", re.I)
SEASON = (10, 4)       # October through April


def _mods():
    return sys.modules["goes_local"], sys.modules["goes_products"]


def in_season(now=None):
    m = time.localtime(now or time.time()).tm_mon
    return m >= SEASON[0] or m <= SEASON[1]


def _sentences(text):
    flat = re.sub(r"\s+", " ", text.replace("\r", ""))
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat) if s.strip()]


def _issued(text):
    m = re.search(r"\d{3,4} [AP]M [EC][SD]T \w{3} \w{3} \d+ \d{4}", text)
    return m.group(0) if m else ""


def text_signals(text, src):
    """Sentences from a forecaster product that talk about winter weather and aren't negated."""
    hits = []
    for s in _sentences(text):
        if not STRONG.search(s) or NEGATION.search(s):
            continue
        if re.search(r"\b(?:Connecticut|CT|Fairfield|southern CT|coastal|tri-state|area|region)\b", s, re.I) or STRONG.search(s):
            hits.append({"src": src, "text": s[:320]})
    return hits[:4]


def _snow_rows(local):
    """Snow/QPF slots in the next 48 h from the PFM block for the home zone."""
    out = []
    for r in local._newest("PFM", local.HOME["office"], n=3, hours=30):
        try:
            lines = local._pfm_block(local._text(r["name"]), local.HOME["zone"])
        except OSError:
            continue
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(("EDT 3hrly", "EST 3hrly"))), None)
        if idx is None:
            continue
        hdr, hours = lines[idx], lines[idx].split()[2:]
        md = re.findall(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}))?", lines[idx - 1])
        snow = next((ln for ln in lines[idx:] if ln.startswith("Snow 12hr")), "")
        if not snow or not md:
            return []
        off = hdr.index("3hrly")
        ends = [off + m.end() for m in re.finditer(r"\d\d", hdr[off:])]
        pf = local.point_forecast(rows=[r], keep_past=True) or {"points": []}
        times = [p["t"] for p in pf["points"] if p["step_h"] == 3][:len(ends)]
        for m in re.finditer(r"\S+", snow[len("Snow 12hr"):]):
            nums = [int(x) if x.isdigit() else 0 for x in re.split(r"-", m.group(0).replace("T", "0"))]
            if not nums:
                continue
            i = min(range(len(ends)), key=lambda k: abs(ends[k] - m.end() - len("Snow 12hr")))
            if abs(ends[i] - m.end() - len("Snow 12hr")) <= 3 and i < len(times):
                out.append({"t": times[i], "lo": min(nums), "hi": max(nums)})
        return out
    return []


def evaluate(now=None):
    local, products = _mods()
    now = now or time.time()
    alerts = [a for a in products.active_alerts(zones=[local.HOME["zone"]], now=now)
              if a["phen"] in WINTER_PHEN and a["level"] in "WAY"]
    snow = [s for s in _snow_rows(local) if now - 12 * 3600 <= s["t"] <= now + 48 * 3600 and s["hi"] >= 1]
    sig = []
    for cat, label in (("HWO", "Hazardous Weather Outlook"), ("AFD", "Forecast discussion")):
        for r in local._newest(cat, local.HOME["office"], n=1, hours=30):
            try:
                text = local._text(r["name"])
            except OSError:
                continue
            if cat == "HWO":     # only the multi-day section; day one is covered by alerts
                text = text.split(".DAYS TWO THROUGH SEVEN", 1)[-1] if ".DAYS TWO THROUGH SEVEN" in text else ""
            for h in text_signals(text, label):
                h["issued"] = _issued(local._text(r["name"]))
                sig.append(h)
    if alerts:
        stage = "alert"
    elif snow:
        stage = "likely"
    elif sig:
        stage = "watch"
    else:
        stage = "none"
    return {"stage": stage, "season": in_season(now), "alerts": [{"name": a["name"], "level": a["level"], "ends": a["ends"]} for a in alerts],
            "snow": snow, "signals": sig, "updated": now}


def headline(ev):
    st = ev["stage"]
    if st == "alert":
        return ev["alerts"][0]["name"] + (" (+%d more)" % (len(ev["alerts"]) - 1) if len(ev["alerts"]) > 1 else "") + " for Stratford"
    if st == "likely":
        top = max(ev["snow"], key=lambda s: s["hi"])
        return "Snow in the forecast: up to %d in. in a 12-hour period" % top["hi"]
    if st == "watch":
        return "Forecasters are discussing winter weather in the next several days"
    return "No winter weather in the forecast"


def _load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"rank": 0, "keys": [], "quiet_since": None}


def check(push, now=None):
    """Called every few minutes: pushes once per escalation (watch -> likely -> alert) and once per new alert."""
    ev = evaluate(now)
    if not ev["season"]:
        return ev
    st, now = _load_state(), now or time.time()
    rank = RANK[ev["stage"]]
    keys = [a["name"] for a in ev["alerts"]]
    new_alerts = [k for k in keys if k not in st["keys"]]
    if rank == 0:
        st["quiet_since"] = st["quiet_since"] or now
        if now - st["quiet_since"] > 12 * 3600:
            st["rank"], st["keys"] = 0, []
    else:
        st["quiet_since"] = None
        if rank > st["rank"] or new_alerts:
            body = headline(ev)
            if ev["stage"] == "watch":
                body += ". " + ev["signals"][0]["text"]
            elif ev["stage"] == "likely":
                when = time.strftime("%a %-I %p", time.localtime(min(s["t"] for s in ev["snow"] if s["hi"] >= 1)))
                body += ", starting around " + when + "."
            title = {"watch": "Winter storm watch (early)", "likely": "Snow likely", "alert": "Winter alert for Stratford"}[ev["stage"]]
            tags = "snowflake"
            push(title, body, key="winter-" + hashlib.md5((ev["stage"] + ",".join(keys)).encode()).hexdigest()[:8],
                 every=6 * 3600, priority="high" if rank >= 2 else "default", tags=tags)
        st["rank"], st["keys"] = max(rank, st["rank"]), keys
    STATE.write_text(json.dumps(st))
    return ev
