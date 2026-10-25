"""Pi health for the panel's Pi tab: hardware specs, live CPU / memory / temperature / storage / network,
and a per-minute history file.

Started from app.py with start(). Standard library only; everything is read from /proc and /sys, plus a
couple of cheap commands (lscpu, vcgencmd, ip) that are optional.
  - live numbers: refreshed every 5 s by a daemon thread, served by live()
  - history: one averaged sample per minute -> ~/goes/pi-history.jsonl (kept 14 days), served by history()
"""
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

HISTORY = Path.home() / "goes" / "pi-history.jsonl"
KEEP_DAYS = 14
INTERVAL = 5            # seconds between live refreshes
CLK = os.sysconf("SC_CLK_TCK")
REAL_FS = {"ext4", "ext3", "ext2", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "btrfs", "xfs", "f2fs"}
THROTTLE_NOW = {0: "under-voltage", 1: "CPU speed capped", 2: "throttled", 3: "temperature limit"}
THROTTLE_PAST = {16: "under-voltage", 17: "CPU speed capped", 18: "throttled", 19: "temperature limit"}

_lock = threading.Lock()
_live = {}
_specs = {}


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _run(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---- specs (read once) ---------------------------------------------------------

def _meminfo():
    out = {}
    for ln in _read("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        try:
            out[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    return out


def _build_specs():
    lscpu = {}
    for ln in _run(["lscpu"]).splitlines():
        k, _, v = ln.partition(":")
        lscpu[k.strip()] = v.strip()
    osrel = {}
    for ln in _read("/etc/os-release").splitlines():
        k, _, v = ln.partition("=")
        osrel[k] = v.strip('"')
    cpuinfo = _read("/proc/cpuinfo")
    revision = next((ln.split(":", 1)[1].strip() for ln in cpuinfo.splitlines() if ln.startswith("Revision")), "")
    soc = _read("/proc/device-tree/compatible").replace("\0", " ").split()
    root_dev = next((p[0] for p in (ln.split() for ln in _read("/proc/mounts").splitlines()) if len(p) > 1 and p[1] == "/"), "")
    fw = _run(["vcgencmd", "version"]).splitlines()
    mem = _meminfo()
    return {
        "model": _read("/proc/device-tree/model").replace("\0", ""),
        "soc": soc[-1].replace("brcm,", "").upper() if soc else "",
        "revision": revision,
        "cpu": lscpu.get("Model name", ""),
        "cores": os.cpu_count(),
        "arch": platform.machine(),
        "cpu_min_mhz": float(lscpu["CPU min MHz"]) if lscpu.get("CPU min MHz") else None,
        "cpu_max_mhz": float(lscpu["CPU max MHz"]) if lscpu.get("CPU max MHz") else None,
        "ram": mem.get("MemTotal"),
        "swap": mem.get("SwapTotal"),
        "os": osrel.get("PRETTY_NAME", ""),
        "kernel": platform.release(),
        "hostname": platform.node(),
        "python": platform.python_version(),
        "firmware": fw[0].strip() if fw else "",
        "root_device": root_dev,
    }


def specs():
    with _lock:
        if not _specs:
            _specs.update(_build_specs())
        return dict(_specs)


# ---- live sampling -------------------------------------------------------------

def _cpu_times():
    """{'cpu': (busy, total), 'cpu0': ...} from /proc/stat."""
    out = {}
    for ln in _read("/proc/stat").splitlines():
        if not ln.startswith("cpu"):
            continue
        p = ln.split()
        v = list(map(int, p[1:9]))
        idle = v[3] + v[4]
        out[p[0]] = (sum(v) - idle, sum(v))
    return out


def _net_bytes():
    out = {}
    for ln in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = ln.partition(":")
        name, f = name.strip(), rest.split()
        if name != "lo" and len(f) >= 9:
            out[name] = (int(f[0]), int(f[8]))
    return out


def _proc_times():
    """{pid: (name, cpu ticks, rss bytes)} for every process."""
    out = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            s = Path(f"/proc/{d}/stat").read_text()
            rss = int(Path(f"/proc/{d}/statm").read_text().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
        a, b = s.find("("), s.rfind(")")
        f = s[b + 2:].split()
        try:
            out[int(d)] = (s[a + 1:b], int(f[11]) + int(f[12]), rss)
        except (ValueError, IndexError):
            continue
    return out


def _disks():
    out, seen = [], set()
    for ln in _read("/proc/mounts").splitlines():
        p = ln.split()
        if len(p) < 3 or p[2] not in REAL_FS or p[0] in seen:
            continue
        seen.add(p[0])
        mnt = p[1].replace("\\040", " ")
        try:
            du = shutil.disk_usage(mnt)
        except OSError:
            continue
        out.append({"dev": p[0], "mount": mnt, "fs": p[2], "total": du.total, "used": du.used, "free": du.free})
    return out


def _ips():
    ips = {}
    for ln in _run(["ip", "-4", "-o", "addr", "show"]).splitlines():
        p = ln.split()
        if len(p) >= 4 and p[1] != "lo":
            ips[p[1]] = p[3].split("/")[0]
    return ips


def _throttle():
    raw = _run(["vcgencmd", "get_throttled"]).strip()
    try:
        v = int(raw.split("=")[1], 16)
    except (IndexError, ValueError):
        return None
    return {"raw": hex(v),
            "now": [n for b, n in THROTTLE_NOW.items() if v >> b & 1],
            "past": [n for b, n in THROTTLE_PAST.items() if v >> b & 1]}


def _temp():
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    return int(t) / 1000 if t.isdigit() else None


def _freq_mhz(core=0):
    f = _read(f"/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_cur_freq")
    return int(f) / 1000 if f.isdigit() else None


DATA_DIR = Path.home() / "goes" / "data"
DISK_LOG = Path.home() / "goes" / "disk-history.jsonl"       # hourly used-bytes samples of the data drive
DISK_CAP = 900 * 1000 ** 3                                     # the cleanup script's size cap
_disk_last = [0.0]


def _disk_track(now):
    """Append an hourly used-bytes sample of the data drive; keep ~200 days."""
    if now - _disk_last[0] < 3600:
        return
    _disk_last[0] = now
    try:
        used = shutil.disk_usage(DATA_DIR).used
        with DISK_LOG.open("a") as f:
            f.write(json.dumps({"t": round(now), "used": used}) + "\n")
        if DISK_LOG.stat().st_size > 400_000:
            rows = DISK_LOG.read_text().splitlines()[-4800:]
            DISK_LOG.write_text("\n".join(rows) + "\n")
    except OSError:
        pass


def disk_outlook(now=None):
    """Growth of the data drive over up to 14 days and a rough time to the cleanup cap, or None while collecting."""
    now = now or time.time()
    try:
        rows = [json.loads(ln) for ln in DISK_LOG.read_text().splitlines()[-1500:]]
    except (OSError, ValueError):
        return None
    rows = [r for r in rows if r["t"] >= now - 14 * 86400]
    if len(rows) < 2 or rows[-1]["t"] - rows[0]["t"] < 20 * 3600:
        return None
    days = (rows[-1]["t"] - rows[0]["t"]) / 86400
    per_day = (rows[-1]["used"] - rows[0]["used"]) / days
    left = DISK_CAP - rows[-1]["used"]
    return {"used": rows[-1]["used"], "per_day": per_day, "days_to_cap": (left / per_day) if per_day > 1e6 and left > 0 else None,
            "span_days": days}


def _updates():
    """Package updates waiting, from apt's local lists (not refreshed here). None if apt is unavailable."""
    try:
        r = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [ln for ln in r.stdout.splitlines() if "upgradable" in ln]
    try:
        age_h = (time.time() - max(p.stat().st_mtime for p in Path("/var/lib/apt/lists").iterdir())) / 3600
    except (OSError, ValueError):
        age_h = None
    return {"count": len(lines), "security": sum(1 for ln in lines if "security" in ln.split("[")[0]),
            "lists_age_h": age_h, "t": time.time()}


def _sd_written():
    """Bytes written to the SD card since boot (field 7 of /sys/block/mmcblk0/stat is 512-byte sectors written)."""
    try:
        return int(_read("/sys/block/mmcblk0/stat").split()[6]) * 512
    except (IndexError, ValueError):
        return None


def _sampler():
    prev_cpu, prev_net, prev_proc, prev_t = _cpu_times(), _net_bytes(), _proc_times(), time.time()
    slow = {"throttle": None, "ips": {}, "t": 0}
    while True:
        time.sleep(INTERVAL)
        try:
            now = time.time()
            dt_ = max(now - prev_t, 0.1)
            cpu, net, procs = _cpu_times(), _net_bytes(), _proc_times()

            def pct(k):
                b0, t0 = prev_cpu.get(k, (0, 0))
                b1, t1 = cpu.get(k, (0, 0))
                return round(100 * (b1 - b0) / (t1 - t0), 1) if t1 > t0 else 0.0
            cores = [pct(f"cpu{i}") for i in range(os.cpu_count() or 1)]
            rates = {k: {"rx": max(0, (v[0] - prev_net.get(k, v)[0]) / dt_), "tx": max(0, (v[1] - prev_net.get(k, v)[1]) / dt_),
                         "rx_total": v[0], "tx_total": v[1]} for k, v in net.items()}
            top = []
            for pid, (name, ticks, rss) in procs.items():
                old = prev_proc.get(pid)
                if old and old[0] == name:
                    top.append({"pid": pid, "name": name, "cpu": round(100 * (ticks - old[1]) / CLK / dt_, 1), "rss": rss})
            top.sort(key=lambda p: (p["cpu"], p["rss"]), reverse=True)
            if now - slow["t"] > 30:
                slow.update(throttle=_throttle(), ips=_ips(), t=now)
                _disk_track(now)
                slow["disk_outlook"] = disk_outlook(now)
            if now - slow.get("upd_t", 0) > 6 * 3600:
                slow.update(updates=_updates(), upd_t=now)
            m = _meminfo()
            avail, total = m.get("MemAvailable", 0), m.get("MemTotal", 1)
            live = {
                "t": now, "cpu": pct("cpu"), "cores": cores, "freq_mhz": _freq_mhz(), "temp": _temp(),
                "load": list(os.getloadavg()), "uptime": float(_read("/proc/uptime", "0 0").split()[0]),
                "mem": {"total": total, "available": avail, "used": total - avail, "cached": m.get("Cached", 0) + m.get("Buffers", 0),
                        "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)},
                "disks": _disks(), "net": rates, "ips": slow["ips"], "throttle": slow["throttle"],
                "procs": len(procs), "top": top[:8], "sd_written": _sd_written(),
                "updates": slow.get("updates"), "disk_outlook": slow.get("disk_outlook"),
            }
            with _lock:
                _live.clear()
                _live.update(live)
            prev_cpu, prev_net, prev_proc, prev_t = cpu, net, procs, now
        except Exception as e:   # a bad read must never kill the sampler
            print("sysstats sampler error:", e, flush=True)


def live():
    with _lock:
        return dict(_live)


# ---- history -------------------------------------------------------------------

KEYS = ("cpu", "mem", "temp", "load", "rx", "tx")


def _avg(rows, k):
    v = [r[k] for r in rows if r.get(k) is not None]
    return round(sum(v) / len(v), 2) if v else None


def _trim():
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


def _recorder():
    _trim()
    trimmed = time.time()
    acc, minute = [], None
    while True:
        time.sleep(INTERVAL)
        lv = live()
        if not lv:
            continue
        m = int(lv["t"] // 60)
        if minute is not None and m != minute and acc:
            row = {"t": minute * 60 + 30}
            for k in KEYS:
                row[k] = _avg(acc, k)
            with HISTORY.open("a") as f:
                f.write(json.dumps(row) + "\n")
            acc = []
        minute = m
        mem = lv["mem"]
        acc.append({"cpu": lv["cpu"], "mem": round(100 * mem["used"] / mem["total"], 1), "temp": lv["temp"],
                    "load": lv["load"][0], "rx": sum(n["rx"] for n in lv["net"].values()),
                    "tx": sum(n["tx"] for n in lv["net"].values())})
        if time.time() - trimmed > 86400:
            _trim()
            trimmed = time.time()


def history(hours=24, buckets=288):
    """Samples for the last `hours`, averaged into at most `buckets` points."""
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
            out.append({"t": cur[len(cur) // 2]["t"], **{k: _avg(cur, k) for k in KEYS}})
            cur = []
        edge = b
        cur.append(r)
    if cur:
        out.append({"t": cur[len(cur) // 2]["t"], **{k: _avg(cur, k) for k in KEYS}})
    return out


def start():
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=_recorder, daemon=True).start()
