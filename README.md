# dx-watch

[![CI](https://github.com/milesburton/dx-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/milesburton/dx-watch/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](docker-compose.yml)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](services/cw-decoder)
[![React](https://img.shields.io/badge/React-19-61DAFB.svg)](ui)

Live HF signal monitoring over the 20m amateur band (14.0–14.35 MHz). Receives IQ samples from an RTL-SDR dongle with an HF upconverter, decodes CW (Morse code) and SSTV in real time, and displays everything in a browser dashboard.

DX is amateur radio shorthand for long-distance contact — signals reaching across continents via ionospheric reflection. This project watches for them.

## Architecture

```
RTL-SDR + HF upconverter
         │
         ▼
   rtl-bridge          TCP mux on :1235  (rtl_tcp-compatible)
         │
         ├──────────────────────────┐
         ▼                          ▼
   cw-decoder                sstv-decoder
   Python asyncio            Python asyncio
   WebSocket :8765           WebSocket :8766
         │                          │
         └──────────┬───────────────┘
                    ▼
              nginx (ui)
              HTTP :8080
         /ws/cw  → cw-decoder
         /ws/sstv → sstv-decoder
         /ws/iq   → rtl-bridge (FFT/waterfall)
```

The browser receives only processed output — the FFT worker runs in a Web Worker for the waterfall, while CW characters and SSTV frames arrive as JSON over WebSocket. No raw IQ data is sent to the browser.

## Features

- **Spectrum & waterfall** — 2.4 MHz wide, 400-row scrolling history, 20m band markers
- **CW decoder** — adaptive speed (5–40 WPM), Schmitt trigger envelope, noise gate, IndexedDB session history; hover any character to see the Morse dots and dashes
- **SSTV monitor** — Robot 36/72, Scottie, Martin mode detection and frame capture, gallery with IndexedDB persistence
- **Dynamic page title** — reflects current signal state (scanning / CW active / SSTV active / offline)

## Hardware

| Component | Notes |
|---|---|
| RTL-SDR dongle (RTL2832U + R820T/R820T2) | Any RTL-SDR will work |
| HF upconverter (125 MHz LO) | Required to receive HF on a VHF/UHF dongle |
| Bias-T capable dongle or separate power | Powers the upconverter |

The decoder is tuned to 14.175 MHz centre (20m band) with 2.4 MHz of bandwidth, covering 12.975–15.375 MHz. The CW decoder targets 14.029 MHz; SSTV monitors 14.230 MHz.

## Quick start

### Prerequisites

- Docker and Docker Compose
- RTL-SDR dongle with HF upconverter connected via USB

### Run

```bash
git clone https://github.com/milesburton/dx-watch.git
cd dx-watch
docker compose up -d --build
```

Open `http://localhost:8080` in your browser.

### Configuration

Environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the IQ multiplexer |
| `MUX_PORT` | `1235` | TCP port of the IQ multiplexer |
| `WS_PORT` | `8765` / `8766` | WebSocket port for each decoder |

SDR parameters (set in `docker/rtl-bridge/Dockerfile`):

| Parameter | Value | Description |
|---|---|---|
| Centre frequency | 139.175 MHz | SDR tunes here; upconverter shifts this to 14.175 MHz RF |
| Sample rate | 2.4 Msps | Covers ±1.2 MHz around centre |
| Gain | configurable | R820T gain in tenths of dB — adjust to suit your antenna |

## Development

### CW decoder (Python)

```bash
cd services/cw-decoder
pip install -r requirements.txt
python -m pytest test_cw_roundtrip.py -v
```

29 tests covering the full signal chain: FIR filter properties, envelope attack/decay behaviour, Morse decoder speed adaptation, noise gate, and end-to-end IQ → character round-trips.

### UI (React + TypeScript)

```bash
cd ui
npm install
npm run dev       # dev server with hot reload
npx tsc --noEmit  # type check
npx vitest run    # unit tests
```

### Docker

```bash
docker compose build
docker compose up -d
docker compose logs -f cw-decoder
```

## Signal chain

### CW decoder

```
IQ bytes (uint8, interleaved)
  → frequency shift (mix to baseband at CW offset)
  → two-stage FIR decimate ÷10 ÷10  (2.4 Msps → 24 kHz)
  → envelope detector (asymmetric IIR: fast decay, slower attack)
  → adaptive Schmitt trigger (threshold from p5/p90 of 3s window)
  → tone/gap timing → adaptive dit estimator (EWMA, 5–40 WPM)
  → Morse symbol buffer → MORSE_CODE lookup
  → JSON events over WebSocket
```

### SSTV decoder

```
IQ bytes
  → frequency shift to 14.230 MHz
  → decimate to 2.4 kHz audio
  → FM discriminator
  → VIS tone detection (Goertzel)
  → mode-specific pixel decode (Robot/Scottie/Martin)
  → PNG frame over WebSocket
```

## Project structure

```
docker/
  rtl-bridge/       Dockerfile for SDR hardware service
  cw-decoder/       Dockerfile for CW decoder
  sstv-decoder/     Dockerfile for SSTV decoder
  ui/               Dockerfile + nginx config for web UI
services/
  rtl-bridge/       Python: rtl_tcp multiplexer
  cw-decoder/       Python: CW signal chain + WebSocket server
  sstv-decoder/     Python: SSTV signal chain + WebSocket server
ui/
  src/
    components/     React panels (Waterfall, CWLog, SSTVGallery)
    workers/        iqWorker.ts — FFT in Web Worker
    utils/          IndexedDB wrapper, type helpers
```

## Licence

MIT
