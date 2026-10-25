"""Surface temperature (split-window infrared) and NWS radar helpers for views.py and insights.py.

Surface temperature: skin temperature of clear-sky pixels from the 10.3 um (C13) and 12.3 um (C15) channels.
    T = T13 + SPLIT_A * (T13 - T15) + offset
The offset is tuned by insights.py against the water temperatures NWS offices report for the Jersey shore
(~/goes/sst-bias.json); until it has enough matches it is 0. Cloudy pixels are found from the C13-C15 difference,
local texture, daytime C02 brightness and night-time C13-C07, and are left out.

Radar: the RADNTHES.GIF mosaic that arrives over EMWIN is a plain image, so it is georeferenced by an affine fit
to the Natural Earth coastline (within about 5 km) and its colours are mapped back to dBZ with the legend bar.
"""
import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

HOME_DIR = Path("/home/pi/goes")
EMWIN = HOME_DIR / "data" / "emwin"
BIAS_FILE = HOME_DIR / "sst-bias.json"

SPLIT_A = 1.0                  # split-window coefficient on (T13 - T15)
MAX_DIFF = 4.0                 # K; a larger T13-T15 means thin cirrus or heavy moisture, not a surface
TEXTURE_WATER, TEXTURE_LAND = 0.7, 1.6      # K of 3x3 standard deviation allowed in a clear pixel
DAY_COSZ = 0.35                # sun about 20 degrees up: the C02 test is trustworthy
DAY_REFL = 0.10                # C02 count/255 above this over a surface that is normally dark means cloud
NIGHT_COSZ = 0.0
NIGHT_STRATUS_K = 1.5          # T13 - T07 above this at night means water-droplet cloud (3.9 um emissivity < 1)

# colour ramp for surface temperature, degrees F -> RGB
RANGE_F = (30.0, 85.0)
STOPS_F = [(30, (40, 30, 150)), (40, (30, 110, 220)), (48, (40, 190, 230)), (55, (50, 200, 120)), (62, (190, 225, 60)),
           (70, (250, 200, 40)), (78, (240, 120, 30)), (85, (200, 30, 40))]

# NWS coastal observations we can check against: name -> (lat, lon)
SST_POINTS = {"Sandy Hook": (40.46, -74.00), "Manasquan": (40.10, -74.03), "Atlantic City": (39.35, -74.42)}


def k_to_f(k):
    return (k - 273.15) * 9 / 5 + 32


def offset_k():
    try:
        return float(json.loads(BIAS_FILE.read_text()).get("offset_k", 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0


def _box_std(a):
    """Standard deviation over each 3x3 neighbourhood (edges reuse the border pixels)."""
    p = np.pad(a, 1, mode="edge")
    s = np.zeros_like(a, dtype=np.float64)
    s2 = np.zeros_like(a, dtype=np.float64)
    h, w = a.shape
    for dy in range(3):
        for dx in range(3):
            v = p[dy:dy + h, dx:dx + w].astype(np.float64)
            s += v
            s2 += v * v
    m = s / 9
    return np.sqrt(np.maximum(s2 / 9 - m * m, 0))


def skin_temp(t13, t15, t07=None, refl=None, cosz=None, land=None):
    """(raw temperature K without the calibration offset, clear-sky mask) for one window."""
    d = t13 - t15
    raw = t13 + SPLIT_A * d
    tex_limit = TEXTURE_WATER if land is None else np.where(land > 0.5, TEXTURE_LAND, TEXTURE_WATER)
    clear = (d > -0.6) & (d < MAX_DIFF) & (t13 > 245) & (_box_std(t13) < tex_limit)
    if cosz is not None:
        if refl is not None:
            clear &= ~((cosz > DAY_COSZ) & (refl > DAY_REFL))
        if t07 is not None:
            clear &= ~((cosz < NIGHT_COSZ) & ((t13 - t07) > NIGHT_STRATUS_K))
    return raw, clear


def temp_rgb(temp_k):
    lo, hi = RANGE_F
    f = np.clip((k_to_f(temp_k) - lo) / (hi - lo), 0, 1)
    xs = [(s - lo) / (hi - lo) for s, _ in STOPS_F]
    return np.stack([np.interp(f, xs, [c[i] for _, c in STOPS_F]) for i in range(3)], axis=-1).astype(np.float32) / 255.0


def legend_ticks():
    return [(f, tuple(c)) for f, c in STOPS_F[::1]]


# ---- radar ---------------------------------------------------------------------------------------------------

RADAR_FIT = [38.22113, 1.54702, 3040.9052, 1.77387, -43.65661, 2300.9176]    # lon, lat -> (x, y) in the 600x571 GIF
RADAR_RE = re.compile(r"_(\d{14})_\d+-\d-RADNTHES\.GIF$")
LEGEND_ROW, LEGEND_X = 551, np.arange(138, 332)         # the colour bar; x -> dBZ below
MAP_ROWS = (28, 545)
DARK_RED_MIN_DBZ = 54          # roads share this colour band; only thick blobs of it count as rain


def radar_files():
    """[(UTC time, path)] of the Northeast radar mosaics, oldest first."""
    out = []
    try:
        names = [n for n in EMWIN.iterdir() if n.name.endswith("RADNTHES.GIF")]
    except OSError:
        return out
    for p in names:
        m = RADAR_RE.search(p.name)
        if m:
            out.append((dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc), p))
    out.sort()
    return out


def radar_pixels(lat, lon):
    """lat/lon degrees (arrays) -> (x, y) in the radar image."""
    a, b, c, d, e, f = RADAR_FIT
    return a * lon + b * lat + c, d * lon + e * lat + f


def radar_dbz(path):
    """600x571 float array of dBZ (NaN where there is no echo), from the image colours."""
    im = Image.open(path)
    idx = np.asarray(im.convert("P") if im.mode != "P" else im)
    pal = np.asarray(im.getpalette() or [], dtype=np.int16)
    pal = np.pad(pal, (0, max(0, 768 - len(pal))))[:768].reshape(256, 3)
    rgb = pal[idx]
    legend = rgb[LEGEND_ROW, LEGEND_X]
    dbz = -30 + (LEGEND_X - 35) / 2.38
    dist = np.abs(pal[:, None, :] - legend[None, :, :]).sum(2)          # 256 palette colours x legend columns
    lut = np.where(dist.min(1) <= 8, dbz[dist.argmin(1)], np.nan).astype(np.float32)
    out = lut[idx]
    h, w = out.shape
    out[:MAP_ROWS[0]] = np.nan
    out[MAP_ROWS[1]:] = np.nan
    echo = ~np.isnan(out)
    p = np.pad(echo, 1)
    nb = sum(p[dy:dy + h, dx:dx + w].astype(np.int8) for dy in range(3) for dx in range(3) if (dy, dx) != (1, 1))
    road = (out >= DARK_RED_MIN_DBZ) & (nb < 6)
    out[road] = np.nan
    return out


def radar_sample(dbz, lat, lon, radius_km=0.0):
    """(centre dBZ, max dBZ within radius_km) at a point, NaN if the point is off the image or has no echo."""
    x, y = radar_pixels(np.float64(lat), np.float64(lon))
    xi, yi = int(round(float(x))), int(round(float(y)))
    if not (0 <= xi < dbz.shape[1] and 0 <= yi < dbz.shape[0]):
        return float("nan"), float("nan")
    r = int(round(radius_km / 2.4))
    win = dbz[max(0, yi - r):yi + r + 1, max(0, xi - r):xi + r + 1]
    mx = float(np.nanmax(win)) if win.size and not np.all(np.isnan(win)) else float("nan")
    return float(dbz[yi, xi]), mx


def radar_on_grid(dbz, lat_deg, lon_deg):
    """dBZ resampled (nearest pixel) onto a lat/lon grid; NaN outside the image."""
    x, y = radar_pixels(lat_deg, lon_deg)
    xi, yi = np.rint(x).astype(int), np.rint(y).astype(int)
    ok = (xi >= 0) & (xi < dbz.shape[1]) & (yi >= 0) & (yi < dbz.shape[0])
    out = np.full(lat_deg.shape, np.nan, np.float32)
    out[ok] = dbz[yi[ok], xi[ok]]
    return out


_DBZ_STOPS = [(10, (60, 160, 90)), (20, (30, 190, 40)), (30, (20, 120, 20)), (40, (240, 220, 0)), (50, (250, 140, 0)),
              (55, (240, 20, 20)), (65, (220, 40, 220)), (75, (255, 255, 255))]


def dbz_rgb(dbz):
    """Radar colours drawn over the satellite picture (not the NWS palette, so the map underneath stays readable)."""
    xs = [s for s, _ in _DBZ_STOPS]
    return np.stack([np.interp(np.nan_to_num(dbz, nan=0), xs, [c[i] for _, c in _DBZ_STOPS]) for i in range(3)],
                    axis=-1).astype(np.float32) / 255.0
