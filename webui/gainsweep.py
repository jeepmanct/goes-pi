"""Tuner gain sweep: try several gains in turn, measure the lock at each, and report the best.

Runs in a background thread inside the panel. For each gain it writes goesrecv.conf, restarts goesrecv,
waits for the lock to settle, then records a few minutes of the per-second monitor stats. The first gain
is measured again at the end so drift in conditions during the test shows up. The original gain is always
put back afterwards (and after a panel restart in the middle of a sweep); applying a result is a separate step.
"""
import json
import os
import statistics
import threading
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "gainsweep.json"
PLAN = (50, 44, 40, 36, 30, 50)       # R820T steps round to 49.6 at the top; the repeat of 50 checks for drift
SETTLE, DWELL = 45, 150               # seconds
_lock = threading.Lock()
_abort = threading.Event()
_ctx = {}


def _save(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}


def init(procs, update_config, read_config):
    """Wire in the panel's helpers; put the gain back if a sweep was cut short by a restart."""
    _ctx.update(procs=procs, update=update_config, read=read_config)
    st = state()
    if st.get("state") == "running":
        try:
            update_config({"gain": st["original"]})
            st.update(state="interrupted", message=f"The panel restarted during the sweep; gain put back to {st['original']}.")
        except Exception as e:
            st.update(state="interrupted", message=f"The panel restarted during the sweep and the gain could not be restored: {e}")
        _save(st)


def _restart(gain=None):
    pr = _ctx["procs"]["goesrecv"]
    if gain is not None:
        _ctx["update"]({"gain": gain})
    if pr.running():
        pr.stop()
        time.sleep(1)
    pr.start()


def _wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if _abort.is_set():
            return False
        time.sleep(1)
    return True


def _measure(t0):
    pts = [p for p in list(_ctx["procs"]["goesrecv"].history) if p.get("t", 0) >= t0]
    n = len(pts)
    locked = [p for p in pts if (p.get("ok") or 0) > 0 and p.get("vit") is not None]
    r = {"n": n, "locked": round(len(locked) / n, 2) if n else 0.0,
         "ok": round(statistics.mean(p.get("ok") or 0 for p in pts), 1) if n else 0.0,
         "rs": int(sum(p.get("rs") or 0 for p in pts)), "drops": int(sum(p.get("drops") or 0 for p in pts)),
         "agc": round(statistics.median(p["gain"] for p in pts if p.get("gain") is not None), 1) if n and any(p.get("gain") is not None for p in pts) else None}
    r["vit"] = round(statistics.median(p["vit"] for p in locked)) if len(locked) >= max(30, 0.3 * n) else None
    return r


def _recommend(results):
    """Best gain by Viterbi errors among steps that held lock; keep the current one unless another is clearly better."""
    good = [r for r in results if r["vit"] is not None and r["locked"] >= 0.9]
    if not good:
        return {"gain": None, "text": "No step held a steady lock, so there is nothing to compare. Check the antenna and try again."}
    first = results[0]
    best = min(good, key=lambda r: r["vit"])
    note = ""
    rep = results[-1]
    if len(results) > 1 and rep["gain"] == first["gain"] and rep["vit"] and first["vit"]:
        drift = abs(rep["vit"] - first["vit"]) / first["vit"]
        if drift > 0.10:
            note = f" Conditions changed during the test (gain {first['gain']} read {first['vit']} at the start and {rep['vit']} at the end), so treat the differences as rough."
    if first["vit"] is None or best["gain"] == first["gain"] or best["vit"] >= first["vit"] * 0.93:
        return {"gain": first["gain"], "text": f"Keep the current gain. Nothing else beat {first['gain']} by a clear margin." + note}
    return {"gain": best["gain"], "text": f"Gain {best['gain']} gave the fewest errors ({best['vit']}, against {first['vit']} at {first['gain']})." + note}


def _run(plan, settle, dwell):
    st = state()
    orig = st["original"]
    try:
        for i, g in enumerate(plan):
            if _abort.is_set():
                break
            st.update(step=i, gain=g, phase="settling", phase_end=time.time() + settle)
            _save(st)
            _restart(g)
            if not _wait(settle):
                break
            t0 = time.time()
            st.update(phase="measuring", phase_end=t0 + dwell)
            _save(st)
            if not _wait(dwell):
                break
            r = _measure(t0)
            r["gain"] = g
            st["results"].append(r)
            _save(st)
        done = len(st["results"]) == len(plan)
        st.update(state="done" if done else "aborted", phase=None, recommend=_recommend(st["results"]) if st["results"] else None,
                  finished=time.time())
        st["message"] = "" if done else "Stopped early."
    except Exception as e:
        st.update(state="error", phase=None, message=f"{type(e).__name__}: {e}")
    finally:
        try:
            _restart(orig)
            st["restored"] = orig
        except Exception as e:
            st["message"] = (st.get("message") or "") + f" Could not restore gain {orig}: {e}"
        _save(st)
        _abort.clear()
        _lock.release()


def start(dwell=DWELL):
    """Begin a sweep. Returns an error string, or None when it has started."""
    pr = _ctx["procs"]["goesrecv"]
    if not pr.running():
        return "Start goesrecv first."
    recent = [p for p in list(pr.history)[-30:] if time.time() - p.get("t", 0) < 15]
    if not any((p.get("ok") or 0) > 0 for p in recent):
        return "goesrecv is not decoding right now, so there is nothing to compare. Wait for a lock first."
    if not _lock.acquire(blocking=False):
        return "A sweep is already running."
    try:
        dwell = max(60, min(int(dwell), 300))
        orig = _ctx["read"]()["gain"]
        if orig is None:
            raise ValueError("no gain set in goesrecv.conf")
        _save({"state": "running", "original": orig, "plan": list(PLAN), "settle": SETTLE, "dwell": dwell, "results": [],
               "started": time.time(), "step": 0, "phase": "settling", "gain": PLAN[0], "phase_end": time.time() + SETTLE})
        _abort.clear()
        threading.Thread(target=_run, args=(PLAN, SETTLE, dwell), daemon=True).start()
    except Exception as e:
        _lock.release()
        return f"Could not start: {e}"
    return None


def abort():
    if state().get("state") != "running":
        return "No sweep is running."
    _abort.set()
    return "Stopping; the original gain is being put back."


def apply(gain):
    """Set the tuner gain to one of the swept values and restart goesrecv."""
    if state().get("state") == "running":
        return "A sweep is still running."
    if gain not in {r["gain"] for r in state().get("results", [])}:
        return "That gain was not part of the last sweep."
    _restart(int(gain))
    return f"Gain set to {int(gain)} and goesrecv restarted."
