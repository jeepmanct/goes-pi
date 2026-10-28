"""Run: python3 tests/test_winter.py. Winter-stage logic on the saved PFM block with a snow row and alert injected."""
import pathlib, sys, time, tempfile, importlib.util
root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
for n, f in (("goes_products", "products.py"), ("goes_local", "local.py")):
    sp = importlib.util.spec_from_file_location(n, root / f); m = importlib.util.module_from_spec(sp); sys.modules[n] = m; sp.loader.exec_module(m)
import winter
local = sys.modules["goes_local"]

fx = pathlib.Path(__file__).with_name("pfm_short_row.txt").read_text()
snowy = fx.replace("Snow 12hr                 00-00       00-00", "Snow 12hr                 00-00         3-6")
local._text = lambda name: snowy
local._newest = lambda cat, *a, **k: [{"name": "x", "t": 0}] if cat == "PFM" else []
local._products = lambda: type("P", (), {"ugc_zones": staticmethod(lambda b: ["CTZ009"])})()
prod = sys.modules["goes_products"]
prod.active_alerts = lambda **k: []
now = 1793199600 - 3600     # Oct 28 2026, 10 AM EDT
ev = winter.evaluate(now)
assert ev["stage"] == "likely" and ev["snow"][0]["hi"] == 6, ev
prod.active_alerts = lambda **k: [{"name": "Winter Storm Watch", "phen": "WS", "level": "A", "ends": None}]
assert winter.evaluate(now)["stage"] == "alert"
winter.STATE = pathlib.Path(tempfile.mkdtemp()) / "s.json"
sent = []
push = lambda *a, **k: sent.append(a[0])
winter.in_season = lambda n=None: True
winter.check(push, now); winter.check(push, now)
assert sent == ["Winter alert for Stratford"], sent
import datetime as dt
winter = importlib.reload(winter)
day = lambda m, d: dt.datetime(2027, m, d, 12).timestamp()
assert [winter.in_season(day(*x)) for x in ((3, 31), (4, 1), (7, 4), (11, 1), (11, 2), (12, 25), (1, 15))] == [True, False, False, False, True, True, True]
print("ok")
