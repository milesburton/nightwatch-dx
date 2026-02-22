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
        ├──────────────────────────────────────┐
        ▼                                      ▼
┌───────────────────┐              ┌───────────────────┐
│   cw-decoder      │              │   sstv-decoder    │
│   Python service  │              │   Python service  │
│   :8765 WebSocket │              │   :8766 WebSocket │
└─────────┬─────────┘              └─────────┬─────────┘
          │  /ws/cw                          │  /ws/sstv
          ▼                                  ▼
┌─────────────────────────────────────────────────────────┐
│                         nginx                           │
│                    ui container :80                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ WaterfallPanel│  │  CWLogPanel  │  │SSTVGalleryPanel│ │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
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

1. Frequency shift (mix down to CW offset)
2. Two-stage FIR decimation: 2.4 Msps → 240 kHz → 24 kHz audio rate
3. Bandpass Kaiser FIR around the CW tone
4. Asymmetric IIR envelope (fast decay so intra-element gaps register cleanly)
5. Schmitt trigger with adaptive threshold (p90/p5 SNR noise gate)
6. Morse timing decoder with adaptive dit estimation (5–40 WPM)
7. Character lookup and bracket-wrapping for unrecognised sequences

Broadcasts JSON over WebSocket on :8765, proxied by nginx to `/ws/cw`.

### sstv-decoder

Connects to the TCP multiplexer on :1235 and processes the 14.230 MHz SSTV sub-band:

1. Mix to SSTV offset (+55 kHz from RF centre)
2. FIR decimation to audio rate
3. FM discriminator → instantaneous frequency
4. VIS tone detection (Goertzel filter)
5. Robot 36/72, Scottie, Martin pixel decode
6. PNG encode via Pillow

Broadcasts JSON with base64 PNG data URLs over WebSocket on :8766, proxied to `/ws/sstv`.

### ui

Nginx serving the Vite/React SPA. Proxies:

- `/ws/iq` → rtl-bridge :1236 (WebSocket IQ stream for waterfall)
- `/ws/cw` → cw-decoder :8765
- `/ws/sstv` → sstv-decoder :8766

## Browser

The browser runs a single Web Worker (`iqWorker.ts`) that connects to `/ws/iq`, receives raw IQ bytes, and computes FFT bins for the waterfall and spectrum display. CW and SSTV decode are intentionally kept in the Python backend — the browser only renders what the backend sends.

## Data Flow Summary

| Path | Protocol | Content |
|---|---|---|
| RTL-SDR → rtl_tcp | USB | Raw IQ uint8 |
| rtl_tcp → multiplexer | TCP localhost | Raw IQ uint8 |
| multiplexer → decoders | TCP :1235 | Raw IQ uint8 (rtl_tcp protocol) |
| multiplexer → browser | WebSocket :1236 | Raw IQ uint8 |
| cw-decoder → browser | WebSocket :8765 | JSON char/word_space/status events |
| sstv-decoder → browser | WebSocket :8766 | JSON frame events (base64 PNG) |
