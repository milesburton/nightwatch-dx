"""
SSTV decoder service.

Connects to the rtl-bridge TCP multiplexer (port 1235) and decodes SSTV on
14.230 MHz.

Signal chain:
  uint8 IQ → complex64 → mix by +55kHz → 10× FIR → 10× FIR
           → FM discriminator → VIS detector → image decoder → PNG

Broadcasts JSON messages over WebSocket (aiohttp) on WS_PORT (default 8766).

Outbound message types:
  {"type": "frame", "imageDataUrl": "data:image/png;base64,...",
   "mode": "Robot 36", "ts": "..."}
  {"type": "status", "connected": true}
  {"type": "status", "connected": false}
"""

import asyncio
import base64
import io
import json
import logging
import os
from datetime import UTC, datetime

import numpy as np
from aiohttp import web
from PIL import Image

# ── Configuration ──────────────────────────────────────────────────────────────

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8766"))

SDR_SAMPLE_RATE = 2_400_000
SDR_CENTER_HZ   = 139_175_000
LO_OFFSET_HZ    = 125_000_000
RF_CENTER_HZ    = SDR_CENTER_HZ - LO_OFFSET_HZ   # 14_175_000
SSTV_FREQ_HZ    = 14_230_000
SSTV_OFFSET_HZ  = SSTV_FREQ_HZ - RF_CENTER_HZ    # +55_000

DECIMATE1       = 10
DECIMATE2       = 10
INTERMEDIATE    = SDR_SAMPLE_RATE // DECIMATE1    # 240_000 Hz
AUDIO_RATE      = INTERMEDIATE // DECIMATE2        # 24_000 Hz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [sstv] %(message)s")
log = logging.getLogger(__name__)

# ── FIR filter builder ─────────────────────────────────────────────────────────

def kaiser_lowpass(cutoff: float, sample_rate: float, duration: float = 0.001, beta: float = 8.0) -> np.ndarray:
    num_taps = int(duration * sample_rate) | 1
    center   = (num_taps - 1) / 2
    norm_cut = 2.0 * cutoff / sample_rate
    x        = np.arange(num_taps) - center
    with np.errstate(invalid='ignore', divide='ignore'):
        sinc = np.where(x == 0, norm_cut, np.sin(np.pi * x * norm_cut) / (np.pi * x))
    taps = sinc * np.kaiser(num_taps, beta)
    return (taps / taps.sum()).astype(np.float32)

_taps1 = kaiser_lowpass(INTERMEDIATE / 2, SDR_SAMPLE_RATE)
_taps2 = kaiser_lowpass(AUDIO_RATE   / 2, INTERMEDIATE)

# ── LO oscillator ─────────────────────────────────────────────────────────────

class LOOscillator:
    """Phase-continuous complex LO for frequency downconversion.

    Generates exp(j·2π·f·t) vectorised over each call, maintaining
    phase across successive calls so the waveform is continuous.
    Phase is renormalised every call to prevent floating-point drift.
    """

    def __init__(self, freq_hz: float, sample_rate: float) -> None:
        self._step  = 2 * np.pi * freq_hz / sample_rate
        self._phase = 0.0

    def generate(self, n: int) -> np.ndarray:
        phases      = self._phase + self._step * np.arange(n)
        self._phase = float(phases[-1] + self._step) % (2 * np.pi)
        return np.exp(-1j * phases).astype(np.complex64)

# ── SSTV signal chain ──────────────────────────────────────────────────────────

class SSTVSignalChain:
    def __init__(self) -> None:
        self._lo     = LOOscillator(SSTV_OFFSET_HZ, SDR_SAMPLE_RATE)
        self._zi1_re = np.zeros(len(_taps1) - 1)
        self._zi1_im = np.zeros(len(_taps1) - 1)
        self._zi2_re = np.zeros(len(_taps2) - 1)
        self._zi2_im = np.zeros(len(_taps2) - 1)
        self._prev_re = 0.0
        self._prev_im = 0.0

    def process(self, raw: bytes) -> np.ndarray:
        """Returns FM-discriminated audio samples (instantaneous frequency in Hz)."""
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(samples) & 1:
            samples = samples[:-1]
        iq = ((samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)) / 127.5
        iq = iq.astype(np.complex64)

        lo    = self._lo.generate(len(iq))
        mixed = iq * lo

        from scipy.signal import lfilter
        re1, self._zi1_re = lfilter(_taps1, 1.0, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = lfilter(_taps1, 1.0, mixed.imag, zi=self._zi1_im)
        stage1 = (re1 + 1j * im1)[DECIMATE1 - 1::DECIMATE1]

        re2, self._zi2_re = lfilter(_taps2, 1.0, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = lfilter(_taps2, 1.0, stage1.imag, zi=self._zi2_im)
        audio = (re2 + 1j * im2)[DECIMATE2 - 1::DECIMATE2]

        # FM discriminator: inst_freq = atan2(Q[i]·I[i-1] - I[i]·Q[i-1],
        #                                      I[i]·I[i-1] + Q[i]·Q[i-1]) × sr / 2π
        i_arr = audio.real
        q_arr = audio.imag
        prev_i = np.empty(len(audio))
        prev_q = np.empty(len(audio))
        prev_i[0] = self._prev_re
        prev_q[0] = self._prev_im
        prev_i[1:] = i_arr[:-1]
        prev_q[1:] = q_arr[:-1]
        self._prev_re = float(i_arr[-1]) if len(i_arr) else self._prev_re
        self._prev_im = float(q_arr[-1]) if len(q_arr) else self._prev_im

        cross = q_arr * prev_i - i_arr * prev_q
        dot   = i_arr * prev_i + q_arr * prev_q
        inst  = np.arctan2(cross, dot) * AUDIO_RATE / (2 * np.pi)
        return inst.astype(np.float32)

# ── VIS detector ───────────────────────────────────────────────────────────────
# Mirrors SSTVVISDetector.ts exactly.

VIS_DURATIONS: dict[int, float] = {
    8:  240 * (0.009 + 0.003 + 0.15),             # Robot 36
    95: 496 * (0.02  + 0.00208 + 0.532),           # PD 120
    44: 256 * (0.004862 + 0.000572 + 3*0.146 + 2*0.000572),  # Martin M1
    60: 256 * (0.009 + 0.0015 + 3*0.138 + 0.0015), # Scottie S1
}

VIS_MODE_NAMES: dict[int, str] = {
    8:  'Robot 36',
    95: 'PD 120',
    44: 'Martin M1',
    60: 'Scottie S1',
}

VIS_FREQS    = [1100, 1200, 1300, 1900]
TOLERANCE    = 100   # Hz

def dominant_tone(samples: np.ndarray) -> int:
    mean = float(samples.mean())
    best_freq, best_dist = 0, float('inf')
    for f in VIS_FREQS:
        d = abs(mean - f)
        if d < best_dist:
            best_dist, best_freq = d, f
    return best_freq if best_dist <= TOLERANCE else 0


class VISDetector:
    WIN_MS       = 10
    LEADER_WINS  = 30   # 300 ms

    def __init__(self, sample_rate: int = AUDIO_RATE) -> None:
        self._sr       = sample_rate
        self._win_size = round(self.WIN_MS * sample_rate / 1000)
        self._reset()

    def _reset(self) -> None:
        self._state     = 'IDLE'
        self._leader_n  = 0
        self._vis_bits: list[int] = []
        self._sub_cnt   = 0
        self._sub_tone  = 0
        self._buf: list[float] = []
        self._leader_idx = 0
        self._win_buf: list[float] = []

    def push(self, samples: np.ndarray) -> tuple[np.ndarray, int] | None:
        """Feed audio samples. Returns (audio_array, vis_code) when frame complete."""
        for s in samples:
            self._buf.append(float(s))
            if self._state == 'BUFFERING':
                needed = self._frame_samples
                pos    = len(self._buf) - self._preamble_end
                if pos >= needed:
                    out = np.array(self._buf[self._leader_idx:], dtype=np.float32)
                    code = self._vis_code
                    self._reset()
                    return out, code
                continue

            self._win_buf.append(float(s))
            if len(self._win_buf) < self._win_size:
                continue
            tone = dominant_tone(np.array(self._win_buf, dtype=np.float32))
            self._win_buf = []
            self._process_window(tone)

        return None

    def _process_window(self, tone: int) -> None:
        s = self._state
        if s == 'IDLE':
            if tone == 1900:
                self._leader_idx = max(0, len(self._buf) - self._win_size)
                self._leader_n   = 1
                self._state      = 'LEADER'
        elif s == 'LEADER':
            if tone == 1900:
                self._leader_n += 1
            elif tone == 1200 and self._leader_n >= self.LEADER_WINS:
                self._state = 'BREAK'
            else:
                self._reset()
        elif s == 'BREAK':
            if tone == 1900:
                self._sub_cnt  = 1
                self._sub_tone = 1900
                self._state    = 'START'
            else:
                self._reset()
        elif s == 'START':
            if tone == self._sub_tone:
                self._sub_cnt += 1
                if self._sub_cnt >= 3:
                    self._vis_bits = []
                    self._sub_cnt  = 0
                    self._sub_tone = 0
                    self._state    = 'VIS_BITS'
            else:
                self._reset()
        elif s == 'VIS_BITS':
            self._process_vis(tone)
        elif s == 'STOP':
            if tone == 1200:
                self._sub_cnt += 1
                if self._sub_cnt >= 3:
                    vis_code = self._parse_vis()
                    duration = VIS_DURATIONS.get(vis_code)
                    if duration is not None:
                        self._vis_code        = vis_code
                        self._preamble_end    = len(self._buf)
                        self._frame_samples   = round(duration * self._sr)
                        self._state           = 'BUFFERING'
                    else:
                        log.info("Unknown VIS code %d, ignoring", vis_code)
                        self._reset()
            else:
                self._reset()

    def _process_vis(self, tone: int) -> None:
        if self._sub_cnt == 0:
            if tone in (1100, 1300):
                self._sub_tone = tone
                self._sub_cnt  = 1
            elif tone == 1200 and len(self._vis_bits) == 8:
                self._sub_tone = 1200
                self._sub_cnt  = 1
                self._state    = 'STOP'
            else:
                self._reset()
        else:
            if tone == self._sub_tone:
                self._sub_cnt += 1
                if self._sub_cnt >= 3:
                    self._vis_bits.append(1 if self._sub_tone == 1100 else 0)
                    self._sub_cnt  = 0
                    self._sub_tone = 0
                    if len(self._vis_bits) == 8:
                        self._state   = 'STOP'
                        self._sub_cnt = 0
            else:
                self._reset()

    def _parse_vis(self) -> int:
        code = 0
        for i in range(7):
            code |= (self._vis_bits[i] << i)
        return code

# ── SSTV image decoder ─────────────────────────────────────────────────────────

FREQ_SYNC  = 1200.0
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0

def freq_to_pixel(freq: float) -> int:
    v = (freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK)
    return int(max(0.0, min(1.0, v)) * 255)


def decode_robot36(audio: np.ndarray, sr: int) -> Image.Image:
    """Minimal Robot 36 decoder → RGB PIL image."""
    lines   = 240
    width   = 320
    sync_ms = 9.0
    porch_ms = 3.0
    luma_ms  = 88.0
    chroma_ms = 44.0

    sync_s   = sync_ms  / 1000
    porch_s  = porch_ms / 1000
    luma_s   = luma_ms  / 1000
    chroma_s = chroma_ms / 1000
    pixels = np.zeros((lines, width, 3), dtype=np.uint8)

    # Find VIS end (1200 Hz start bit already consumed by VISDetector;
    # audio starts from leader. Scan for first sync pulse.)
    def find_sync(start_idx: int) -> int:
        window = int(sync_s * sr * 0.8)
        for i in range(start_idx, min(len(audio) - window, start_idx + int(sr * 2))):
            seg = audio[i:i + window]
            if float(seg.mean()) < (FREQ_SYNC + 100):
                return i
        return start_idx

    pos = find_sync(0)

    for row in range(lines):
        pos += int(sync_s * sr)    # skip sync pulse
        pos += int(porch_s * sr)   # skip porch

        luma_end = pos + int(luma_s * sr)
        if luma_end >= len(audio):
            break
        luma_seg   = audio[pos:luma_end]
        luma_vals  = np.interp(
            np.linspace(0, len(luma_seg), width, endpoint=False),
            np.arange(len(luma_seg)),
            luma_seg,
        )
        Y = np.clip((luma_vals - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK), 0, 1)
        pos = luma_end

        chroma_end = pos + int(chroma_s * sr)
        if chroma_end >= len(audio):
            pixels[row, :, 0] = (Y * 255).astype(np.uint8)
            pixels[row, :, 1] = (Y * 255).astype(np.uint8)
            pixels[row, :, 2] = (Y * 255).astype(np.uint8)
            pos = chroma_end
            continue
        chroma_seg  = audio[pos:chroma_end]
        chroma_vals = np.interp(
            np.linspace(0, len(chroma_seg), width // 2, endpoint=False),
            np.arange(len(chroma_seg)),
            chroma_seg,
        )
        C = np.clip((chroma_vals - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK), 0, 1) * 2 - 1
        C = np.repeat(C, 2)
        pos = chroma_end

        # Simple YUV→RGB (even rows = U, odd = V, Robot 36)
        if row % 2 == 0:
            U = C
            V = np.zeros_like(C)
        else:
            V = C
            U = np.zeros_like(C)

        R = np.clip(Y + 1.13983 * V, 0, 1)
        G = np.clip(Y - 0.39465 * U - 0.58060 * V, 0, 1)
        B = np.clip(Y + 2.03211 * U, 0, 1)

        pixels[row, :, 0] = (R * 255).astype(np.uint8)
        pixels[row, :, 1] = (G * 255).astype(np.uint8)
        pixels[row, :, 2] = (B * 255).astype(np.uint8)

    return Image.fromarray(pixels, 'RGB')


def decode_generic(audio: np.ndarray, sr: int, vis_code: int) -> Image.Image:
    """Fallback: render as greyscale luma strip (always produces something)."""
    height = 200
    width  = 320
    total  = len(audio)
    step   = max(1, total // (height * width))
    vals   = audio[::step][: height * width]
    if len(vals) < height * width:
        vals = np.pad(vals, (0, height * width - len(vals)))
    arr = np.clip((vals.reshape(height, width) - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK), 0, 1)
    grey = (arr * 255).astype(np.uint8)
    return Image.fromarray(grey, 'L').convert('RGB')


def decode_image(audio: np.ndarray, vis_code: int, sr: int) -> Image.Image:
    try:
        if vis_code == 8:
            return decode_robot36(audio, sr)
    except Exception as e:
        log.warning("Robot 36 decode failed: %s, falling back to generic", e)
    return decode_generic(audio, sr, vis_code)


def image_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"

# ── WebSocket broadcast hub ────────────────────────────────────────────────────

class Hub:
    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._connected = False

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)

    async def set_connected(self, connected: bool) -> None:
        self._connected = connected
        await self.broadcast({'type': 'status', 'connected': connected})

    async def send_status(self, ws: web.WebSocketResponse) -> None:
        await ws.send_str(json.dumps({'type': 'status', 'connected': self._connected}))

# ── IQ reader loop ─────────────────────────────────────────────────────────────

async def iq_reader(hub: Hub) -> None:
    chain   = SSTVSignalChain()
    detector = VISDetector(AUDIO_RATE)

    while True:
        try:
            log.info("Connecting to TCP mux at %s:%d…", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            header = await reader.readexactly(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Unexpected header: {header!r}")
            log.info("Connected. Listening for SSTV on %.3f MHz…", SSTV_FREQ_HZ / 1e6)
            await hub.set_connected(True)

            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                audio = chain.process(chunk)
                result = detector.push(audio)
                if result is not None:
                    frame_audio, vis_code = result
                    mode_name = VIS_MODE_NAMES.get(vis_code, f'VIS {vis_code}')
                    log.info("SSTV frame detected: %s (%d samples)", mode_name, len(frame_audio))
                    loop = asyncio.get_event_loop()
                    img  = await loop.run_in_executor(
                        None, decode_image, frame_audio, vis_code, AUDIO_RATE
                    )
                    data_url = image_to_data_url(img)
                    ts = datetime.now(UTC).isoformat()
                    await hub.broadcast({
                        'type': 'frame',
                        'imageDataUrl': data_url,
                        'mode': mode_name,
                        'ts': ts,
                    })

        except Exception as e:
            log.warning("Mux connection lost: %s, retrying in 5s…", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            await hub.set_connected(False)
        await asyncio.sleep(5)

# ── HTTP / WebSocket server ────────────────────────────────────────────────────

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app['hub']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.add(ws)
    await hub.send_status(ws)
    try:
        async for _ in ws:
            pass
    finally:
        hub.remove(ws)
    return ws


async def main() -> None:
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/sstv', ws_handler)

    asyncio.create_task(iq_reader(hub))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("SSTV decoder WebSocket listening on :%d /ws/sstv", WS_PORT)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
