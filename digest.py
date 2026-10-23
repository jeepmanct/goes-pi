#!/usr/bin/env python3
"""Morning Northeast US digest by email: latest GOES-19 pictures, a 24 h loop, and
forecast discussions from the NWS bulletins this station already receives (EMWIN).
No external AI or APIs; sends through iCloud SMTP with an app-specific password.

  digest.py --if-due          cron mode: send once per day from send_time (US Eastern) for 4.5 hours
  digest.py --out x.eml       build the message and save it instead of sending
  digest.py --force [--to a@b]  send now
"""
import argparse
import datetime as dt
import html
import io
import re
import smtplib
import ssl
import sys
import time
import tomllib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

GOES = Path("/home/pi/goes")
DATA = GOES / "data"
EMWIN = DATA / "emwin"
LOCAL = ZoneInfo("America/New_York")
STATE = GOES / ".digest-sent"
TRIED = GOES / ".digest-failed"
SEND_WINDOW_MIN = 270   # a due send is attempted for 4.5 hours after send_time

TS_RE = re.compile(r"_(20\d{2})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})Z?\.jpg$")
EMWIN_RE = re.compile(r"_C_KWIN_(\d{14})_")


# ---- pictures -----------------------------------------------------------------

def frames(view):
    """[(UTC time, path)] for a rendered view, oldest first."""
    out = []
    for p in (DATA / "goes19" / view).rglob("*.jpg"):
        m = TS_RE.search(p.name)
        if m:
            out.append((dt.datetime(*map(int, m.groups()), tzinfo=dt.timezone.utc), p))
    return sorted(out)


def jpeg(path, width=900, quality=82):
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def loop_gif(view, hours=24, width=640, max_bytes=1_800_000):
    now = dt.datetime.now(dt.timezone.utc)
    fr = [p for t, p in frames(view) if now - t <= dt.timedelta(hours=hours)]
    if len(fr) < 3:
        return None
    for n, w in ((24, width), (18, width), (14, 520), (10, 480)):
        pick = fr[:: max(1, -(-len(fr) // n))][-n:]
        ims = []
        for p in pick:
            im = Image.open(p).convert("RGB")
            ims.append(im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
                       .quantize(colors=96, method=Image.Quantize.MEDIANCUT))
        buf = io.BytesIO()
        ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=450, loop=0, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    return None


# ---- forecast text --------------------------------------------------------------

def latest_bulletin(prefix, site, max_age_h=30):
    """Newest EMWIN bulletin like AFD+BOX, as text, or None."""
    best = None
    now = dt.datetime.now(dt.timezone.utc)
    for p in EMWIN.glob(f"*-{prefix}{site}??.TXT"):
        m = EMWIN_RE.search(p.name)
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        if now - t <= dt.timedelta(hours=max_age_h) and (best is None or t > best[0]):
            best = (t, p)
    if not best:
        return None
    raw = best[1].read_bytes().decode("utf-8", errors="replace")   # EMWIN files use \r\r\n line endings
    return raw.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n"), best[0]


SECTION_RE = re.compile(r"^\.([A-Z][A-Z0-9 /,\-&]*?)\.\.\.(.*)$")
WANTED = ("WHAT HAS CHANGED", "KEY MESSAGES", "SYNOPSIS", "NEAR TERM", "SHORT TERM")


def paragraphs(lines):
    """Re-flow hard-wrapped bulletin lines into paragraphs; keep '- ' bullets separate."""
    out, cur = [], []
    for ln in lines + [""]:
        s = ln.strip()
        if not s or s.startswith("- ") or s.startswith("* "):
            if cur:
                out.append(" ".join(cur)); cur = []
            if s:
                cur = [s]
        else:
            cur.append(s)
    return [re.sub(r"\s+", " ", p) for p in out if p]


def parse_afd(text, budget=1500):
    """-> (office line, issued line, [(heading, [paragraphs])]) with the useful top sections."""
    lines = text.split("\n")
    office = next((l.strip() for l in lines if l.startswith("National Weather Service")), "")
    issued = next((l.strip() for l in lines if re.match(r"^\d{3,4} [AP]M ", l.strip())), "")
    secs, cur = [], None
    for ln in lines:
        m = SECTION_RE.match(ln.strip())
        if m:
            cur = [m.group(1).strip(), [m.group(2)] if m.group(2).strip() else []]
            secs.append(cur)
        elif ln.strip() == "&&":
            cur = None
        elif cur is not None:
            cur[1].append(ln)
    picked, used = [], 0
    for want in WANTED:
        for head, body in secs:
            if head.startswith(want):
                paras = paragraphs(body)
                if not paras:
                    continue
                room = budget - used
                if room < 150:
                    return office, issued, picked
                kept = []
                for p in paras:
                    if used + len(p) > budget and kept:
                        break
                    kept.append(p if len(p) <= room else p[:room].rsplit(" ", 1)[0] + "…")
                    used += len(kept[-1])
                picked.append((head.title(), kept))
                break
        if picked and want in ("KEY MESSAGES", "SYNOPSIS"):
            break   # key messages / synopsis is the summary; don't pile on more
    return office, issued, picked


SKY = {"SUNNY": "Sunny", "MOSUNNY": "Mostly sunny", "PTSUNNY": "Partly sunny", "CLEAR": "Clear",
       "FAIR": "Fair", "MOCLDY": "Mostly cloudy", "PTCLDY": "Partly cloudy", "CLOUDY": "Cloudy",
       "LGT RAIN": "Light rain", "RAIN": "Rain", "HVY RAIN": "Heavy rain", "FOG": "Fog", "HAZE": "Haze",
       "LGT SNOW": "Light snow", "SNOW": "Snow", "DRIZZLE": "Drizzle", "TSTRMS": "Thunderstorms"}
WIND_RE = re.compile(r"^([A-Z]+)(\d+)(?:G(\d+))?$")


def fmt_wind(w):
    m = WIND_RE.match(w)
    if not m:
        return "calm" if w in ("CALM", "0") else w
    d, sp, g = m.groups()
    return f"{d} {sp} mph" + (f", gusts {g}" if g else "")


def parse_roundup(text, stations):
    """Regional Weather Roundup -> (issued line, [(station, sky, temp F, dew pt, wind)]) for chosen stations."""
    lines = text.split("\n")
    issued = next((l.strip() for l in lines if re.match(r"^\d{3,4} [AP]M ", l.strip())), "")
    cols, found = None, {}
    for ln in lines:
        if ln.startswith("CITY ") and "TMP" in ln:
            cols = (ln.index("SKY/WX"), ln.index("TMP"), ln.index("DP"), ln.index("RH"), ln.index("WIND"), ln.index("PRES"))
            continue
        if ln.strip() == "$$":
            cols = None
        if not cols or len(ln) < cols[1] + 3:
            continue
        name = ln[:cols[0]].strip()
        want = next((w for w in stations if name.lower() == w.lower()), None)
        if want is None or want in found:
            continue
        sky, tmp, dp = ln[cols[0]:cols[1]].strip(), ln[cols[1]:cols[2]].strip(), ln[cols[2]:cols[3]].strip()
        wind = (ln[cols[4]:].split() or [""])[0]   # first token; the PRES column is not aligned with its header
        if not tmp.isdigit():
            continue
        found[want] = (name, SKY.get(sky, sky.title()) if sky != "N/A" else "", tmp, dp if dp.isdigit() else "", fmt_wind(wind))
    m = re.match(r"^(\d{1,2})(\d{2}) ([AP]M) (\w+) (\w+) (\w+) (\d+)", issued)
    if m:
        h, mi, ap, tz, dow, mon, d = m.groups()
        issued = f"{h}:{mi} {ap} {tz}, {dow.title()} {mon.title()} {d}"
    return issued, [found[w] for w in stations if w in found]


def roundup_block(cfg):
    got = latest_bulletin("RWR", cfg.get("roundup_office", "OKX"), max_age_h=4)
    if not got:
        return None
    issued, rows = parse_roundup(got[0], cfg.get("roundup_stations", []))
    return (issued, rows) if rows else None


def forecast_blocks(cfg):
    blocks, seen = [], 0
    for site in cfg["afd_offices"]:
        got = latest_bulletin("AFD", site)
        if not got:
            continue
        office, issued, secs = parse_afd(got[0])
        if secs:
            blocks.append((office or f"NWS {site}", issued, secs))
            seen += 1
        if seen >= cfg.get("max_afd", 2):
            break
    return blocks


# ---- message ------------------------------------------------------------------

def build(cfg, now_local):
    msg = EmailMessage()
    msg["Subject"] = f"GOES Northeast briefing, {now_local:%a %b %-d}"
    msg["From"] = f"{cfg['sender_name']} <{cfg['sender']}>"
    msg["To"] = ", ".join(cfg["recipients"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="goes.local")

    now = dt.datetime.now(dt.timezone.utc)
    imgs = []   # (cid, caption, bytes, subtype)
    ne = frames("northeast")
    fog = [(t, p) for t, p in frames("fog_northeast") if now - t <= dt.timedelta(hours=14)]
    ny = frames("nyc")
    if fog:
        imgs.append(("fog", "Overnight fog and low cloud (aqua/green = fog or low cloud)", fog[-1]))
    if ne and now - ne[-1][0] <= dt.timedelta(hours=8):
        imgs.append(("ne", "Northeast US, latest scan", ne[-1]))
    if ny and now - ny[-1][0] <= dt.timedelta(hours=8):
        imgs.append(("nyc", "New York City area, latest scan", ny[-1]))
    gif = loop_gif("northeast")
    blocks = forecast_blocks(cfg)
    now_obs = roundup_block(cfg)
    if not imgs and not blocks and not now_obs:
        return None

    def cap(t, text):
        return f"{text} - {t.astimezone(LOCAL):%a %-I:%M %p %Z}"

    link = share_link(cfg)

    # plain text
    txt = [f"GOES Northeast briefing - {now_local:%A, %B %-d}", ""]
    if link:
        txt += [f"Browse all the maps and the gallery: {link}", ""]
    for _cid, text, (t, _p) in imgs:
        txt.append(f"[picture] {cap(t, text)}")
    if gif:
        txt.append("[picture] Last 24 hours loop (animated)")
    if now_obs:
        txt += ["", f"RIGHT NOW - {now_obs[0]}", "-" * 40]
        txt += [f"{n}: {t} F, {sky.lower() or 'sky n/a'}" + (f", dew point {dp}" if dp else "") + f", wind {w}"
                for n, sky, t, dp, w in now_obs[1]]
    for office, issued, secs in blocks:
        txt += ["", f"{office} - {issued}", "-" * 40]
        for head, paras in secs:
            txt += [head.upper(), *paras, ""]
    txt += ["", "Imagery: NOAA GOES-19, received at home. Forecasts: National Weather Service.",
            "Reply 'unsubscribe' to stop these emails."]
    msg.set_content("\n".join(txt))

    # html
    h = ['<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Helvetica,Arial,sans-serif;'
         'color:#1b2430;line-height:1.45">',
         f'<h2 style="margin:0 0 4px">Northeast briefing</h2>'
         f'<div style="color:#667;margin-bottom:14px">{now_local:%A, %B %-d}</div>']
    if link:
        h.append(f'<p style="margin:0 0 6px"><a href="{html.escape(link)}" style="display:inline-block;background:#1f6feb;'
                 'color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;font-weight:600">'
                 'Browse all maps &amp; gallery</a></p>')
    for cid, text, (t, _p) in imgs:
        h.append(f'<p style="margin:14px 0 4px"><img src="cid:{cid}" style="width:100%;border-radius:6px" alt="{html.escape(text)}"><br>'
                 f'<span style="font-size:13px;color:#556">{html.escape(cap(t, text))}</span></p>')
    if gif:
        h.append('<p style="margin:14px 0 4px"><img src="cid:loop" style="width:100%;border-radius:6px" alt="24 hour loop"><br>'
                 '<span style="font-size:13px;color:#556">Last 24 hours, oldest to newest</span></p>')
    if now_obs:
        h.append('<h3 style="margin:22px 0 2px">Right now</h3>'
                 f'<div style="font-size:13px;color:#667;margin-bottom:6px">{html.escape(now_obs[0])} (NWS New York)</div>'
                 '<table style="border-collapse:collapse;width:100%;font-size:14px">'
                 '<tr style="text-align:left;color:#667;font-size:12px"><th style="padding:3px 6px 3px 0">Station</th>'
                 '<th style="padding:3px 6px">Temp</th><th style="padding:3px 6px">Sky</th><th style="padding:3px 0 3px 6px">Wind</th></tr>')
        for n, sky, t, dp, w in now_obs[1]:
            h.append(f'<tr style="border-top:1px solid #e3e6ee"><td style="padding:4px 6px 4px 0">{html.escape(n)}</td>'
                     f'<td style="padding:4px 6px;font-weight:600">{t}&deg;</td>'
                     f'<td style="padding:4px 6px">{html.escape(sky)}</td>'
                     f'<td style="padding:4px 0 4px 6px">{html.escape(w)}</td></tr>')
        h.append('</table>')
    for office, issued, secs in blocks:
        h.append(f'<h3 style="margin:22px 0 2px">{html.escape(office)}</h3>'
                 f'<div style="font-size:13px;color:#667;margin-bottom:6px">{html.escape(issued)}</div>')
        for head, paras in secs:
            h.append(f'<div style="font-weight:600;margin-top:8px">{html.escape(head)}</div>')
            h += [f'<p style="margin:4px 0">{html.escape(p)}</p>' for p in paras]
    h.append('<hr style="border:0;border-top:1px solid #ccd;margin:22px 0 8px">'
             '<div style="font-size:12px;color:#778">Imagery: NOAA GOES-19, received at home. '
             'Forecasts: National Weather Service. Reply "unsubscribe" to stop these emails.</div></div>')
    msg.add_alternative("<html><body>" + "".join(h) + "</body></html>", subtype="html")
    body = msg.get_payload()[1]
    for cid, _text, (_t, p) in imgs:
        body.add_related(jpeg(p), "image", "jpeg", cid=f"<{cid}>")
    if gif:
        body.add_related(gif, "image", "gif", cid="<loop>")
    return msg


def share_link(cfg):
    """Public read-only page URL (mail.conf public_url + the secret token), or None if not set up."""
    base = str(cfg.get("public_url", "")).rstrip("/")
    try:
        tok = (GOES / "share-token.txt").read_text().strip()
    except OSError:
        return None
    return f"{base}/s/{tok}/" if base.startswith("https://") and tok else None


# ---- one picture by email (from the app's image viewer) ------------------------

VIEW_LABELS = {
    "globe": "Full disk globe", "northeast": "Northeast US", "eastus": "East Coast US",
    "nyc": "New York City area", "namer": "North America", "gulf": "Gulf and Caribbean",
    "samer": "South America", "fd": "Full disk", "m1": "Mesoscale region 1", "m2": "Mesoscale region 2",
}
CHANNELS = {
    "01": "blue visible", "02": "red visible", "03": "near-IR 'veggie'", "07": "shortwave IR",
    "08": "upper-level water vapor", "09": "mid-level water vapor", "10": "lower-level water vapor",
    "13": "clean longwave IR", "14": "longwave IR", "15": "dirty longwave IR",
}
ABI_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")
CHAN_RE = re.compile(r"-M\dC(\d{2})_")
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif"}


def describe(rel):
    """Label and capture time for a picture under data/, given its path relative to data/."""
    base = DATA.resolve()
    p = (base / rel.lstrip("/")).resolve()
    if base not in p.parents or p.suffix.lower() not in IMG_EXT or not p.is_file():
        raise ValueError("not an image in the data folder")
    parts = p.relative_to(base).parts
    name = p.name
    t = None
    m = ABI_RE.search(name)
    if m:
        y, doy, hh, mm, ss = map(int, m.groups())
        t = dt.datetime(y, 1, 1, hh, mm, ss, tzinfo=dt.timezone.utc) + dt.timedelta(days=doy - 1)
    elif (m := TS_RE.search(name)):
        t = dt.datetime(*map(int, m.groups()), tzinfo=dt.timezone.utc)
    elif (m := EMWIN_RE.search(name)):
        t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    if t is None:
        t = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
    view = parts[1] if parts[0] == "goes19" and len(parts) > 2 else parts[0]
    label = VIEW_LABELS.get(view)
    if view.startswith("fog_"):
        label = "Night fog, " + VIEW_LABELS.get(view[4:], view[4:])
    if view == "emwin":
        label = "NWS weather graphic"
    c = CHAN_RE.search(name)
    detail = ""
    if c:
        detail = f"channel {int(c.group(1))}" + (f" ({CHANNELS[c.group(1)]})" if c.group(1) in CHANNELS else "")
        label = f"{label or view} - {detail}"
    local = t.astimezone(LOCAL)
    return {"path": p, "label": label or view, "detail": detail, "when_utc": t, "when_local": local,
            "when": f"{local:%a %b %-d, %-I:%M %p %Z}", "name": name}


def build_image_message(cfg, rel, to, note=""):
    d = describe(rel)
    msg = EmailMessage()
    msg["Subject"] = f"GOES-19: {d['label']} - {d['when']}"
    msg["From"] = f"{cfg['sender_name']} <{cfg['sender']}>"
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="goes.local")
    note = " ".join(note.split())[:500]
    cap = [("View", d["label"]), ("Time", f"{d['when']}  ({d['when_utc']:%Y-%m-%d %H:%M} UTC)"),
           ("File", d["name"])]
    txt = [note, ""] if note else []
    txt += [f"{d['label']}", f"{d['when']} ({d['when_utc']:%Y-%m-%d %H:%M} UTC)", "",
            "Imagery: NOAA GOES-19, received at home."]
    msg.set_content("\n".join(txt))
    h = ['<div style="max-width:720px;margin:0 auto;font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1b2430;line-height:1.45">']
    if note:
        h.append(f'<p style="margin:0 0 14px;font-size:16px">{html.escape(note)}</p>')
    h.append(f'<h3 style="margin:0 0 6px">{html.escape(d["label"])}</h3>'
             '<img src="cid:pic" style="width:100%;border-radius:6px" alt="GOES-19 image">'
             '<table style="border-collapse:collapse;margin-top:8px;font-size:14px">')
    for k, v in cap:
        h.append(f'<tr><td style="padding:2px 12px 2px 0;color:#667">{k}</td><td>{html.escape(v)}</td></tr>')
    h.append('</table><div style="font-size:12px;color:#778;margin-top:12px">Imagery: NOAA GOES-19, received at home.</div></div>')
    msg.add_alternative("<html><body>" + "".join(h) + "</body></html>", subtype="html")
    p = d["path"]
    if p.suffix.lower() in (".jpg", ".jpeg") and p.stat().st_size <= 1_500_000:
        data, sub = p.read_bytes(), "jpeg"
    elif p.suffix.lower() == ".gif" and p.stat().st_size <= 1_500_000:
        data, sub = p.read_bytes(), "gif"
    else:
        data, sub = jpeg(p, width=2000, quality=85), "jpeg"
    msg.get_payload()[1].add_related(data, "image", sub, cid="<pic>")
    return msg


def send(cfg, msg):
    pw = Path(cfg["password_file"]).read_text().strip()
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=60) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(cfg["username"], pw)
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--if-due", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", help="save the message to this .eml file instead of sending")
    ap.add_argument("--to", nargs="*", help="override recipients")
    ap.add_argument("--image", help="email just this picture (path relative to data/), to --to")
    ap.add_argument("--note", default="", help="message to put above the picture (with --image)")
    a = ap.parse_args()

    cfg = tomllib.loads((GOES / "mail.conf").read_text())
    if a.to:
        cfg["recipients"] = a.to
    now_local = dt.datetime.now(LOCAL)

    if a.image:
        msg = build_image_message(cfg, a.image, cfg["recipients"], a.note)
        if a.out:
            Path(a.out).write_bytes(bytes(msg))
            print(f"saved {a.out} ({len(bytes(msg)) // 1024} KB)")
            return 0
        send(cfg, msg)
        print(f"sent to {', '.join(cfg['recipients'])} ({len(bytes(msg)) // 1024} KB)")
        return 0

    if a.if_due and not a.force and not a.out:
        hh, mm = map(int, cfg["send_time"].split(":"))
        start = hh * 60 + mm
        if not (start <= now_local.hour * 60 + now_local.minute < start + SEND_WINDOW_MIN):
            return 0
        if STATE.exists() and STATE.read_text().strip() == f"{now_local:%Y-%m-%d}":
            return 0

    if a.if_due and not a.force and not a.out:
        if not Path(cfg["password_file"]).exists():
            return 0   # not set up yet
        if TRIED.exists() and time.time() - TRIED.stat().st_mtime < 3000:
            return 0   # a send failed within the last ~50 minutes; don't hammer iCloud
    msg = build(cfg, now_local)
    if msg is None:
        print("nothing to send (no recent images or forecasts)")
        return 0
    if a.out:
        Path(a.out).write_bytes(bytes(msg))
        print(f"saved {a.out} ({len(bytes(msg)) // 1024} KB)")
        return 0
    try:
        send(cfg, msg)
    except (OSError, smtplib.SMTPException) as e:
        if a.if_due:
            TRIED.touch()
        print(f"send failed: {e}")
        return 1
    TRIED.unlink(missing_ok=True)
    if a.if_due:
        STATE.write_text(f"{now_local:%Y-%m-%d}\n")
    print(f"sent to {', '.join(cfg['recipients'])} ({len(bytes(msg)) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
