import asyncio
import json
import logging
import math
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime

import websockets

try:                                   # optional: needs numpy (pip install numpy)
    import vibecal_analysis as analysis
except Exception as _e:                # noqa: BLE001
    analysis = None
    _ANALYSIS_ERR = str(_e)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-7s | %(message)s')

SEQUENCE = [
    ("nero",      (0,   0,   0)),
    ("grigio_10", (26,  26,  26)),
    ("grigio_20", (51,  51,  51)),
    ("grigio_30", (77,  77,  77)),
    ("grigio_40", (102, 102, 102)),
    ("grigio_50", (128, 128, 128)),
    ("grigio_60", (153, 153, 153)),
    ("grigio_70", (179, 179, 179)),
    ("grigio_80", (204, 204, 204)),
    ("grigio_90", (230, 230, 230)),
    ("bianco",    (255, 255, 255)),
    ("rosso",     (255, 0,   0)),
    ("verde",     (0,   255, 0)),
    ("blu",       (0,   0,   255)),
    ("ciano",     (0,   255, 255)),
    ("magenta",   (255, 0,   255)),
    ("giallo",    (255, 255, 0)),
]

# ----------------------------------------------------------------------------------------
# Timing
# ----------------------------------------------------------------------------------------
PREPARE_SETTLE_MS = 2000
SETTLE_MS = 1200            # after a patch change
REPEAT_SETTLE_MS = 150      # same patch again (only the exposure changes)
TIMEOUT_S = 20.0            # waiting for 'ready'
READING_TIMEOUT_S = 60.0    # waiting for one patch reading (a whole exposure ladder)
COUNTDOWN_SECONDS = 15      # time to tap Connetti and place the phone

# ----------------------------------------------------------------------------------------
# Auto-setup (runs once, right after the phone is ready, before the real sequence)
# ----------------------------------------------------------------------------------------
AUTO_SETUP = True
DISPLAY_HZ = 60.0       # refresh rate of the monitor under test: long shutters are whole multiples of 1/DISPLAY_HZ
ISO_MIN = 20.0          # raised automatically if the camera minimum is higher
ISO_CAP = 800.0         # never go above this ISO (noise)
SHUTTER_MAX = 1 / 15    # longest shutter (the frame rate drops to 1/shutter)
T_WHITE = 190.0         # the SHORTEST exposure of the ladder puts white (255) at this pixel value
T_DARK = 45.0           # the LONGEST exposure puts the 10 % grey at this pixel value
STOP_ABOVE = 225.0      # during the sequence: stop climbing the ladder once a channel reaches this value
MIN_CONTRAST = 25.0     # white - black (green channel) below this = phone is not looking at the patch
PROBE_FRAMES = 5
PROBE_DISCARD = 3
LADDER_MAX = 10         # max number of exposures in the ladder
LADDER_MIN_STOPS = 4    # the ladder spans at least this many stops

# Ladder used when AUTO_SETUP = False: (id, ISO, shutter seconds)
FALLBACK_LADDER = [("e250", 20, 1 / 250), ("e60", 20, 1 / 60), ("e30", 20, 1 / 30), ("e15", 20, 1 / 15)]

BURST_FRAMES = 8        # frames averaged per exposure in the sequence
DISCARD_FRAMES = 3      # minimum frames dropped after each exposure change (the phone also checks EXIF)

# Grey patches tried (in this order of preference) as probe for the linearity test
PROBE_LEVELS = [("grigio_10", 26), ("grigio_20", 51), ("grigio_30", 77), ("grigio_40", 102), ("grigio_50", 128)]

patch_queue: "queue.Queue[str]" = queue.Queue()

# Comandi speciali per la finestra patch (oltre ai colori '#rrggbb')
SHOW_WINDOW = "__show__"
HIDE_WINDOW = "__hide__"


def hex_of(rgb):
    return '#%02x%02x%02x' % tuple(rgb)


class SetupError(Exception):
    pass


# ----------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------
def prod(shot):
    """ISO x shutter actually applied (falls back to the requested values)."""
    iso = shot.get("iso") or shot.get("req_iso") or 0
    sh = shot.get("shutter_s") or shot.get("req_shutter_s") or 0
    return iso * sh


def good_shots(reading):
    return [s for s in (reading.get("shots") or []) if not s.get("error") and s.get("frames", 0) >= 1]


def describe_shot(s):
    if s.get("error"):
        return "%s ERROR:%s" % (s.get("id"), s["error"])
    clip = "".join("RGB"[c] for c in range(3) if s.get("clip_high", [0, 0, 0])[c] >= 0.005)
    tags = (" !clip:" + clip) if clip else ""
    if s.get("exposure_stable") is False:
        tags += " UNSTABLE"
    if s.get("exif_match") is False:
        tags += " EXIF?"
    return "%s %s%s" % (s.get("id"), [round(v, 1) for v in s["mean"]], tags)


def crossing(shots, target):
    """Exposure product at which the brightest channel reaches `target` (log-interpolated)."""
    pts = sorted(((prod(s), max(s["mean"])) for s in shots if prod(s) > 0), key=lambda t: t[0])
    if not pts:
        return None, "no_data"
    if pts[0][1] >= target:
        return pts[0][0], "above_at_start"
    for (p0, z0), (p1, z1) in zip(pts, pts[1:]):
        if z1 >= target:
            if z1 - z0 <= 1e-6:
                return p1, "ok"
            f = (target - z0) / (z1 - z0)
            return math.exp(math.log(p0) + f * (math.log(p1) - math.log(p0))), "ok"
    return pts[-1][0], "below_range"


class Camera:
    """Limits of the phone camera + the rules that turn an exposure product into (ISO, shutter)."""

    def __init__(self, info):
        self.iso_min = max(ISO_MIN, float(info.get("min_iso") or 0))
        self.iso_cap = min(ISO_CAP, float(info.get("max_iso") or ISO_CAP))
        self.s_ref = 1.0 / DISPLAY_HZ
        self.s_max = SHUTTER_MAX
        self.s_min_safe = self.s_ref     # lowered by the probe
        self.iso_max = self.iso_min      # raised by the probe

    @property
    def p_cap(self):
        return self.iso_max * self.s_max

    def realize(self, p):
        """(ISO, shutter) for an exposure product p: lowest ISO, shutter a whole number of display
        periods when >= 1 period, never shorter than the shortest safe shutter."""
        period = 1.0 / DISPLAY_HZ
        s = min(p / self.iso_min, self.s_max)
        if s >= period * 0.999:
            s = math.floor(s / period + 1e-6) * period
        s = max(s, self.s_min_safe)
        iso = min(max(p / s, self.iso_min), self.iso_max)
        return iso, s


class Link:
    """One websocket + the patch window; one measurement = one display_patch round trip."""

    def __init__(self, ws):
        self.ws = ws
        self.patch = None

    async def measure(self, patch_id, rgb, exposures, frames, discard, stop_above=None):
        changed = patch_id != self.patch
        self.patch = patch_id
        patch_queue.put(hex_of(rgb))
        msg = {"command": "display_patch", "patch_id": patch_id, "rgb": list(rgb),
               "settle_ms": SETTLE_MS if changed else REPEAT_SETTLE_MS,
               "exposures": [{"id": e["id"], "iso": e["iso"], "shutter_s": e["shutter_s"]} for e in exposures],
               "burst_frames": frames, "discard_frames": discard}
        if stop_above is not None:
            msg["stop_above"] = stop_above
        await self.ws.send(json.dumps(msg))
        t0 = time.time()
        raw = await asyncio.wait_for(self.ws.recv(), READING_TIMEOUT_S)
        reading = json.loads(raw)
        if reading.get("patch_id") != patch_id:
            logging.error("⚠️ mismatch: expected %s got %s", patch_id, reading.get("patch_id"))
        reading["latency_ms"] = round((time.time() - t0) * 1000, 1)
        return reading


# ----------------------------------------------------------------------------------------
# Auto-setup steps
# ----------------------------------------------------------------------------------------
def evaluate_probe(shots, s_ref):
    """Equal-product tests. Every pair below has the same ISO x shutter, so the pixel value must be the
    same whatever the split: this needs no knowledge of the camera tone curve.
      - shutter tests: (ISO*k, s_ref/k) vs reference  -> shortest shutter that behaves linearly
      - ISO tests: (2*ISO, s_ref) vs (ISO, 2*s_ref), same for 4x -> ISO works as an exposure control"""
    by = {s["id"]: s for s in shots}

    def z(i):
        return by[i]["mean"][1] if i in by else None

    r1, r2 = z("ref1"), z("ref2")
    if r1 is None or r2 is None:
        raise SetupError("probe: reference shots missing")
    zref = (r1 + r2) / 2.0
    drift = abs(r1 - r2)
    tol = max(3.0, 0.03 * zref, 1.5 * drift)
    out = {"zref": zref, "drift": drift, "tol": tol, "shutter_tests": [], "iso_tests": []}

    s_min_safe, iso_ok = s_ref, by["ref1"]["iso"]
    chain_ok = True
    for k in (2, 4, 8, 16):
        i = "s%d" % k
        if i not in by:
            continue
        dz = by[i]["mean"][1] - zref
        ok = abs(dz) <= tol
        out["shutter_tests"].append({"id": i, "iso": by[i]["iso"], "shutter_s": by[i]["shutter_s"],
                                     "dz": dz, "ok": ok})
        chain_ok = chain_ok and ok
        if chain_ok:
            s_min_safe = by[i]["shutter_s"]
            iso_ok = max(iso_ok, by[i]["iso"])
    iso_linear = True
    for k in (2, 4):
        a, b = "i%da" % k, "i%db" % k
        if a in by and b in by:
            dz = by[a]["mean"][1] - by[b]["mean"][1]
            ok = abs(dz) <= tol
            out["iso_tests"].append({"x": k, "dz": dz, "ok": ok})
            iso_linear = iso_linear and ok
            if ok:
                iso_ok = max(iso_ok, by[a]["iso"])
    out.update(s_min_safe=s_min_safe, iso_linear=iso_linear, iso_ok_max=iso_ok if iso_linear else by["ref1"]["iso"])
    return out


async def auto_setup(link, cam):
    """Returns (setup dict, ladder list). Raises SetupError if the phone does not see the display."""
    warnings = []
    setup = {"ok": False, "warnings": warnings}
    ref = [{"id": "ref", "iso": cam.iso_min, "shutter_s": cam.s_ref}]

    def first(reading, what):
        shots = good_shots(reading)
        if not shots:
            raise SetupError("%s: the phone returned no valid shot (%s)" % (what, reading.get("shots")))
        return shots[0]

    # 1) Placement check: the phone must see a clear black -> white change -------------------
    black = first(await link.measure("setup_nero", (0, 0, 0), ref, PROBE_FRAMES, PROBE_DISCARD), "black")
    white = first(await link.measure("setup_bianco", (255, 255, 255), ref, PROBE_FRAMES, PROBE_DISCARD), "white")
    zb, zw = black["mean"][1], white["mean"][1]
    setup["placement"] = {"black": black["mean"], "white": white["mean"], "shot_black": black, "shot_white": white}
    logging.info("🔧 [1/5] placement: black G=%.1f, white G=%.1f at ISO %.0f 1/%.0f s",
                 zb, zw, cam.iso_min, 1 / cam.s_ref)
    if zw - zb < MIN_CONTRAST:
        raise SetupError("the phone sees almost no difference between black and white (G %.1f -> %.1f): "
                         "is it flat on the display, facing it, with the camera over the patch?" % (zb, zw))

    # 2) Probe patch: a grey whose reference reading is mid-scale -----------------------------
    idx = 2
    best = None
    for _ in range(4):
        name, level = PROBE_LEVELS[idx]
        shot = first(await link.measure(name, (level,) * 3, ref, PROBE_FRAMES, PROBE_DISCARD), "probe patch")
        z = shot["mean"][1]
        if best is None or abs(z - 80) < abs(best[1] - 80):
            best = (idx, z)
        if 55 <= z <= 110:
            break
        idx = max(0, idx - 1) if z > 110 else min(len(PROBE_LEVELS) - 1, idx + 1)
    idx, zprobe = best
    name, level = PROBE_LEVELS[idx]
    logging.info("🔧 [2/5] probe patch %s (G=%.1f at the reference exposure)", name, zprobe)

    # 3) Equal-product tests --------------------------------------------------------------------
    p0 = cam.iso_min * cam.s_ref
    seq = [("ref1", cam.iso_min, cam.s_ref)]
    for k in (2, 4, 8, 16):
        iso = cam.iso_min * k
        if iso <= cam.iso_cap:
            seq.append(("s%d" % k, iso, cam.s_ref / k))
    seq += [("ref2", cam.iso_min, cam.s_ref),
            ("i2a", 2 * cam.iso_min, cam.s_ref), ("i2b", cam.iso_min, 2 * cam.s_ref),
            ("i4a", 4 * cam.iso_min, cam.s_ref), ("i4b", cam.iso_min, 4 * cam.s_ref)]
    rd = await link.measure(name, (level,) * 3,
                            [{"id": i, "iso": iso, "shutter_s": s} for i, iso, s in seq],
                            PROBE_FRAMES, PROBE_DISCARD)
    shots = good_shots(rd)
    ev = evaluate_probe(shots, cam.s_ref)
    ev["patch"] = name
    ev["shots"] = shots
    setup["probe"] = ev
    cam.s_min_safe = ev["s_min_safe"]
    cam.iso_max = min(cam.iso_cap, max(cam.iso_min, ev["iso_ok_max"]))
    logging.info("🔧 [3/5] linearity probe on %s (reference G=%.1f, tolerance ±%.1f):", name, ev["zref"], ev["tol"])
    for t in ev["shutter_tests"]:
        logging.info("      shutter 1/%-5.0f ISO %-5.0f ΔG %+6.1f  %s", 1 / t["shutter_s"], t["iso"], t["dz"],
                     "ok" if t["ok"] else "FAIL")
    for t in ev["iso_tests"]:
        logging.info("      ISO x%d vs shutter x%d      ΔG %+6.1f  %s", t["x"], t["x"], t["dz"],
                     "ok" if t["ok"] else "FAIL")
    logging.info("      -> shortest safe shutter 1/%.0f s, ISO %s as exposure control (up to ISO %.0f)",
                 1 / cam.s_min_safe, "usable" if ev["iso_linear"] else "NOT linear, not used", cam.iso_max)
    if cam.s_min_safe > cam.s_ref / 2 + 1e-9:
        warnings.append("shutters shorter than 1/%.0f s do not behave linearly on this display (flicker?)"
                        % (1 / cam.s_min_safe))
    if not ev["iso_linear"]:
        warnings.append("ISO changes do not scale the signal like shutter changes: ladder uses shutter only")
    if ev["drift"] > 4:
        warnings.append("the reference reading drifted by %.1f levels during the probe" % ev["drift"])

    # 4) White window: shortest exposure of the ladder ---------------------------------------
    p_floor = cam.iso_min * cam.s_min_safe
    rungs = []
    for k in range(8):
        iso, s = cam.realize(p_floor * 2 ** k)
        if rungs and abs(iso * s - rungs[-1]["iso"] * rungs[-1]["shutter_s"]) < 1e-12:
            continue
        rungs.append({"id": "w%d" % k, "iso": round(iso, 2), "shutter_s": s})
    rd = await link.measure("bianco", (255, 255, 255), rungs, PROBE_FRAMES, PROBE_DISCARD, stop_above=T_WHITE)
    wshots = good_shots(rd)
    p_cross, status = crossing(wshots, T_WHITE)
    if p_cross is None:
        raise SetupError("white window: no valid shots")
    p_short = max(p_cross, p_floor)
    setup["white_window"] = {"status": status, "p_cross": p_cross, "p_floor": p_floor, "shots": wshots}
    logging.info("🔧 [4/5] white window: %s | G=%s -> shortest exposure product %.5f (ISO %.0f x 1/%.0f s)",
                 status, [round(max(s['mean']), 1) for s in wshots], p_short,
                 *(lambda a: (a[0], 1 / a[1]))(cam.realize(p_short)))
    if status == "above_at_start":
        zw0 = max(wshots[0]["mean"])
        if zw0 > 235:
            warnings.append("white is saturated (%.0f) even at the shortest safe exposure: lower the monitor "
                            "brightness, tilt the phone, or put a diffuser over the camera" % zw0)
        else:
            warnings.append("white already reads %.0f at the shortest safe exposure (target %.0f): the top of "
                            "the range will be compressed" % (zw0, T_WHITE))
    elif status == "below_range":
        warnings.append("white never reached %.0f within the search (display dim or phone far from it)" % T_WHITE)

    # 5) Dark window: longest exposure of the ladder -------------------------------------------
    rungs = []
    k = 0
    while True:
        iso, s = cam.realize(p_short * 2 ** k)
        p = iso * s
        if p > cam.p_cap * 1.0001 and rungs:
            break
        if not rungs or abs(p - rungs[-1]["iso"] * rungs[-1]["shutter_s"]) > 1e-12:
            rungs.append({"id": "d%d" % k, "iso": round(iso, 2), "shutter_s": s})
        k += 1
        if k > 14:
            break
    rd = await link.measure("grigio_10", (26, 26, 26), rungs, PROBE_FRAMES, PROBE_DISCARD, stop_above=T_DARK)
    dshots = good_shots(rd)
    p_dark, dstatus = crossing(dshots, T_DARK)
    if p_dark is None:
        raise SetupError("dark window: no valid shots")
    p_long = min(max(p_dark, p_short * 2 ** LADDER_MIN_STOPS), cam.p_cap)
    setup["dark_window"] = {"status": dstatus, "p_cross": p_dark, "shots": dshots}
    logging.info("🔧 [5/5] dark window (10 %% grey): %s | max channel=%s -> longest exposure product %.4f",
                 dstatus, [round(max(s['mean']), 1) for s in dshots], p_long)
    if dstatus == "below_range":
        warnings.append("the 10 %% grey stays below %.0f even at the longest allowed exposure: dark patches "
                        "will be noisy" % T_DARK)

    # Ladder: equal log spacing from p_short to p_long -----------------------------------------
    stops = math.log2(p_long / p_short)
    n = min(LADDER_MAX, int(math.ceil(stops - 1e-9)) + 1)
    step = stops / (n - 1) if n > 1 else 0.0
    ladder = []
    for k in range(n):
        iso, s = cam.realize(p_short * 2 ** (k * step))
        if ladder and abs(iso * s - ladder[-1]["iso"] * ladder[-1]["shutter_s"]) < 1e-12:
            continue
        ladder.append({"id": "x%d" % k, "iso": round(iso, 2), "shutter_s": s})
    setup.update(ok=True, s_min_safe=cam.s_min_safe, iso_max=cam.iso_max, p_short=p_short, p_long=p_long,
                 stops=stops, ladder=ladder)
    logging.info("🪜 ladder (%d exposures, %.1f stops):", len(ladder), stops)
    for r in ladder:
        logging.info("      %-3s ISO %-7.1f 1/%-6.1f s", r["id"], r["iso"], 1 / r["shutter_s"])
    for w in warnings:
        logging.warning("⚠️ %s", w)
    return setup, ladder


# ----------------------------------------------------------------------------------------
# Websocket handler
# ----------------------------------------------------------------------------------------
def save_session(session):
    session["ended_at"] = datetime.now().isoformat()
    os.makedirs("sessions", exist_ok=True)
    fname = os.path.join("sessions", datetime.now().strftime("session_%Y%m%d_%H%M%S.json"))
    with open(fname, "w", encoding="utf-8") as fh:
        json.dump(session, fh, indent=2)
    logging.info("💾 Saved %s", fname)
    return fname


async def handler(websocket):
    logging.info("✅ iPhone connected from %s", websocket.remote_address[0])
    try:
        handshake = json.loads(await websocket.recv())
        logging.info("👉 Handshake: %s", handshake)
        info = handshake.get("info") or {}
        if info:
            logging.info("📱 %s iOS %s | %s %s | ISO %s-%s | exposure %s-%s s | WB gain max %s",
                         info.get("model"), info.get("os"), info.get("camera"), info.get("format"),
                         info.get("min_iso"), info.get("max_iso"),
                         info.get("min_exposure_s"), info.get("max_exposure_s"), info.get("max_wb_gain"))
        session = {"started_at": datetime.now().isoformat(),
                   "device": handshake.get("device"),
                   "device_info": info,
                   "config": {"auto_setup": AUTO_SETUP, "display_hz": DISPLAY_HZ,
                              "t_white": T_WHITE, "t_dark": T_DARK, "stop_above": STOP_ABOVE,
                              "burst_frames": BURST_FRAMES, "discard_frames": DISCARD_FRAMES,
                              "settle_ms": SETTLE_MS, "prepare_settle_ms": PREPARE_SETTLE_MS},
                   "patches": []}

        # Il Connetti dell'iPhone è arrivato: ora compare la finestra patch (nera)
        patch_queue.put('#000000')
        patch_queue.put(SHOW_WINDOW)

        # 0) COUNTDOWN: give the user time to place the phone on the display
        await websocket.send(json.dumps({"command": "countdown", "seconds": COUNTDOWN_SECONDS}))
        logging.info("👈 COUNTDOWN %ds — place the phone on the display NOW", COUNTDOWN_SECONDS)
        await asyncio.sleep(COUNTDOWN_SECONDS)

        # 1) PREPARE: phone locks WB, blacks out its screen (exposures come with every patch)
        patch_queue.put('#000000')
        await websocket.send(json.dumps({"command": "prepare", "settle_ms": PREPARE_SETTLE_MS}))
        logging.info("👈 PREPARE (WB 1/1/1, blackout screen)")
        try:
            ready = json.loads(await asyncio.wait_for(websocket.recv(), TIMEOUT_S))
        except asyncio.TimeoutError:
            logging.error("⚠️ no 'ready' from phone"); return
        if ready.get("type") != "ready":
            logging.error("⚠️ unexpected reply: %s", ready); return
        logging.info("👉 Phone ready")

        link = Link(websocket)
        cam = Camera(info)

        # 2) AUTO-SETUP: find the exposure ladder that suits this display and this phone position
        if AUTO_SETUP:
            logging.info("🔧 AUTO-SETUP starting (about 30 s)")
            try:
                setup, ladder = await auto_setup(link, cam)
            except SetupError as e:
                logging.error("❌ AUTO-SETUP FAILED: %s", e)
                session["auto_setup"] = {"ok": False, "error": str(e)}
                patch_queue.put('#000000')
                patch_queue.put(HIDE_WINDOW)
                await websocket.send(json.dumps({"command": "finish"}))
                save_session(session)
                return
            except asyncio.TimeoutError:
                logging.error("❌ AUTO-SETUP FAILED: the phone stopped answering")
                return
        else:
            ladder = [{"id": i, "iso": iso, "shutter_s": s} for i, iso, s in FALLBACK_LADDER]
            setup = {"ok": True, "ladder": ladder, "note": "fixed ladder, auto-setup disabled"}
        session["auto_setup"] = setup
        session["ladder"] = ladder

        # 3) PATCH SEQUENCE
        for patch_id, rgb in SEQUENCE:
            logging.info("👈 PATCH %-10s %s", patch_id, hex_of(rgb))
            try:
                reading = await link.measure(patch_id, rgb, ladder, BURST_FRAMES, DISCARD_FRAMES,
                                             stop_above=STOP_ABOVE)
            except asyncio.TimeoutError:
                logging.error("⚠️ timeout on %s", patch_id)
                session["patches"].append({"patch_id": patch_id, "sent_rgb": list(rgb), "error": "timeout"})
                continue
            shots = reading.get("shots") or []
            for s in shots:
                if s.get("exposure_stable") is False or s.get("timed_out"):
                    logging.warning("⚠️ %s %s: unstable exposure or timeout", patch_id, s.get("id"))
            logging.info("📊 %-10s %d/%s exposures (%.0f ms): %s", patch_id, len(shots),
                         reading.get("planned_shots", "?"), reading["latency_ms"],
                         " | ".join(describe_shot(s) for s in shots))
            session["patches"].append({
                "patch_id": patch_id, "sent_rgb": list(rgb), "shots": shots,
                "planned_shots": reading.get("planned_shots"),
                "thermal_state": reading.get("thermal_state"),
                "phone_time": reading.get("phone_time"),
                "phone_measure_ms": reading.get("measure_ms"),
                "latency_ms": reading["latency_ms"]})

        # 4) FINISH
        patch_queue.put('#000000')
        patch_queue.put(HIDE_WINDOW)
        await websocket.send(json.dumps({"command": "finish"}))

        # 5) ANALYSIS (needs numpy): camera response from the grey ramp, linear light relative to white
        if analysis is not None:
            try:
                result = analysis.analyze(session)
                session["analysis"] = result
                for line in analysis.format_report(result).splitlines():
                    logging.info(line)
            except Exception:
                logging.exception("⚠️ analysis failed (raw data is saved anyway)")
        else:
            logging.warning("ℹ️ analysis skipped (%s). Run: pip install numpy, or analyse the saved "
                            "file later with vibecal_analysis.py", globals().get("_ANALYSIS_ERR", "module missing"))
        save_session(session)
        logging.info("🔌 Connection left open")
        async for msg in websocket:
            logging.debug("idle: %s", msg)
    except websockets.exceptions.ConnectionClosed:
        logging.info("❌ closed by peer")
    except Exception:
        logging.exception("⚠️ handler error")
    finally:
        patch_queue.put('#000000')
        patch_queue.put(HIDE_WINDOW)


# ---------- fullscreen patch window (main thread) ----------
def ui_main():
    root = tk.Tk()
    root.title("VibeCal patch window")
    root.configure(bg='#000000')
    root.config(cursor='none')
    root.withdraw()   # nascosta finché l'iPhone non preme Connetti

    def show():
        root.deiconify()
        root.attributes('-fullscreen', True)
        root.attributes('-topmost', True)
        root.lift()
        root.focus_force()

    def poll():
        try:
            while True:
                item = patch_queue.get_nowait()
                if item == SHOW_WINDOW:
                    show()
                elif item == HIDE_WINDOW:
                    root.withdraw()
                else:
                    root.configure(bg=item)
        except queue.Empty:
            pass
        root.after(16, poll)

    root.after(16, poll)
    root.bind('<Escape>', lambda e: root.destroy())
    root.mainloop()


def server_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_serve())


async def _serve():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        logging.info("🚀 VibeCal sequencer v5 on :8765 — patch window appears when the iPhone connects, ESC to quit")
        await asyncio.Future()


if __name__ == "__main__":
    threading.Thread(target=server_thread, daemon=True).start()
    ui_main()
