"""Atlantic tropical weather from NHC products already in the EMWIN feed: the 7-day outlook and active-storm advisories."""
import re
import sys
import time

SEASON = ((6, 1), (11, 30))
CHANCE = re.compile(r"Formation chance through (\d+) (hours|days)\.{3}(\w+)\.{3}(\d+) percent\.?", re.I)


def in_season(now=None):
    t = time.localtime(now or time.time())
    return SEASON[0] <= (t.tm_mon, t.tm_mday) <= SEASON[1]


def _p():
    return sys.modules["goes_products"]


def _flat(s):
    return re.sub(r"\s+", " ", s.replace("\r", "")).strip()


def parse_outlook(text):
    """{'issued', 'none': bool, 'areas': [{'text', 'two_day', 'seven_day'}]} from a TWOAT bulletin."""
    issued = re.search(r"\d{3,4} [AP]M [EC][SD]T \w{3} \w{3} \d+ \d{4}", text)
    body = text.split("Gulf of America:", 1)[-1] if "Gulf of America:" in text else text
    body = body.split("$$")[0]
    out = {"issued": issued.group(0) if issued else "", "none": bool(re.search(r"formation is not expected", body, re.I)), "areas": []}
    for para in re.split(r"\n\s*\n", body):
        flat = _flat(para)
        pcts = CHANCE.findall(flat)
        if not pcts:
            continue
        two = next((int(p[3]) for p in pcts if p[0] == "48" and p[1].lower() == "hours"), None)
        seven = next((int(p[3]) for p in pcts if p[0] == "7" and p[1].lower() == "days"), None)
        out["areas"].append({"text": CHANCE.sub("", flat).strip()[:400], "two_day": two, "seven_day": seven})
    return out


def parse_advisory(text):
    """Storm name, type and key lines from a public advisory (TCPAT)."""
    flat = text.replace("\r", "")
    m = re.search(r"((?:Post-Tropical Cyclone|Potential Tropical Cyclone|Subtropical Storm|Tropical Depression|Tropical Storm|Hurricane|Major Hurricane|Tropical Cyclone)\s+[\w-]+)\s+Advisory Number\s+(\w+)", flat, re.I)
    if not m:
        return None
    kinds = ("LOCATION", "MAXIMUM SUSTAINED WINDS", "PRESENT MOVEMENT", "MINIMUM CENTRAL PRESSURE")
    keys = {}
    for k in kinds:
        mm = re.search(k + r"\.{3}(.*?)(?:\n\n|\n[A-Z ]{6,}\.{3})", flat, re.S)
        if mm:
            keys[k.title()] = _flat(mm.group(1))
    return {"title": _flat(m.group(1)), "number": m.group(2), "keys": keys}


def active_storms(now=None, hours=18):
    local = sys.modules["goes_local"]
    now = now or time.time()
    seen = {}
    for r in _p()._rows():
        if r["t"] < now - hours * 3600:
            break
        if r["office"] != "NHC" or not r["head"].startswith("WTNT3"):
            continue
        try:
            adv = parse_advisory(local._text(r["name"]))
        except OSError:
            continue
        if adv and adv["title"] not in seen:           # rows are newest first
            adv["t"] = r["t"]
            seen[adv["title"]] = adv
    return list(seen.values())


def evaluate(now=None):
    local = sys.modules["goes_local"]
    now = now or time.time()
    outlook = None
    for r in _p()._rows():
        if r["office"] == "NHC" and r["head"] == "ABNT20":
            try:
                outlook = {**parse_outlook(local._text(r["name"])), "t": r["t"]}
            except OSError:
                continue
            break
    storms = active_storms(now)
    return {"season": in_season(now), "outlook": outlook, "storms": storms,
            "headline": storms[0]["title"] if storms else ("Tropical formation is not expected in the next 7 days" if (outlook or {}).get("none")
                                                          else "Areas being watched in the Atlantic" if (outlook or {}).get("areas") else "No outlook available"),
            "updated": now}


def check(push, now=None):
    ev = evaluate(now)
    for s in ev["storms"]:
        body = ". ".join(f"{k}: {v}" for k, v in s["keys"].items()) or "See the Tropical tab for details."
        push("Atlantic: " + s["title"], body, key="trop-" + s["title"].lower().replace(" ", "-"), every=24 * 3600,
             priority="default", tags="cyclone")
    for i, a in enumerate((ev["outlook"] or {}).get("areas", [])):
        if (a["two_day"] or 0) >= 60:
            push("Atlantic: high chance of a tropical storm forming", a["text"], key=f"trop-area-{i}-{a['text'][:30]}", every=48 * 3600,
                 priority="default", tags="cyclone")
    return ev
