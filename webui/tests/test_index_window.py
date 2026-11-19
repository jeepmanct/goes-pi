import datetime as dt
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import products


def name(recv_ts, pil="AFDEPZTX", ddhhmm=None):
    r = dt.datetime.fromtimestamp(recv_ts, dt.timezone.utc)
    return f"A_FXUS64KEPZ{ddhhmm or r.strftime('%d%H%M')}_C_KWIN_{r.strftime('%Y%m%d%H%M%S')}_130176-2-{pil}.TXT"


class IndexWindow(unittest.TestCase):
    def test_only_the_last_week_is_indexed_and_old_rows_age_out(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as d:
            fresh, old = name(now - 3600), name(now - 10 * 86400)
            for n in (fresh, old):
                (Path(d) / n).write_text("x")
            saved = (products.EMWIN, dict(products._seen), dict(products._gseen), dict(products._index))
            try:
                products.EMWIN = Path(d)
                products._seen.clear(); products._gseen.clear(); products._index["t"] = 0
                self.assertEqual([r.name for r in products._rows()], [fresh])
                self.assertNotIn(old, products._seen)              # never stored, so it costs no memory
                products.INDEX_DAYS = 0.0001                       # a window of seconds: the hour-old row falls out on the next refresh
                products._index["t"] = 0
                self.assertEqual(products._rows(), [])
            finally:
                products.INDEX_DAYS = 7
                products.EMWIN = saved[0]
                products._seen.clear(); products._seen.update(saved[1])
                products._gseen.clear(); products._gseen.update(saved[2])
                products._index.update(saved[3])


if __name__ == "__main__":
    unittest.main()
