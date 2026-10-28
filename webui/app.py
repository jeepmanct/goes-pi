#!/usr/bin/env python3
"""GOES control panel: start/stop goesrecv + goesproc, live signal stats,
config editing and image browsing. Standard library only (Pillow optional,
used for thumbnails)."""

import datetime
import gzip
import hashlib
import base64
import collections
import hmac
import json
import mimetypes
import os
import re
import secrets
import smtplib
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image
except ImportError:
    Image = None

GZIP_SUFFIXES = {".html", ".css", ".js", ".json", ".svg"}      # the app's own files only (ETag + no-cache)
_gz_cache = {}          # path -> (mtime_ns, gzipped bytes)


def _load(name):
    """Load a sibling module by path (the service runs python -I, which drops the script dir from sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watch = _load("watch")
share = _load("share")
sysstats = _load("sysstats")
products = _load("products")
sys.modules["goes_products"] = products        # one shared product index for the panel, local.py and watch.py
local = _load("local")
tides = _load("tides")
winter = _load("winter")
tropical = _load("tropical")
for _n, _m in (("tides", tides), ("winter", winter), ("tropical", tropical)):
    sys.modules["goes_" + _n] = _m        # watch.py's background checks share these instances
sys.modules["goes_local"] = local
skywarn = _load("skywarn")
sys.modules["goes_skywarn"] = skywarn
HOME = Path.home()
GOES_DIR = HOME / "goes"
DATA_DIR = GOES_DIR / "data"
WEB_DIR = Path(__file__).resolve().parent
STATIC = WEB_DIR / "static"
THUMBS = WEB_DIR / "thumbs"
PASSWORD_FILE = WEB_DIR / "password.txt"
RECV_CONF = GOES_DIR / "goesrecv.conf"
PROC_CONF = GOES_DIR / "goesproc.conf"
PANEL_STARTED = time.time()
_pf_cache = {}
PORT = int(os.environ.get("GOES_UI_PORT", "8080"))
CERT = Path(os.environ.get("GOES_UI_CERT", WEB_DIR / "tls" / "fullchain.pem"))
KEY = Path(os.environ.get("GOES_UI_KEY", WEB_DIR / "tls" / "privkey.pem"))
MAX_FAILS = 5
LOCKOUT_SECS = 900
FAILS = {}  # ip -> (count, first_fail_time)
FAILS_LOCK = threading.Lock()

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif"}

COMMANDS = {
    "goesrecv": ["goesrecv", "-v", "-i", "1", "--config", str(RECV_CONF)],
    "goesproc": ["goesproc", "-c", str(PROC_CONF), "-m", "packet",
                 "--subscribe", "tcp://127.0.0.1:5004"],
}

MONITOR_RE = re.compile(
    r"\[monitor\]\s+gain:\s*(?P<gain>\S+),\s*freq:\s*(?P<freq>\S+),\s*"
    r"omega:\s*(?P<omega>\S+),\s*vit\(avg\):\s*(?P<vit>\S+),\s*"
    r"rs\(sum\):\s*(?P<rs>\S+),\s*packets:\s*(?P<ok>\S+),\s*drops:\s*(?P<drops>\S+)")


def num(s):
    try:
        v = float(s)
        return v if v == v else None  # NaN -> None
    except ValueError:
        return None


class Proc:
    """A managed child process with a rolling log."""

    def __init__(self, name):
        self.name = name
        self.p = None
        self.started = None
        self.log = collections.deque(maxlen=600)
        self.history = collections.deque(maxlen=900)  # goesrecv monitor lines
        self.lock = threading.Lock()
        self.wanted = False  # the user (or autostart) wants it running; the watchdog restarts it if it dies

    def running(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        with self.lock:
            if self.running():
                return "already running"
            self.wanted = True
            if external_pids(self.name):
                return "already running outside the UI (systemd or a terminal)"
            self.history.clear()
            self.p = subprocess.Popen(
                COMMANDS[self.name], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                cwd=str(GOES_DIR), start_new_session=True)
            self.started = time.time()
            self.log.append(f"--- started {self.name} pid {self.p.pid} ---")
            threading.Thread(target=self._read, args=(self.p,), daemon=True).start()
            return "started"

    def _read(self, p):
        for line in p.stdout:
            line = line.rstrip("\n")
            self.log.append(line)
            m = MONITOR_RE.search(line)
            if m and self.name == "goesrecv":
                d = {k: num(v) for k, v in m.groupdict().items()}
                d["t"] = time.time()
                self.history.append(d)
        p.wait()
        self.log.append(f"--- {self.name} exited with code {p.returncode} ---")

    def stop(self):
        with self.lock:
            self.wanted = False
            if self.running():
                os.killpg(self.p.pid, signal.SIGTERM)
                try:
                    self.p.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.p.pid, signal.SIGKILL)
                return "stopped"
        pids = external_pids(self.name)
        if pids:
            subprocess.run(["pkill", "-x", self.name])
            return "stopped outside process"
        return "not running"


def external_pids(name):
    r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


PROCS = {n: Proc(n) for n in COMMANDS}


def status():
    out = {}
    for n, pr in PROCS.items():
        run = pr.running()
        ext = (not run) and bool(external_pids(n))
        out[n] = {"running": run or ext, "external": ext,
                  "uptime": int(time.time() - pr.started) if run else None}
    du = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else HOME)
    temp = None
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        pass
    out["panel"] = {"started": PANEL_STARTED}
    out["system"] = {"disk_free": du.free, "disk_total": du.total,
                     "cpu_temp": temp, "load": os.getloadavg()[0],
                     "time": time.time()}
    return out


# ---- config editing -------------------------------------------------------

FIELDS = {  # ui name -> (section, key, type)
    "source": ("demodulator", "source", "str"),
    "frequency": ("rtlsdr", "frequency", "int"),
    "sample_rate": ("rtlsdr", "sample_rate", "int"),
    "gain": ("rtlsdr", "gain", "int"),
    "bias_tee": ("rtlsdr", "bias_tee", "bool"),
    "device_index": ("rtlsdr", "device_index", "int"),
}


def toml_value(v, typ):
    if typ == "bool":
        return "true" if v in (True, "true", "True", 1) else "false"
    if typ == "int":
        return str(int(float(v)))
    if v not in ("rtlsdr", "airspy", "nanomsg"):
        raise ValueError(f"unknown source {v!r}")
    return f'"{v}"'


def set_key(text, section, key, value):
    lines = text.split("\n")
    sec = None
    last_in_sec = None
    for i, ln in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]\s*$", ln)
        if m:
            sec = m.group(1)
            if sec == section:
                last_in_sec = i
            continue
        if sec == section:
            if re.match(rf"\s*{re.escape(key)}\s*=", ln):
                lines[i] = f"{key} = {value}"
                return "\n".join(lines)
            if ln.strip():
                last_in_sec = i
    if last_in_sec is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {value}\n"
    lines.insert(last_in_sec + 1, f"{key} = {value}")
    return "\n".join(lines)


def read_config():
    text = RECV_CONF.read_text()
    cfg = tomllib.loads(text)
    out = {}
    for name, (sec, key, _) in FIELDS.items():
        out[name] = cfg.get(sec, {}).get(key)
    return out


def save_text(path, text):
    tomllib.loads(text)  # raises on invalid TOML
    if path.exists():
        shutil.copy2(path, str(path) + ".bak")
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(text)
    os.replace(tmp, path)


def check_proc_dirs(text):
    """goesproc writes wherever 'directory' points; keep it under the data dir."""
    base = str(DATA_DIR.resolve())

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "directory" and isinstance(v, str):
                    if not os.path.normpath(v).startswith(base + os.sep) or ".." in v.split("/"):
                        raise ValueError(f"directory {v!r} must be inside {base}")
                else:
                    walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(tomllib.loads(text))


def update_config(changes):
    text = RECV_CONF.read_text()
    for name, v in changes.items():
        if name not in FIELDS:
            raise ValueError(f"unknown field {name}")
        sec, key, typ = FIELDS[name]
        text = set_key(text, sec, key, toml_value(v, typ))
    save_text(RECV_CONF, text)


# ---- files ---------------------------------------------------------------

def safe_path(rel):
    base = DATA_DIR.resolve()
    p = (base / rel.lstrip("/")).resolve()
    if p != base and base not in p.parents:
        raise PermissionError("path outside data directory")
    hidden = base / "backup"          # state backups (passwords, mail settings) are never served through the panel
    if p == hidden or hidden in p.parents:
        raise PermissionError("not available")
    return p


def list_dir(rel):
    p = safe_path(rel)
    dirs, files = [], []
    for e in sorted(p.iterdir(), key=lambda e: e.name, reverse=True):
        if e.name == "backup" and p == DATA_DIR.resolve():
            continue
        st = e.stat()
        item = {"name": e.name, "mtime": st.st_mtime,
                "path": str(e.relative_to(DATA_DIR.resolve()))}
        if e.is_dir():
            dirs.append(item)
        else:
            item["size"] = st.st_size
            item["image"] = e.suffix.lower() in IMAGE_EXT
            files.append(item)
    rel_p = str(p.relative_to(DATA_DIR.resolve()))
    return {"path": "" if rel_p == "." else rel_p, "dirs": dirs, "files": files}


ABI_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")  # GOES scan start: year, day-of-year, hhmmss
CHAN_RE = re.compile(r"-M\dC(\d{2})_")
TS_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})[T_-]?(\d{2})(\d{2})(\d{2})")


def gallery(offset=0, limit=60, region=""):
    """Every image under the data dir, newest capture first. The capture time
    is parsed from the filename (UTC) and falls back to the file mtime."""
    base = DATA_DIR.resolve()
    items = []
    for root, _dirs, names in os.walk(base):
        for n in names:
            if os.path.splitext(n)[1].lower() not in IMAGE_EXT:
                continue
            fp = os.path.join(root, n)
            rel = os.path.relpath(fp, base)
            parts = rel.split(os.sep)
            reg = parts[1] if len(parts) > 2 else parts[0]
            if region and reg != region:
                continue
            t = None
            try:
                m = ABI_RE.search(n)
                if m:
                    y, doy, hh, mm, ss = map(int, m.groups())
                    t = (datetime.datetime(y, 1, 1, hh, mm, ss, tzinfo=datetime.timezone.utc)
                         + datetime.timedelta(days=doy - 1)).timestamp()
                else:
                    m = TS_RE.search(n)
                    if m:
                        t = datetime.datetime(*map(int, m.groups()),
                                              tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                t = None
            c = CHAN_RE.search(n)
            items.append({"path": rel, "name": n, "region": reg,
                          "chan": c.group(1) if c else "",
                          "t": t if t is not None else os.stat(fp).st_mtime})
    items.sort(key=lambda i: i["t"], reverse=True)
    return {"total": len(items), "items": items[offset:offset + limit]}


def dir_size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


THUMB_WIDTHS = (360, 720)       # 360 = gallery grid, 720 = time-lapse frames


def thumb(rel, width=360):
    width = width if width in THUMB_WIDTHS else 360
    src = safe_path(rel)
    if Image is None or src.suffix.lower() not in IMAGE_EXT:
        return src
    dst = THUMBS / f"{hashlib.md5(str(src).encode()).hexdigest()}_{int(src.stat().st_mtime)}_{width}.jpg"
    if not dst.exists():
        THUMBS.mkdir(exist_ok=True)
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im.draft("RGB", (width * 2, width * 2))
            im = im.convert("RGB")
            im.thumbnail((width, width))
            tmp = dst.with_name(dst.name + ".tmp")
            im.save(tmp, "JPEG", quality=80)
            tmp.replace(dst)
    return dst


def prewarm_thumbs():
    """Background: render thumbnails for new images before anyone asks, so the gallery and the
    time-lapse never wait on a 5424 px PNG. Rendered views (JPEG) get both sizes; raw scans only the grid size."""
    while True:
        try:
            items = gallery(0, 400)["items"]
            made = 0
            for i, x in enumerate(items):
                view = x["region"] not in ("fd", "m1", "m2") and x["name"].lower().endswith((".jpg", ".jpeg"))
                if not view and i >= 40:
                    continue
                for w in (THUMB_WIDTHS if view else THUMB_WIDTHS[:1]):
                    try:
                        src = safe_path(x["path"])
                        dst = THUMBS / f"{hashlib.md5(str(src).encode()).hexdigest()}_{int(src.stat().st_mtime)}_{w}.jpg"
                        if not dst.exists():
                            thumb(x["path"], w)
                            made += 1
                            time.sleep(0.2)
                    except (OSError, ValueError):
                        pass
                if made >= 80:
                    break
            cutoff = time.time() - 9 * 86400          # sources are pruned after ~7 days
            for p in THUMBS.glob("*.jpg"):
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except Exception as e:                        # never let the warmer die
            print("thumb prewarm:", e, flush=True)
        time.sleep(60)


# ---- morning email settings ------------------------------------------------

MAIL_CONF = GOES_DIR / "mail.conf"
DIGEST = GOES_DIR / "digest.py"
DIGEST_STATE = GOES_DIR / ".digest-sent"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MAX_RECIPIENTS = 10
SEND_WINDOW_MIN = 270          # keep in step with digest.py
_mail_test = {"t": 0.0}


def mail_status():
    cfg = tomllib.loads(MAIL_CONF.read_text())
    tz = ZoneInfo("America/New_York")
    now = datetime.datetime.now(tz)
    hh, mm = map(int, cfg["send_time"].split(":"))
    st = DIGEST_STATE.read_text().split() if DIGEST_STATE.exists() else []
    sent_today = bool(st) and st[0] == f"{now:%Y-%m-%d}" and "partial" not in st
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    in_window = nxt <= now < nxt + datetime.timedelta(minutes=SEND_WINDOW_MIN)
    if sent_today or (now >= nxt and not in_window):
        nxt += datetime.timedelta(days=1)
    elif in_window:
        nxt = now          # due now; the next 10-minute check will send it
    last = st[0] if st else None
    return {"sender": cfg["sender"], "recipients": cfg["recipients"], "send_time": cfg["send_time"],
            "password_ok": Path(cfg["password_file"]).exists(), "last_sent": last,
            "next_send": nxt.strftime("%a %b %-d, %-I:%M %p %Z") + (" (due now)" if in_window and not sent_today else ""),
            "max": MAX_RECIPIENTS}


def push_topic():
    a = tomllib.loads(MAIL_CONF.read_text()).get("alerts", {})
    return str(a.get("ntfy_topic", ""))


def set_push_topic(topic):
    topic = str(topic or "").strip()
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
        raise ValueError("use 6-64 letters, numbers, - or _ (pick something hard to guess)")
    text = MAIL_CONF.read_text()
    line = f'ntfy_topic = "{topic}"'
    if re.search(r"(?m)^ntfy_topic\s*=", text):
        text = re.sub(r'(?m)^ntfy_topic\s*=.*$', line, text)
    elif re.search(r"(?m)^\[alerts\]\s*$", text):
        text = re.sub(r"(?m)^(\[alerts\]\s*)$", lambda m: m.group(1) + "\n" + line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n[alerts]\n" + line + "\n"
    tomllib.loads(text)   # refuse to write anything that isn't valid TOML
    save_text(MAIL_CONF, text)


def share_status():
    cfg = tomllib.loads(MAIL_CONF.read_text())
    return {"public_url": str(cfg.get("public_url", "")).rstrip("/"), "links": share.summary(),
            "recipients": cfg["recipients"], "topic": push_topic()}


def forecast_data():
    return digest_module().forecast_full(tomllib.loads(MAIL_CONF.read_text()))


def mail_update(recipients=None, send_time=None):
    text = MAIL_CONF.read_text()
    if recipients is not None:
        text, n = re.subn(r'(?m)^(recipients\s*=\s*)\[[^\]]*\]', lambda m: m.group(1) + json.dumps(recipients), text)
        if n != 1:
            raise ValueError("could not find recipients in mail.conf")
    if send_time is not None:
        text, n = re.subn(r'(?m)^(send_time\s*=\s*)"[^"]*"', lambda m: m.group(1) + json.dumps(send_time), text)
        if n != 1:
            raise ValueError("could not find send_time in mail.conf")
    save_text(MAIL_CONF, text)


def mail_recipients_change(add=None, remove=None):
    cur = tomllib.loads(MAIL_CONF.read_text())["recipients"]
    if add is not None:
        add = str(add).strip()
        if not EMAIL_RE.match(add) or len(add) > 120:
            raise ValueError("that doesn't look like a valid email address")
        if add.lower() in [r.lower() for r in cur]:
            raise ValueError("already on the list")
        if len(cur) >= MAX_RECIPIENTS:
            raise ValueError(f"the list is limited to {MAX_RECIPIENTS} addresses")
        cur = cur + [add]
    if remove is not None:
        new = [r for r in cur if r.lower() != str(remove).strip().lower()]
        if len(new) == len(cur):
            raise ValueError("that address isn't on the list")
        if not new:
            raise ValueError("keep at least one recipient")
        cur = new
    mail_update(recipients=cur)
    return cur


def mail_set_time(value):
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(value))
    if not m or int(m.group(1)) > 19:
        raise ValueError("pick a time between 00:00 and 19:59")
    mail_update(send_time=value)


_digest_mod = {}
_image_sends = []       # monotonic times of recent picture emails, for rate limiting


def digest_module():
    """digest.py as a module (the panel runs with -I, so load it by path)."""
    if "m" not in _digest_mod:
        import importlib.util
        spec = importlib.util.spec_from_file_location("goes_digest", DIGEST)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _digest_mod["m"] = mod
    return _digest_mod["m"]


def mail_describe(rel):
    d = digest_module().describe(rel)
    return {"label": d["label"], "when": d["when"], "name": d["name"]}


def mail_image(rel, to, note):
    if not isinstance(to, list) or not 1 <= len(to) <= 5:
        raise ValueError("choose between 1 and 5 recipients")
    addrs = []
    for a in to:
        a = str(a).strip()
        if not EMAIL_RE.match(a) or len(a) > 120:
            raise ValueError(f"{a[:60]!r} doesn't look like a valid email address")
        if a.lower() not in [x.lower() for x in addrs]:
            addrs.append(a)
    now = time.monotonic()
    _image_sends[:] = [t for t in _image_sends if now - t < 3600]
    if _image_sends and now - _image_sends[-1] < 5:
        raise ValueError("one moment - a picture was just sent")
    if len(_image_sends) >= 20:
        raise ValueError("limit reached: 20 picture emails per hour")
    cfg = tomllib.loads(MAIL_CONF.read_text())
    if not Path(cfg["password_file"]).exists():
        raise ValueError("email isn't set up: the password file is missing")
    dm = digest_module()
    msg = dm.build_image_message(cfg, rel, addrs, str(note or ""))
    try:
        dm.send(cfg, msg)
    except (OSError, smtplib.SMTPException) as e:
        raise ValueError(f"send failed: {e}")
    _image_sends.append(now)
    print(f"emailed {rel} to {len(addrs)} recipient(s)", flush=True)
    return f"Sent to {len(addrs)} recipient{'s' if len(addrs) != 1 else ''}."


def mail_test(to):
    cur = tomllib.loads(MAIL_CONF.read_text())["recipients"]
    if to.lower() not in [r.lower() for r in cur]:
        raise ValueError("test emails can only go to addresses on the list")
    wait = 60 - (time.time() - _mail_test["t"])
    if wait > 0:
        raise ValueError(f"please wait {int(wait) + 1} seconds between test emails")
    _mail_test["t"] = time.time()
    r = subprocess.run([sys.executable, "-I", str(DIGEST), "--force", "--to", to],
                       capture_output=True, text=True, timeout=180)
    out = (r.stdout + r.stderr).strip().splitlines()
    return (out[-1] if out else "no output") if r.returncode == 0 else "failed: " + (out[-1] if out else "unknown error")


# ---- time-lapse GIF download --------------------------------------------

GIF_DIR = THUMBS / "gifs"
GIF_LOCK = threading.Lock()


def make_gif(view, hours):
    """An animated GIF of a rendered view's last `hours` hours (cached 10 min). Returns its path."""
    if not re.fullmatch(r"[a-z0-9_]{2,20}", view) or view in ("fd", "m1", "m2") \
            or not (DATA_DIR / "goes19" / view).is_dir():
        raise ValueError("unknown view")
    hours = min(max(hours, 1), 72)
    GIF_DIR.mkdir(parents=True, exist_ok=True)
    out = GIF_DIR / f"{view}-{int(hours)}h.gif"
    if out.exists() and time.time() - out.stat().st_mtime < 600:
        return out
    from PIL import Image
    with GIF_LOCK:
        if out.exists() and time.time() - out.stat().st_mtime < 600:
            return out
        now = datetime.datetime.now(datetime.timezone.utc)
        fr = [p for t, p in digest_module().frames(view) if now - t <= datetime.timedelta(hours=hours)]
        if len(fr) < 3:
            raise ValueError("not enough frames in that time range yet")
        pick = fr[:: max(1, -(-len(fr) // 72))][-72:]
        ims = []
        for p in pick:
            im = Image.open(p).convert("RGB")
            w = 720
            ims.append(im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
                       .quantize(colors=128, method=Image.Quantize.MEDIANCUT))
        tmp = out.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            ims[0].save(f, "GIF", save_all=True, append_images=ims[1:], duration=[250] * (len(ims) - 1) + [1200],
                        loop=0, optimize=True)
        tmp.replace(out)
    return out


MP4_LOCK = threading.Lock()


def make_mp4(view, hours):
    """An H.264 time-lapse of a rendered view's last `hours` hours (cached 10 min). Returns its path."""
    if not re.fullmatch(r"[a-z0-9_]{2,20}", view) or view in ("fd", "m1", "m2") \
            or not (DATA_DIR / "goes19" / view).is_dir():
        raise ValueError("unknown view")
    hours = min(max(hours, 1), 72)
    GIF_DIR.mkdir(parents=True, exist_ok=True)
    out = GIF_DIR / f"{view}-{int(hours)}h.mp4"
    if out.exists() and time.time() - out.stat().st_mtime < 600:
        return out
    with MP4_LOCK:
        if out.exists() and time.time() - out.stat().st_mtime < 600:
            return out
        now = datetime.datetime.now(datetime.timezone.utc)
        fr = [p for t, p in digest_module().frames(view) if now - t <= datetime.timedelta(hours=hours)]
        if len(fr) < 3:
            raise ValueError("not enough frames in that time range yet")
        pick = fr[:: max(1, -(-len(fr) // 150))][-150:]
        lst = GIF_DIR / f"{view}-{int(hours)}h.txt"
        lines = "".join(f"file '{p}'\nduration 0.12\n" for p in pick) + f"file '{pick[-1]}'\nduration 1.5\nfile '{pick[-1]}'\n"
        lst.write_text(lines)
        tmp = out.with_name(out.stem + ".tmp.mp4")
        r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-vf", "scale=960:-2:flags=lanczos,format=yuv420p", "-r", "12",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart",
                            str(tmp)], capture_output=True, text=True, timeout=300)
        lst.unlink(missing_ok=True)
        if r.returncode:
            tmp.unlink(missing_ok=True)
            raise ValueError("could not build the video: " + r.stderr.strip()[-200:])
        tmp.replace(out)
    return out


# ---- SDR detection ------------------------------------------------------

def sdr_check():
    usb = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    sdr = [l for l in usb.splitlines()
           if re.search(r"0bda:28(32|38)|1d50:60a1|RTL|Airspy", l, re.I)]
    res = {"usb": sdr, "rtl_test": None}
    if sdr and not PROCS["goesrecv"].running() and not external_pids("goesrecv"):
        try:
            r = subprocess.run(["rtl_test", "-t"], capture_output=True,
                               text=True, timeout=8)
            res["rtl_test"] = (r.stdout + r.stderr).strip()[-1500:]
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"") + (e.stderr or b"")
            res["rtl_test"] = out.decode(errors="replace")[-1500:] if isinstance(out, bytes) else str(out)[-1500:]
    elif sdr:
        res["rtl_test"] = "goesrecv is running and holds the SDR; stop it to run rtl_test."
    return res


# ---- HTTP ---------------------------------------------------------------

def load_password():
    if not PASSWORD_FILE.exists():
        PASSWORD_FILE.write_text(secrets.token_urlsafe(9) + "\n")
        PASSWORD_FILE.chmod(0o600)
    return PASSWORD_FILE.read_text().strip()


load_password()  # creates the file on first run
SECURE = CERT.exists() and KEY.exists()


class Handler(BaseHTTPRequestHandler):
    server_version = "goes-ui"
    timeout = 30  # also bounds the lazy TLS handshake

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        t0 = time.time()
        super().handle_one_request()
        secs = time.time() - t0
        path = getattr(self, "path", "")
        if secs > 1 and self.command == "GET" and path.startswith("/api/"):
            watch.note_slow(path.split("?")[0], secs)

    def client_ip(self):
        # Behind `tailscale serve` every request comes from localhost; trust its
        # X-Forwarded-For only in that case so lockouts stay per-client.
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if fwd:
                return fwd
        return ip

    def locked_out(self):
        with FAILS_LOCK:
            n, t = FAILS.get(self.client_ip(), (0, 0))
            if n >= MAX_FAILS and time.time() - t < LOCKOUT_SECS:
                return True
            if n >= MAX_FAILS:
                FAILS.pop(self.client_ip(), None)
        return False

    def authed(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(pw, load_password()) & hmac.compare_digest(user, "pi")
            except Exception:
                ok = False
            if not ok:
                with FAILS_LOCK:
                    n, t = FAILS.get(self.client_ip(), (0, time.time()))
                    FAILS[self.client_ip()] = (n + 1, t if n else time.time())
                    print(f"failed login from {self.client_ip()} ({n + 1})", flush=True)
                time.sleep(1)
            return ok
        return False

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def wants_gzip(self):
        return "gzip" in self.headers.get("Accept-Encoding", "")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        gz = len(body) > 1024 and self.wants_gzip()
        if gz:
            body = gzip.compress(body, 4)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download=False):
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.parent == STATIC and path.suffix in GZIP_SUFFIXES and not download and "Range" not in self.headers and self.wants_gzip():
            st = path.stat()
            hit = _gz_cache.get(path)
            if not hit or hit[0] != st.st_mtime_ns:
                hit = _gz_cache[path] = (st.st_mtime_ns, gzip.compress(path.read_bytes(), 6))
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", etag)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(hit[1])))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(hit[1])
            return
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))   # iOS needs this for video
        if m and (m.group(1) or m.group(2)) and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
        else:
            # the app page changes often; images/thumbnails are immutable
            self.send_header("Cache-Control",
                             "no-cache" if path.suffix in (".html", ".css", ".js") else "max-age=3600")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    def via_tailscale(self):
        """`tailscale serve` connects from localhost and adds the signed-in tailnet
        user; tailnet membership is the login. Direct (LAN / port-forward) requests
        come from a real address, so a forged header there is ignored."""
        return (self.client_address[0] in ("127.0.0.1", "::1")
                and bool(self.headers.get("Tailscale-User-Login")))

    def gate(self):
        if self.via_tailscale():
            return True
        if self.locked_out():
            body = b"Too many failed logins. Try again later."
            self.send_response(429)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        if self.authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="GOES"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        # Home Screen install assets are fetched without credentials by iOS.
        if self.path == "/manifest.webmanifest":
            return self.send_json({
                "name": "GOES Control", "short_name": "GOES", "start_url": "/",
                "scope": "/", "display": "standalone",
                "background_color": "#0e1828", "theme_color": "#0e1828",
                "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]})
        if self.path == "/icon.png":
            return self.send_file(STATIC / "icon.png")
        if not self.gate():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send_file(STATIC / "index.html")
            if u.path in ("/app.css", "/app.js"):
                return self.send_file(STATIC / u.path[1:])
            if u.path == "/api/status":
                return self.send_json(status())
            if u.path == "/api/stats":
                since = float(q.get("since", ["0"])[0])
                h = [d for d in PROCS["goesrecv"].history if d["t"] > since]
                return self.send_json({"points": h})
            if u.path == "/api/log":
                n = PROCS.get(q.get("name", ["goesrecv"])[0])
                lines = [l for l in n.log if "[monitor]" not in l] if q.get("quiet") else list(n.log)
                return self.send_json({"lines": lines[-300:]})
            if u.path == "/api/config":
                return self.send_json({"fields": read_config(),
                                       "goesrecv": RECV_CONF.read_text(),
                                       "goesproc": PROC_CONF.read_text()})
            if u.path == "/api/files":
                return self.send_json(list_dir(q.get("path", [""])[0]))
            if u.path == "/api/gallery":
                return self.send_json(gallery(int(q.get("offset", ["0"])[0]),
                                              min(int(q.get("limit", ["60"])[0]), 200),
                                              q.get("region", [""])[0]))
            if u.path == "/api/mail/describe":
                return self.send_json(mail_describe(q.get("path", [""])[0]))
            if u.path == "/api/share":
                return self.send_json(share_status())
            if u.path == "/api/forecast":
                return self.send_json({"offices": forecast_data()})
            if u.path == "/api/products/summary":
                return self.send_json(products.summary(min(max(float(q.get("hours", ["48"])[0]), 1), 72)))
            if u.path == "/api/products":
                return self.send_json(products.search(
                    q.get("cat", [""])[0], q.get("office", [""])[0], q.get("q", [""])[0][:80],
                    min(max(float(q.get("hours", ["48"])[0]), 1), 72), min(int(q.get("limit", ["50"])[0]), 100),
                    max(int(q.get("offset", ["0"])[0]), 0), q.get("warnings", [""])[0] == "1"))
            if u.path == "/api/charts":
                return self.send_json({"groups": products.charts()})
            if u.path == "/api/chart":
                return self.send_json({"frames": products.chart_frames(q.get("code", [""])[0],
                                                                       int(q.get("n", ["12"])[0]))})
            if u.path == "/api/product":
                return self.send_json(products.product(q.get("name", [""])[0]))
            if u.path == "/api/alerts/active":
                offs = [o for o in q.get("office", [""])[0].split(",") if o.strip()]
                return self.send_json({"alerts": products.active_alerts(offs or None),
                                       "local": products.active_alerts(zones=[local.HOME["zone"], local.HOME["marine"]])})
            if u.path == "/api/skywarn":
                return self.send_json(skywarn.summary())
            if u.path == "/api/notify":
                return self.send_json({"log": watch.notification_log(100), "settings": watch.notify_settings(),
                                       "categories": [{"id": c[0], "label": c[1], "desc": c[2]} for c in watch.NOTIFY_CATEGORIES]})
            if u.path == "/api/outages":
                return self.send_json({"outages": watch.outages(min(max(int(q.get("days", ["14"])[0]), 1), 14))})
            if u.path == "/api/pointforecast":
                hit = _pf_cache.get("p")
                if not hit or time.time() - hit[0] > 300:
                    _pf_cache["p"] = hit = (time.time(), local.point_forecast())
                return self.send_json(hit[1] or {"points": []})
            if u.path == "/api/weather/history":
                return self.send_json(local.weather_history(min(max(int(q.get("hours", ["72"])[0]), 6), 336)))
            if u.path == "/api/insights":
                try:
                    return self.send_json(json.loads((HOME / "goes" / "insights.json").read_text()))
                except (OSError, ValueError):
                    return self.send_json({})
            if u.path == "/api/tropical":
                return self.send_json(tropical.evaluate())
            if u.path == "/api/winter":
                ev = winter.evaluate()
                return self.send_json({**ev, "headline": winter.headline(ev)})
            if u.path == "/api/tides":
                return self.send_json(tides.tides())
            if u.path == "/api/local":
                return self.send_json(local.summary())
            if u.path == "/api/mail":
                return self.send_json(mail_status())
            if u.path == "/api/history":
                hrs = min(max(float(q.get("hours", ["24"])[0]), 1), 24 * 14)
                return self.send_json({"points": watch.history(hrs), "state": watch.status(),
                                       "hours": hrs})
            gm = re.fullmatch(r"/gif/([a-z0-9_]+)-(\d+)h\.gif", u.path)
            if gm:
                return self.send_file(make_gif(gm.group(1), float(gm.group(2))))
            mm = re.fullmatch(r"/mp4/([a-z0-9_]+)-(\d+)h\.mp4", u.path)
            if mm:
                return self.send_file(make_mp4(mm.group(1), float(mm.group(2))))
            if u.path == "/api/pi":
                return self.send_json({"specs": sysstats.specs(), "live": sysstats.live()})
            if u.path == "/api/pi/history":
                hrs = min(max(float(q.get("hours", ["24"])[0]), 1), 24 * 14)
                return self.send_json({"points": sysstats.history(hrs), "hours": hrs})
            if u.path == "/api/storage":
                return self.send_json({"bytes": dir_size(DATA_DIR) if DATA_DIR.exists() else 0})
            if u.path.startswith("/file/"):
                return self.send_file(safe_path(unquote(u.path[6:])), download="dl" in q)
            if u.path.startswith("/thumb/"):
                return self.send_file(thumb(unquote(u.path[7:]), int(q.get("w", ["360"])[0])))
            if u.path == "/zip":
                return self.send_zip(q.get("path", [""])[0])
            self.send_json({"error": "not found"}, 404)
        except (PermissionError, FileNotFoundError, NotADirectoryError) as e:
            self.send_json({"error": str(e)}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass

    def send_zip(self, rel):
        d = safe_path(rel)
        if not d.is_dir():
            raise FileNotFoundError(rel)
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                hidden = DATA_DIR.resolve() / "backup"
                for f in d.rglob("*"):
                    if f.is_file() and hidden not in f.parents:
                        z.write(f, f.relative_to(d))
            size = tmp.tell()
            tmp.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{d.name or "goes"}.zip"')
            self.end_headers()
            shutil.copyfileobj(tmp, self.wfile)

    def do_POST(self):
        if not self.gate():
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "content-type must be application/json"}, 415)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad json"}, 400)
        path = urlparse(self.path).path
        try:
            m = re.fullmatch(r"/api/(goesrecv|goesproc)/(start|stop|restart)", path)
            if m:
                pr, act = PROCS[m.group(1)], m.group(2)
                if act == "restart":
                    pr.stop()
                    time.sleep(1)
                    return self.send_json({"result": pr.start()})
                return self.send_json({"result": getattr(pr, act)()})
            if path == "/api/config/fields":
                update_config(body["fields"])
                msg = "saved"
                if body.get("restart") and (PROCS["goesrecv"].running()):
                    PROCS["goesrecv"].stop()
                    time.sleep(1)
                    msg += "; " + PROCS["goesrecv"].start()
                return self.send_json({"result": msg})
            if path == "/api/config/raw":
                if body["which"] == "goesproc":
                    check_proc_dirs(body["text"])
                save_text(GOES_DIR / ("goesrecv.conf" if body["which"] == "goesrecv"
                                      else "goesproc.conf"), body["text"])
                return self.send_json({"result": "saved (restart to apply)"})
            if path == "/api/mail/recipients":
                mail_recipients_change(add=body.get("add"), remove=body.get("remove"))
                return self.send_json(mail_status())
            if path == "/api/mail/time":
                mail_set_time(body.get("send_time"))
                return self.send_json(mail_status())
            if path == "/api/mail/image":
                return self.send_json({"result": mail_image(str(body.get("path", "")), body.get("to"), body.get("note"))})
            if path == "/api/mail/test":
                return self.send_json({"result": mail_test(str(body.get("to", "")).strip())})
            if path == "/api/share/add":
                share.add_link(body.get("name", ""), body.get("email", ""))
                return self.send_json(share_status())
            if path == "/api/share/rotate":
                share.rotate_link(str(body.get("id", "")))
                return self.send_json(share_status())
            if path == "/api/share/revoke":
                share.revoke_link(str(body.get("id", "")))
                return self.send_json(share_status())
            if path == "/api/push/topic":
                set_push_topic(body.get("topic"))
                return self.send_json(share_status())
            if path == "/api/alerts/test":
                kind = body.get("kind")
                if kind == "signal":
                    ok = watch.notify("GOES: test alert", "This is a test of the GOES signal alerts. "
                                      "Real ones are sent when the signal is lost, a process is restarted, "
                                      "the disk is nearly full or the Pi overheats.", key="test", every=60)
                    return self.send_json({"result": "sent" if ok else "not sent (wait a minute, or check mail setup)"})
                if kind == "push":
                    cfg = tomllib.loads(MAIL_CONF.read_text())
                    ok = digest_module().push(cfg, "GOES test push", "Push notifications from the GOES receiver work.")
                    return self.send_json({"result": "push sent - check your phone" if ok
                                           else "not sent (no topic set, or ntfy.sh unreachable)"})
                if kind == "weather":
                    hits = watch.wx_check(dry=True)
                    return self.send_json({"result": "no new matching warnings in the feed right now"
                                           if not hits else "; ".join(f"{n} ({o})" for n, o, _ in hits)})
                raise ValueError("unknown test")
            if path == "/api/notify/settings":
                return self.send_json({"settings": watch.save_notify_settings(body)})
            if path == "/api/notify/test":
                ok = watch.push_only("GOES: test notification", "If you can read this, phone alerts are working.",
                                     priority="default", tags="white_check_mark", category="test")
                return self.send_json({"result": "sent" if ok else "not sent: check the ntfy topic in the Mail tab"})
            if path == "/api/sdrtest":
                return self.send_json(sdr_check())
            if path == "/api/webui/restart":
                # the service has Restart=always: ending the process (cleanly, via the SIGTERM handler that stops
                # goesrecv/goesproc) makes systemd start a fresh panel about 5 seconds later; no sudo needed
                threading.Thread(target=lambda: (time.sleep(1), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except (ValueError, KeyError, tomllib.TOMLDecodeError) as e:
            self.send_json({"error": str(e)}, 400)


def shutdown(*_):
    for pr in PROCS.values():
        if pr.running():
            pr.stop()
    sys.exit(0)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, shutdown)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    if SECURE:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True,
                                     do_handshake_on_connect=False)
    watch.start(PROCS, external_pids)
    sysstats.start()
    threading.Thread(target=lambda: (products.charts(), local.summary(), skywarn.summary()), daemon=True).start()     # warm the product index so the first Charts/Weather open is quick
    threading.Thread(target=prewarm_thumbs, daemon=True).start()
    try:
        share.start(lambda v: digest_module().frames(v), make_gif, forecast_data, STATIC / "icon.png", wx=(local, products))
    except OSError as e:
        print("share page not started:", e, flush=True)
    if os.environ.get("GOES_AUTOSTART", "1") == "1":
        def autostart():
            """Bring the receiver up after a boot or panel restart (set GOES_AUTOSTART=0 to disable)."""
            time.sleep(8)  # let USB / the SDR settle after boot
            print("autostart: goesrecv", PROCS["goesrecv"].start(), flush=True)
            time.sleep(10)  # goesproc subscribes to goesrecv's packet socket
            print("autostart: goesproc", PROCS["goesproc"].start(), flush=True)
        threading.Thread(target=autostart, daemon=True).start()
    print(f"GOES UI on port {PORT} ({'HTTPS' if SECURE else 'plain HTTP - LAN only'}); "
          f"password in {PASSWORD_FILE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        shutdown()
