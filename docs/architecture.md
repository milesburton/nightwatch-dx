# Architecture

dx-watch is built around four Docker services that communicate over a Docker bridge network.

```
RTL-SDR USB dongle
        │
        ▼
┌───────────────────┐
│   rtl-bridge      │  rtl_tcp + Python multiplexer
│   :1235 TCP mux   │  Broadcasts raw IQ to all TCP subscribers
│   :1236 WS        │──────────────────────────────────────────► Browser iqWorker.ts
└───────┬───────────┘                                            (FFT / waterfall)
        │
        ├──────────────────┬───────────────────┐
        ▼                  ▼                   ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│  cw-decoder  │  │ sstv-decoder │  │ easypal-decoder  │
│  :8765 /ws/cw│  │:8766 /ws/sstv│  │:8767 /ws/easypal │
└──────┬───────┘  └──────┬───────┘  └────────┬─────────┘
       │                 │                   │
       └─────────────────┴───────────────────┘
                         │
                         ▼
        ┌────────────────────────────────┐
        │        nginx  :8080            │
        │  WaterfallPanel  CWLogPanel    │
        │  SSTVGalleryPanel EasyPalPanel │
        └────────────────────────────────┘
```

## Services

### rtl-bridge

Runs `rtl_biast` (bias-T to power the upconverter), then `rtl_tcp`, then a Python multiplexer that:

- Accepts multiple simultaneous TCP connections using the rtl_tcp wire protocol
- Broadcasts raw IQ bytes non-blocking (slow clients drop chunks rather than stalling the upstream reader)
- Serves a WebSocket endpoint for the browser iqWorker (raw uint8 IQ stream)

**Ports (internal):** 1234 (rtl_tcp upstream), 1235 (TCP mux), 1236 (WebSocket IQ)

### cw-decoder

Connects to the TCP multiplexer on :1235 and runs a full CW signal chain in Python:

1. Frequency shift (mix down to CW offset: 14.029 MHz → DC)
2. Two-stage FIR decimation: 2.4 Msps → 240 kHz → 24 kHz
3. Narrow Kaiser FIR bandpass ±500 Hz around DC (rejects adjacent SSB / other CW)
4. Asymmetric IIR envelope (fast attack 0.5 ms, fast decay 0.2 ms)
5. Schmitt trigger with adaptive threshold (p10/p90 midpoint of 3-second window)
6. Morse timing decoder with adaptive dit estimation (5–40 WPM)
7. Character lookup; unrecognised sequences bracketed as `[.--.-]`

Broadcasts JSON over WebSocket on :8765, proxied by nginx to `/ws/cw`.

### sstv-decoder

Connects to the TCP multiplexer on :1235 and processes the 14.230 MHz SSTV sub-band:

1. Mix to SSTV offset (+55 kHz from RF centre)
2. Two-stage FIR decimation to 24 kHz audio rate
3. FM discriminator → instantaneous frequency in Hz
4. VIS tone detector state machine (leader / break / start / bits / stop)
5. Robot 36 pixel decode (YUV → RGB); generic greyscale fallback for other modes
6. PNG encode via Pillow → base64 data URL

Broadcasts JSON with base64 PNG data URLs over WebSocket on :8766, proxied to `/ws/sstv`.

### easypal-decoder

Connects to the TCP multiplexer on :1235 and processes the 14.233 MHz EasyPal sub-band
using the DRM (Digital Radio Mondiale) Mode B physical layer:

1. Mix to EasyPal offset (+58 kHz from RF centre)
2. Two-stage FIR decimation to 24 kHz, then FM discriminator
3. Resample to 12 kHz DRM internal rate; phase-integrate back to complex signal
4. Guard-interval correlation OFDM sync
5. 256-point FFT; extract 29 active carriers (k = −10 … +18)
6. Pilot-based channel estimation + equalisation
7. 16-QAM hard-decision demapping
8. Frequency + time deinterleave
9. Rate-1/2 Viterbi FEC decode (from rate-1/6 convolutional with puncturing)
10. CRC-16 MSC segment reassembly → JPEG → PNG

Broadcasts JSON with base64 PNG data URLs over WebSocket on :8767, proxied to `/ws/easypal`.

### ui

Nginx serving the Vite/React SPA. Proxies:

- `/ws/iq` → rtl-bridge :1236 (WebSocket IQ stream for waterfall)
- `/ws/cw` → cw-decoder :8765
- `/ws/sstv` → sstv-decoder :8766
- `/ws/easypal` → easypal-decoder :8767
- `/ws/logs` → cw-decoder :8765 (browser log shipping)

## Browser

The browser runs a single Web Worker (`iqWorker.ts`) that connects to `/ws/iq`, receives raw IQ bytes, and computes FFT bins for the waterfall and spectrum display. All signal decoding runs in the Python backend — the browser only renders what the backend sends.

## Data Flow Summary

| Path | Protocol | Content |
|---|---|---|
| RTL-SDR → rtl_tcp | USB | Raw IQ uint8 |
| rtl_tcp → multiplexer | TCP localhost | Raw IQ uint8 |
| multiplexer → decoders | TCP :1235 | Raw IQ uint8 (rtl_tcp protocol) |
| multiplexer → browser | WebSocket :1236 | Raw IQ uint8 |
| cw-decoder → browser | WebSocket :8765 | JSON char/word_space/status events |
| sstv-decoder → browser | WebSocket :8766 | JSON frame events (base64 PNG) |
| easypal-decoder → browser | WebSocket :8767 | JSON frame events (base64 PNG) |
