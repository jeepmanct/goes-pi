#!/usr/bin/env python3
"""GOES control panel: start/stop goesrecv + goesproc, live signal stats,
config editing and image browsing. Standard library only (Pillow optional,
used for thumbnails)."""

import base64
import collections
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image
except ImportError:
    Image = None

HOME = Path.home()
GOES_DIR = HOME / "goes"
DATA_DIR = GOES_DIR / "data"
WEB_DIR = Path(__file__).resolve().parent
STATIC = WEB_DIR / "static"
THUMBS = WEB_DIR / "thumbs"
PASSWORD_FILE = WEB_DIR / "password.txt"
RECV_CONF = GOES_DIR / "goesrecv.conf"
PROC_CONF = GOES_DIR / "goesproc.conf"
PORT = int(os.environ.get("GOES_UI_PORT", "8080"))

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

    def running(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        with self.lock:
            if self.running():
                return "already running"
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
    return p


def list_dir(rel):
    p = safe_path(rel)
    dirs, files = [], []
    for e in sorted(p.iterdir(), key=lambda e: e.name, reverse=True):
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


def dir_size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def thumb(rel, width=360):
    src = safe_path(rel)
    if Image is None or src.suffix.lower() not in IMAGE_EXT:
        return src
    dst = THUMBS / f"{abs(hash(str(src)))}_{int(src.stat().st_mtime)}_{width}.jpg"
    if not dst.exists():
        THUMBS.mkdir(exist_ok=True)
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            im.draft("RGB", (width * 2, width * 2))
            im = im.convert("RGB")
            im.thumbnail((width, width))
            im.save(dst, "JPEG", quality=80)
    return dst


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


PASSWORD = load_password()


class Handler(BaseHTTPRequestHandler):
    server_version = "goes-ui"

    def log_message(self, *a):
        pass

    def authed(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                return hmac.compare_digest(pw, PASSWORD)
            except Exception:
                return False
        return False

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, download=False):
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
        else:
            self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def gate(self):
        if self.authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="GOES"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.gate():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send_file(STATIC / "index.html")
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
            if u.path == "/api/storage":
                return self.send_json({"bytes": dir_size(DATA_DIR) if DATA_DIR.exists() else 0})
            if u.path.startswith("/file/"):
                return self.send_file(safe_path(unquote(u.path[6:])), download="dl" in q)
            if u.path.startswith("/thumb/"):
                return self.send_file(thumb(unquote(u.path[7:])))
            if u.path == "/zip":
                return self.send_zip(q.get("path", [""])[0])
            self.send_json({"error": "not found"}, 404)
        except (PermissionError, FileNotFoundError, NotADirectoryError) as e:
            self.send_json({"error": str(e)}, 404)
        except BrokenPipeError:
            pass

    def send_zip(self, rel):
        d = safe_path(rel)
        if not d.is_dir():
            raise FileNotFoundError(rel)
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for f in d.rglob("*"):
                    if f.is_file():
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
                save_text(GOES_DIR / ("goesrecv.conf" if body["which"] == "goesrecv"
                                      else "goesproc.conf"), body["text"])
                return self.send_json({"result": "saved (restart to apply)"})
            if path == "/api/sdrtest":
                return self.send_json(sdr_check())
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
    print(f"GOES UI on port {PORT}; password in {PASSWORD_FILE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        shutdown()
