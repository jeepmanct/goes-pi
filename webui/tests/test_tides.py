"""Run: python3 tests/test_tides.py. Surge, flood category and push logic with made-up water levels (no network)."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import tides

tides._flood.update(t=__import__("time").time(), levels={"nws_minor": 10.9, "nws_moderate": 12.4, "nws_major": 13.4})
now = 1_000_000.0
pred = [[now - 3600 + i * 720, 6.0] for i in range(400)]
obs = [[now - 3600 + i * 720, 7.0] for i in range(6)]       # water 1 ft above prediction, last sample 55 min ago
obs.append([now - 120, 7.0])
hilo = [{"t": now + 3 * 3600, "v": 8.2, "type": "H"}, {"t": now + 9 * 3600, "v": 0.0, "type": "L"},
        {"t": now + 15 * 3600, "v": 10.2, "type": "H"}]
surge, highs = tides.surge_and_highs(pred, obs, hilo, now)
assert surge == 1.0, surge
assert [h["category"] for h in highs] == ["statement", "minor"], highs      # 9.2 ft and ~11.1 ft estimated
assert tides.classify(8.4, tides.flood_levels()) is None and tides.classify(13.5, tides.flood_levels()) == "major"
tides._cache.update(t=__import__("time").time(), data={"surge_ft": surge, "highs": highs})
sent = []
tides.check(lambda *a, **k: sent.append(a[0]), now)
assert sent == ["Coastal flooding possible: statement", "Coastal flooding possible: minor"], sent
print("ok")
