"""
Spectrum / waterfall WebSocket service.

Reads raw IQ from rtl-bridge, computes averaged 1024-point FFT every ~100ms,
and broadcasts power spectrum (dBFS) to all WebSocket clients.

WebSocket message schema:
  {
    "type": "fft",
    "bins": [float, ...],    # 1024 dBFS values, DC-centred (bin 0 = lowest freq)
    "centerFreq": 14175000,  # actual RF centre after LO subtraction (Hz)
    "sampleRate": 2400000,   # Hz
    "ts": "ISO8601"
  }
  { "type": "status", "connected": bool, "centerFreq": int, "sampleRate": int }
"""

import asyncio
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from datetime import datetime, timezone

import numpy as np
import websockets

# ── Configuration ─────────────────────────────────────────────────────────────

RTL_HOST      = os.environ.get("RTL_HOST",     "rtl-bridge")
RTL_PORT      = int(os.environ.get("RTL_PORT", "1235"))
WS_PORT       = int(os.environ.get("WS_PORT",  "8767"))

LO_OFFSET_HZ  = int(os.environ.get("LO_OFFSET_HZ",  str(125_000_000)))
SDR_CENTER_HZ = int(os.environ.get("SDR_CENTER_HZ",  str(139_175_000)))
RF_CENTER_HZ  = SDR_CENTER_HZ - LO_OFFSET_HZ          # 14.175 MHz
SAMPLE_RATE   = int(os.environ.get("SAMPLE_RATE",     str(2_400_000)))
GAIN          = int(os.environ.get("GAIN",            "420"))

FFT_SIZE      = 1024
# Publish one averaged spectrum every FFT_AVERAGES FFT frames.
# At 2.4 Msps: 2400000/1024 ≈ 2344 FFTs/s → 100 avg → ~23 updates/s (fast enough)
FFT_AVERAGES  = int(os.environ.get("FFT_AVERAGES", "50"))

READ_BYTES    = 65536

logging.basicConfig(level=logging.INFO, format="%(asctime)s [spectrum] %(message)s")
log = logging.getLogger(__name__)

# Precompute Hann window and its power for dBFS scaling
_WINDOW       = np.hanning(FFT_SIZE).astype(np.float32)
_WINDOW_POWER = float(np.sum(_WINDOW ** 2))

# ── RTL-TCP helpers ───────────────────────────────────────────────────────────

def rtl_command(sock: socket.socket, cmd: int, param: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, param))


def connect_rtl_sync() -> socket.socket:
    delay = 1.0
    while True:
        try:
            sock = socket.create_connection((RTL_HOST, RTL_PORT), timeout=10)
            header = sock.recv(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Bad header: {header!r}")
            rtl_command(sock, 0x02, SAMPLE_RATE)
            rtl_command(sock, 0x04, GAIN)
            rtl_command(sock, 0x03, 1)
            log.info("Connected %s:%d RF %.4f MHz %d ksps gain=%.1f dB",
                     RTL_HOST, RTL_PORT, RF_CENTER_HZ / 1e6,
                     SAMPLE_RATE // 1000, GAIN / 10.0)
            return sock
        except (OSError, ValueError) as e:
            log.warning("Connect failed (%s), retry in %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ── Reader thread ─────────────────────────────────────────────────────────────

def iq_reader_thread(raw_q: queue.Queue, stop: threading.Event) -> None:
    while not stop.is_set():
        sock = connect_rtl_sync()
        try:
            while not stop.is_set():
                data = sock.recv(READ_BYTES)
                if not data:
                    log.warning("EOF, reconnecting…")
                    break
                try:
                    raw_q.put(data, timeout=2.0)
                except queue.Full:
                    pass   # drop silently — spectrum is best-effort
        except (OSError, ConnectionResetError) as e:
            log.warning("Read error: %s, reconnecting…", e)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if not stop.is_set():
            time.sleep(2)


# ── WebSocket server ──────────────────────────────────────────────────────────

CLIENTS: set = set()


async def broadcast(msg: dict) -> None:
    if not CLIENTS:
        return
    data = json.dumps(msg)
    await asyncio.gather(*[ws.send(data) for ws in list(CLIENTS)], return_exceptions=True)


async def ws_handler(websocket) -> None:
    CLIENTS.add(websocket)
    log.info("WS client connected (total: %d)", len(CLIENTS))
    try:
        await websocket.send(json.dumps({
            "type": "status",
            "connected": True,
            "centerFreq": RF_CENTER_HZ,
            "sampleRate": SAMPLE_RATE,
        }))
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        log.info("WS client disconnected (total: %d)", len(CLIENTS))


# ── Spectrum compute loop ─────────────────────────────────────────────────────

async def spectrum_loop(raw_q: queue.Queue) -> None:
    loop        = asyncio.get_running_loop()
    iq_acc      = np.zeros(0, dtype=np.complex64)
    power_acc   = np.zeros(FFT_SIZE, dtype=np.float64)
    fft_count   = 0

    log.info("FFT_SIZE=%d, FFT_AVERAGES=%d, update ~%.0fms",
             FFT_SIZE, FFT_AVERAGES,
             FFT_AVERAGES * FFT_SIZE / SAMPLE_RATE * 1000)

    while True:
        try:
            raw = await loop.run_in_executor(None, lambda: raw_q.get(timeout=1.0))
        except queue.Empty:
            continue

        # uint8 IQ → complex64
        u8    = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        iq    = ((u8[0::2] - 127.5) + 1j * (u8[1::2] - 127.5)) / 127.5
        iq_acc = np.concatenate([iq_acc, iq.astype(np.complex64)])

        # Consume as many FFT_SIZE windows as available
        while len(iq_acc) >= FFT_SIZE:
            window_data = iq_acc[:FFT_SIZE]
            iq_acc      = iq_acc[FFT_SIZE:]

            windowed = window_data * _WINDOW
            spectrum = np.fft.fftshift(np.fft.fft(windowed, FFT_SIZE))
            power    = (np.abs(spectrum) ** 2) / _WINDOW_POWER
            power_acc += power
            fft_count += 1

            if fft_count >= FFT_AVERAGES:
                avg  = power_acc / fft_count
                db   = 10.0 * np.log10(np.maximum(avg, 1e-12))
                await broadcast({
                    "type":       "fft",
                    "bins":       db.tolist(),
                    "centerFreq": RF_CENTER_HZ,
                    "sampleRate": SAMPLE_RATE,
                    "ts":         datetime.now(timezone.utc).isoformat(),
                })
                power_acc[:] = 0.0
                fft_count    = 0

        # Cap accumulator to avoid unbounded growth during dropout
        if len(iq_acc) > FFT_SIZE * 4:
            iq_acc = iq_acc[-FFT_SIZE * 2:]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("Spectrum service — WS :%d, RF %.4f MHz, %d ksps",
             WS_PORT, RF_CENTER_HZ / 1e6, SAMPLE_RATE // 1000)

    raw_q = queue.Queue(maxsize=32)
    stop  = threading.Event()
    threading.Thread(
        target=iq_reader_thread, args=(raw_q, stop),
        daemon=True, name="spectrum-iq-reader",
    ).start()

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket on :%d", WS_PORT)

    try:
        await asyncio.gather(ws_server.serve_forever(), spectrum_loop(raw_q))
    finally:
        stop.set()


if __name__ == "__main__":
    asyncio.run(main())
