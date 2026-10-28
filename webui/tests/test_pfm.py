"""Run: python3 tests/test_pfm.py. Parses a saved PFM block whose 3-hourly rows are one value short (Oct 2026)."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import local

fx = pathlib.Path(__file__).with_name("pfm_short_row.txt")
local._text = lambda name: fx.read_text()
local._newest = lambda *a, **k: [{"name": "x", "t": 0}]
local._products = lambda: type("P", (), {"ugc_zones": staticmethod(lambda b: ["CTZ009"])})()
pf = local.point_forecast(keep_past=True)
assert pf and pf["points"], "no points"
p3 = [p for p in pf["points"] if p["step_h"] == 3]
assert len(p3) == 22, len(p3)
assert p3[0]["temp"] == 43 and p3[0]["wind_mph"] == 6 and p3[0]["wind_dir"] == "NE", p3[0]
assert p3[20]["temp"] == 55 and p3[20]["wind_mph"] == 10, p3[20]
p6 = [p for p in pf["points"] if p["step_h"] == 6]
assert [p["wind_dir"] for p in p6[:4]] == [None, "NW", None, "NW"], [p["wind_dir"] for p in p6[:4]]
print("ok")
