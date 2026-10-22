#!/usr/bin/env python3
"""Build GOES-19 full-disk views: the whole globe plus regional crops of the Americas.

Day side: visible (C02, color when C01 also arrives); night side: C13 infrared
in blue-gray; blended by the sun's elevation so the terminator shows. Regional
crops keep the satellite's native resolution, with coastlines and borders from
Natural Earth drawn on top.

Output: ~/goes/data/goes19/<view>/YYYY-MM-DD/GOES19_<NAME>_<scan start>Z.jpg
Run from cron; backfills any scans that have no output yet, newest first.
"""
import argparse
import datetime as dt
import fcntl
import json
import math
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None

DATA = Path("/home/pi/goes/data/goes19")
FD = DATA / "fd"
MAPS = Path("/home/pi/goes/maps")
LOCAL = ZoneInfo("America/New_York")

SUB_LON = -75.0                 # projection origin in the HRIT headers (geos(-75.0)); coastline fit peaks here, not at -75.2
H = 42164160.0                  # distance from Earth's center, m
R_EQ, R_POL = 6378137.0, 6356752.31414
K = (R_EQ / R_POL) ** 2
X0, STEP = -0.151844, 0.000056  # fixed-grid scan angle of pixel 0 / per pixel (2 km, 5424 px)
NATIVE = 5424
VIS_AGE = dt.timedelta(minutes=12)   # visible/infrared must come from the same scan, roughly
NYC = (40.71, -74.01)

# bbox = (lat_min, lat_max, lon_min, lon_max); f = downsample factor, scale = upsample after
REGIONS = {
    "globe":   dict(name="FD_GLOBE", label="Full disk", bbox=None, f=3, scale=1, borders=False, nyc=False),
    "namer":   dict(name="NORTH_AMERICA", label="North America", bbox=(12, 62, -140, -48), f=2, scale=1, borders=True, nyc=True),
    "samer":   dict(name="SOUTH_AMERICA", label="South America", bbox=(-56, 14, -86, -32), f=2, scale=1, borders=True, nyc=False),
    "gulf":    dict(name="GULF_CARIBBEAN", label="Gulf and Caribbean", bbox=(8, 33, -100, -56), f=1, scale=1, borders=True, nyc=False),
    "northeast": dict(name="NORTHEAST_US", label="Northeast US", bbox=(38.0, 47.5, -82.0, -64.0), f=1, scale=2, borders=True, nyc=True),
    "eastus":  dict(name="EAST_US", label="East Coast US", bbox=(24, 48, -90, -58), f=1, scale=1, borders=True, nyc=True),
    "nyc":     dict(name="NYC_AREA", label="New York City area", bbox=(37.5, 44.5, -78.5, -68.5), f=1, scale=3, borders=True, nyc=True),
}
def _fog(key, label, name):
    base = REGIONS[key]
    return dict(base, name=name, label=label, night="fog")


REGIONS.update({
    "fog_northeast": _fog("northeast", "Night fog, Northeast US", "FOG_NORTHEAST_US"),
    "fog_eastus": _fog("eastus", "Night fog, East Coast US", "FOG_EAST_US"),
    "fog_nyc":    _fog("nyc", "Night fog, New York City area", "FOG_NYC_AREA"),
    "fog_namer":  _fog("namer", "Night fog, North America", "FOG_NORTH_AMERICA"),
    "fog_gulf":   _fog("gulf", "Night fog, Gulf and Caribbean", "FOG_GULF_CARIBBEAN"),
    "fog_samer":  _fog("samer", "Night fog, South America", "FOG_SOUTH_AMERICA"),
})
FOG_AGE = dt.timedelta(minutes=35)   # channels are staggered by ~30 min in the broadcast; fog moves slowly

OUTLINE = dict(stroke_width=2, stroke_fill=(0, 0, 0, 200))
NAME_RE = re.compile(r"CMIPF-M\dC(\d{2})_G19_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")


# ---- inputs -----------------------------------------------------------------

def scan_files():
    """{channel: [(scan start, path), ...]} oldest first."""
    found = {}
    for p in FD.rglob("*.png"):
        m = NAME_RE.search(p.name)
        if not m:
            continue
        y, doy, hh, mm, ss = map(int, m.groups()[1:])
        t = dt.datetime(y, 1, 1, hh, mm, ss, tzinfo=dt.timezone.utc) + dt.timedelta(days=doy - 1)
        found.setdefault(m.group(1), []).append((t, p))
    for v in found.values():
        v.sort()
    return found


def nearest(files, ch, t, age=VIS_AGE):
    best = min(files.get(ch, []), key=lambda tp: abs(tp[0] - t), default=None)
    return best[1] if best and abs(best[0] - t) <= age else None


def bt_table(png):
    """256-entry count -> Kelvin table from goesproc's sidecar header, or None."""
    j = png.with_suffix(".json")
    if not j.exists():
        return None

    def find(o):
        if isinstance(o, dict):
            if "Table" in o and isinstance(o["Table"], list) and len(o["Table"]) == 256:
                return o["Table"]
            for v in o.values():
                r = find(v)
                if r:
                    return r
        return None
    try:
        t = find(json.loads(j.read_text()))
    except ValueError:
        return None
    return np.asarray(t, dtype=np.float32) if t else None


def fog_inputs(files, t):
    """{'07': path, '13': path, '15': path} with tables available, or None."""
    got = {}
    for c in ("07", "13", "15"):
        p = nearest(files, c, t, FOG_AGE)
        if not p or bt_table(p) is None:
            return None
        got[c] = p
    return got


def load_full(path):
    im = Image.open(path)
    if im.size != (NATIVE, NATIVE):
        im = im.resize((NATIVE, NATIVE), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def window_bt(a, table, box, f):
    """Same as window() but converts counts to brightness temperature (K) first."""
    r0, r1, c0, c1 = box
    w = table[a[r0:r1, c0:c1]]
    h, wd = (w.shape[0] // f) * f, (w.shape[1] // f) * f
    w = w[:h, :wd]
    return w.reshape(h // f, f, wd // f, f).mean(axis=(1, 3)) if f > 1 else w


def window(a, box, f):
    """Crop native array to box=(r0,r1,c0,c2) and average down by f -> float32 0..1."""
    r0, r1, c0, c1 = box
    w = a[r0:r1, c0:c1]
    h, wd = (w.shape[0] // f) * f, (w.shape[1] // f) * f
    w = w[:h, :wd].astype(np.float32)
    if f > 1:
        w = w.reshape(h // f, f, wd // f, f).mean(axis=(1, 3))
    return w / 255.0


# ---- geometry ---------------------------------------------------------------

def to_pixels(lat, lon):
    """lat/lon (degrees, arrays) -> native (col, row) float arrays and a visible mask."""
    phi, lam = np.radians(lat), np.radians(lon - SUB_LON)
    phic = np.arctan(np.tan(phi) / K)
    e2 = 1 - 1 / K
    rc = R_POL / np.sqrt(1 - e2 * np.cos(phic) ** 2)
    sx = H - rc * np.cos(phic) * np.cos(lam)
    sy = -rc * np.cos(phic) * np.sin(lam)
    sz = rc * np.sin(phic)
    vis = H * (H - sx) >= sy ** 2 + K * sz ** 2
    y = np.arctan(sz / sx)
    x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    return (x - X0) / STEP, (-X0 - y) / STEP, vis


def region_box(bbox, f):
    """Native pixel window (r0, r1, c0, c1) covering a lat/lon box, aligned to f."""
    if bbox is None:
        return (0, NATIVE, 0, NATIVE)
    la0, la1, lo0, lo1 = bbox
    lats, lons = np.meshgrid(np.linspace(la0, la1, 60), np.linspace(lo0, lo1, 60), indexing="ij")
    c, r, vis = to_pixels(lats, lons)
    c, r = c[vis], r[vis]
    c0, c1 = max(int(c.min()), 0), min(int(c.max()) + 1, NATIVE)
    r0, r1 = max(int(r.min()), 0), min(int(r.max()) + 1, NATIVE)
    c0, r0 = c0 // f * f, r0 // f * f
    c1, r1 = min(c0 + -(-(c1 - c0) // f) * f, NATIVE), min(r0 + -(-(r1 - r0) // f) * f, NATIVE)
    return (r0, r1, c0, c1)


def grid_latlon(box, f):
    """lat, lon (radians) for each pixel center in the window; mask of on-disk pixels."""
    r0, r1, c0, c1 = box
    cols = c0 + (np.arange((c1 - c0) // f) + 0.5) * f - 0.5
    rows = r0 + (np.arange((r1 - r0) // f) + 0.5) * f - 0.5
    x = (X0 + cols * STEP)[None, :]
    y = (-X0 - rows * STEP)[:, None]
    cx, sx_, cy, sy_ = np.cos(x), np.sin(x), np.cos(y), np.sin(y)
    a = sx_ ** 2 + cx ** 2 * (cy ** 2 + K * sy_ ** 2)
    b = -2.0 * H * cx * cy
    disc = b ** 2 - 4 * a * (H ** 2 - R_EQ ** 2)
    ok = disc > 0
    rs = (-b - np.sqrt(np.where(ok, disc, 0))) / (2 * a)
    px, py, pz = rs * cx * cy, -rs * sx_, rs * cx * sy_
    lat = np.arctan(K * pz / np.sqrt((H - px) ** 2 + py ** 2))
    lon = math.radians(SUB_LON) - np.arctan(py / (H - px))
    return lat, lon, ok


def subsolar(t):
    """Sub-solar latitude/longitude in radians (low-precision almanac formulas)."""
    n = (t - dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)).total_seconds() / 86400.0
    L = math.radians(280.460 + 0.9856474 * n)
    g = math.radians(357.528 + 0.9856003 * n)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    gmst = math.radians((280.46061837 + 360.98564736629 * n) % 360)
    return dec, (ra - gmst + math.pi) % (2 * math.pi) - math.pi


# ---- imagery ----------------------------------------------------------------

def smoothstep(e0, e1, v):
    u = np.clip((v - e0) / (e1 - e0), 0, 1)
    return u * u * (3 - 2 * u)


def stretch(a, mask, lo=1, hi=99.5):
    if not mask.any():
        return np.zeros_like(a)
    p0, p1 = np.percentile(a[mask], [lo, hi])
    return np.clip((a - p0) / max(p1 - p0, 1e-6), 0, 1)


def fog_rgb(bt, box, f):
    """NOAA Nighttime Microphysics RGB: R=C15-C13, G=C13-C07, B=C13 (all in K)."""
    b07, b13, b15 = (window_bt(bt[c][0], bt[c][1], box, f) for c in ("07", "13", "15"))
    r = np.clip((b15 - b13 + 6.7) / (2.6 + 6.7), 0, 1)
    g = np.clip((b13 - b07 + 3.1) / (5.2 + 3.1), 0, 1)
    b = np.clip((b13 - 243.55) / (292.55 - 243.55), 0, 1)
    return np.dstack([r, g, b])


def compose(chans, box, f, t, bt=None):
    """chans: {'13': uint8 array, '02': ..., '01': ..., '03': ...} (full native). Returns (rgb float, mode)."""
    lat, lon, disc = grid_latlon(box, f)
    slat, slon = subsolar(t)
    cosz = np.sin(lat) * math.sin(slat) + np.cos(lat) * math.cos(slat) * np.cos(lon - slon)
    cosz = np.where(disc, cosz, -1)
    day = smoothstep(math.cos(math.radians(100)), math.cos(math.radians(80)), cosz)[..., None]

    ir = stretch(window(chans["13"], box, f), disc) if "13" in chans else np.zeros(disc.shape, np.float32)
    night = np.dstack([0.10 + 0.55 * ir, 0.13 + 0.62 * ir, 0.22 + 0.78 * ir]) * 0.9
    if bt is not None:
        night = fog_rgb(bt, box, f)

    if "02" in chans:
        red = window(chans["02"], box, f)
        if "01" in chans:
            blue = window(chans["01"], box, f)
            veg = window(chans["03"], box, f) if "03" in chans else (red + blue) / 2
            green = 0.45 * red + 0.45 * blue + 0.10 * veg
            mode = "color"
        else:
            blue = green = red
            mode = "visible (C02 only, color needs C01)"
        rgb = np.dstack([red, green, blue])
        lit = disc & (cosz > 0.25)
        gain = 1.0 / max(float(np.percentile(rgb[lit], 99.0)) if lit.any() else 1.0, 1e-3)
        rgb = np.clip(rgb * min(gain, 3.0), 0, 1) ** (1 / 1.8)
    else:
        rgb = np.dstack([ir * 0.95, ir * 0.97, ir]) * 0.9 + 0.04
        mode = "infrared (no visible scan at this time)"
    if bt is not None:
        mode = "night fog: aqua/green = fog or low cloud" + ("" if mode == "color" else "  ·  day " + mode.split(" (")[0])
    return np.where(disc[..., None], day * rgb + (1 - day) * night, 0.0), mode


# ---- map overlay ------------------------------------------------------------

_LINES = {}


def lines(kind):
    """List of (N, 2) lon/lat arrays for a Natural Earth line file."""
    if kind not in _LINES:
        p = MAPS / f"ne_50m_{kind}.geojson"
        out = []
        if p.exists():
            for ft in json.loads(p.read_text())["features"]:
                g = ft["geometry"]
                parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
                out += [np.asarray(c, dtype=np.float64)[:, :2] for c in parts]
        _LINES[kind] = out
    return _LINES[kind]


def draw_lines(draw, kind, box, f, scale, color, width):
    r0, _r1, c0, _c1 = box
    for ln in lines(kind):
        c, r, vis = to_pixels(ln[:, 1], ln[:, 0])
        x, y = (c - c0 + 0.5) / f * scale, (r - r0 + 0.5) / f * scale
        pts = list(zip(x.tolist(), y.tolist()))
        for i in range(len(pts) - 1):
            if vis[i] and vis[i + 1]:
                draw.line((pts[i], pts[i + 1]), fill=color, width=width)


def annotate(img, spec, box, f, t, mode):
    scale = spec["scale"]
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    lw = 2 if scale >= 2 or img.width > 2000 else 1
    if spec["borders"]:
        draw_lines(d, "admin_1_states_provinces_lines", box, f, scale, (255, 255, 255, 60), lw)
        draw_lines(d, "admin_0_boundary_lines_land", box, f, scale, (255, 235, 140, 150), lw)
        draw_lines(d, "coastline", box, f, scale, (255, 235, 140, 150), lw)
    fs = max(14, img.width // 60)
    try:
        font = ImageFont.load_default(size=fs)
    except TypeError:
        font = ImageFont.load_default()
    if spec["nyc"]:
        c, r, vis = to_pixels(np.array([NYC[0]]), np.array([NYC[1]]))
        x, y = (c[0] - box[2] + 0.5) / f * scale, (r[0] - box[0] + 0.5) / f * scale
        if 0 <= x < img.width and 0 <= y < img.height:
            rad = max(5, fs // 3)
            d.ellipse((x - rad, y - rad, x + rad, y + rad), outline=(255, 90, 90, 230), width=2)
            d.text((x + rad + 4, y - fs * 0.6), "NYC", fill=(255, 170, 170, 245), font=font, **OUTLINE)
    local = t.astimezone(LOCAL)
    d.text((14, 12), f"GOES-19  {spec['label']}", fill=(235, 240, 250, 255), font=font, **OUTLINE)
    d.text((14, 12 + fs * 1.4), f"{t:%Y-%m-%d %H:%M} UTC  ·  {local:%-I:%M %p %Z}",
           fill=(215, 225, 240, 255), font=font, **OUTLINE)
    if mode != "color":
        d.text((14, img.height - 12 - fs * 1.3), mode, fill=(190, 200, 215, 255), font=font, **OUTLINE)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


# ---- driver -----------------------------------------------------------------

def out_path(key, spec, t):
    return DATA / key / f"{t:%Y-%m-%d}" / f"GOES19_{spec['name']}_{t:%Y%m%dT%H%M%S}Z.jpg"


def has_night(spec, t):
    """True if at least ~3% of the region is on the night side (so a fog view is worth making)."""
    la0, la1, lo0, lo1 = spec["bbox"]
    lat, lon = np.meshgrid(np.radians(np.linspace(la0, la1, 12)), np.radians(np.linspace(lo0, lo1, 12)),
                           indexing="ij")
    slat, slon = subsolar(t)
    cosz = np.sin(lat) * math.sin(slat) + np.cos(lat) * math.cos(slat) * np.cos(lon - slon)
    return float((cosz < 0.05).mean()) > 0.03


def build_scan(t, files, wanted):
    """Load the channels for scan time t once, then render every wanted view."""
    chans = {}
    for c in ("13", "02", "01", "03"):
        p = nearest(files, c, t)
        if p:
            chans[c] = load_full(p)
    if "13" not in chans:
        return
    bt = None
    if any(REGIONS[k].get("night") == "fog" for k in wanted):
        fi = fog_inputs(files, t)
        if fi:
            bt = {c: (load_full(p), bt_table(p)) for c, p in fi.items()}
    for key in wanted:
        spec = REGIONS[key]
        if spec.get("night") == "fog" and bt is None:
            continue
        box = region_box(spec["bbox"], spec["f"])
        rgb, mode = compose(chans, box, spec["f"], t, bt if spec.get("night") == "fog" else None)
        img = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))
        if spec["scale"] > 1:
            img = img.resize((img.width * spec["scale"], img.height * spec["scale"]), Image.LANCZOS)
        img = annotate(img, spec, box, spec["f"], t, mode)
        dest = out_path(key, spec, t)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.jpg")
        img.save(tmp, "JPEG", quality=88)
        tmp.rename(dest)
        print(f"wrote {dest.relative_to(DATA)} ({mode})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if outputs exist")
    ap.add_argument("--views", nargs="*", default=list(REGIONS), choices=list(REGIONS))
    ap.add_argument("--max", type=int, default=6, help="scans to build per run, newest first")
    a = ap.parse_args()

    lock = open("/tmp/goes-views.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    files = scan_files()
    if not files.get("13"):
        print("no full-disk infrared scans yet")
        return 0
    done = 0
    for t, _p in reversed(files["13"]):
        todo = [k for k in a.views if (a.force or not out_path(k, REGIONS[k], t).exists())
                and (REGIONS[k].get("night") != "fog" or (fog_inputs(files, t) and has_night(REGIONS[k], t)))]
        if not todo:
            continue
        build_scan(t, files, todo)
        done += 1
        if done >= a.max:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
