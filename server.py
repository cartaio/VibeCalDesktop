import asyncio
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime

import websockets

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

PREPARE_SETTLE_MS = 2000
SETTLE_MS = 1200
TIMEOUT_S = 20.0
COUNTDOWN_SECONDS = 15   # time to tap Connetti and place the phone

patch_queue: "queue.Queue[str]" = queue.Queue()

def hex_of(rgb):
    return '#%02x%02x%02x' % tuple(rgb)

async def handler(websocket):
    logging.info("✅ iPhone connected from %s", websocket.remote_address[0])
    try:
        handshake = json.loads(await websocket.recv())
        logging.info("👉 Handshake: %s", handshake)
        session = {"started_at": datetime.now().isoformat(),
                   "device": handshake.get("device"), "patches": []}

        # 0) COUNTDOWN: give the user time to place the phone on the display
        await websocket.send(json.dumps({"command": "countdown", "seconds": COUNTDOWN_SECONDS}))
        logging.info("👈 COUNTDOWN %ds — place the phone on the display NOW", COUNTDOWN_SECONDS)
        await asyncio.sleep(COUNTDOWN_SECONDS)
        
        # 1) PREPARE: phone locks AE/WB and blacks out its screen
        patch_queue.put('#000000')
        await websocket.send(json.dumps({"command": "prepare", "settle_ms": PREPARE_SETTLE_MS}))
        logging.info("👈 PREPARE (lock AE/WB, blackout screen)")
        try:
            ready = json.loads(await asyncio.wait_for(websocket.recv(), TIMEOUT_S))
        except asyncio.TimeoutError:
            logging.error("⚠️ no 'ready' from phone"); return
        if ready.get("type") != "ready":
            logging.error("⚠️ unexpected reply: %s", ready); return
        logging.info("👉 Phone ready")

        # 2) PATCH SEQUENCE
        for patch_id, rgb in SEQUENCE:
            patch_queue.put(hex_of(rgb))
            await websocket.send(json.dumps({
                "command": "display_patch", "patch_id": patch_id,
                "rgb": list(rgb), "settle_ms": SETTLE_MS}))
            logging.info("👈 PATCH %-10s %s", patch_id, hex_of(rgb))
            t0 = time.time()
            try:
                raw_reading = await asyncio.wait_for(websocket.recv(), TIMEOUT_S)
            except asyncio.TimeoutError:
                logging.error("⚠️ timeout on %s", patch_id)
                session["patches"].append({"patch_id": patch_id, "sent_rgb": list(rgb), "error": "timeout"})
                continue
            dt = (time.time() - t0) * 1000
            reading = json.loads(raw_reading)
            if reading.get("patch_id") != patch_id:
                logging.error("⚠️ mismatch: expected %s got %s", patch_id, reading.get("patch_id"))
            logging.info("📊 %-10s measured=%-22s σ=%.2f frames=%s (%.0f ms)",
                         patch_id, reading.get("raw_data"), reading.get("sigma", -1),
                         reading.get("frames", 0), dt)
            session["patches"].append({
                "patch_id": patch_id, "sent_rgb": list(rgb),
                "measured": reading.get("raw_data"),
                "sigma": reading.get("sigma"), "frames": reading.get("frames"),
                "latency_ms": round(dt, 1)})

        # 3) FINISH
        patch_queue.put('#000000')
        await websocket.send(json.dumps({"command": "finish"}))
        session["ended_at"] = datetime.now().isoformat()
        os.makedirs("sessions", exist_ok=True)
        fname = os.path.join("sessions", datetime.now().strftime("session_%Y%m%d_%H%M%S.json"))
        with open(fname, "w", encoding="utf-8") as fh:
            json.dump(session, fh, indent=2)
        logging.info("💾 Saved %s", fname)
        logging.info("🔌 Connection left open")
        async for msg in websocket:
            logging.debug("idle: %s", msg)
    except websockets.exceptions.ConnectionClosed:
        logging.info("❌ closed by peer")
    except Exception:
        logging.exception("⚠️ handler error")
    finally:
        patch_queue.put('#000000')

# ---------- fullscreen patch window (main thread) ----------
def ui_main():
    root = tk.Tk()
    root.title("VibeCal patch window")
    root.attributes('-fullscreen', True)
    root.attributes('-topmost', True)
    root.configure(bg='#000000')
    root.config(cursor='none')

    def poll():
        try:
            while True:
                root.configure(bg=patch_queue.get_nowait())
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
        logging.info("🚀 VibeCal sequencer v3 on :8765 — patch window fullscreen, ESC to quit")
        await asyncio.Future()

if __name__ == "__main__":
    threading.Thread(target=server_thread, daemon=True).start()
    ui_main()