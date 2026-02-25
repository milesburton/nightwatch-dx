# nightwatch-dx

[![CI](https://github.com/milesburton/nightwatch-dx/actions/workflows/ci.yml/badge.svg)](https://github.com/milesburton/nightwatch-dx/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](docker-compose.yml)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](services/cw-decoder)
[![React](https://img.shields.io/badge/React-19-61DAFB.svg)](ui)

Live HF signal monitoring over the 20m amateur band (14.0–14.35 MHz). Receives IQ samples from an RTL-SDR dongle with an HF upconverter, decodes CW (Morse code), PSK31, SSTV, and EasyPal in real time, and displays everything in a browser dashboard.

DX is amateur radio shorthand for long-distance contact — signals reaching across continents via ionospheric reflection. This project watches for them.

## Architecture

```
RTL-SDR + HF upconverter
         │
         ▼
   rtl-bridge     :1235 TCP mux (rtl_tcp-compatible, for waterfall)
                  :1236 WebSocket (raw IQ → browser FFT worker)
                  :1237 AudioMux → cw-decoder      (24 kHz, CW freq)
                  :1238 AudioMux → sstv-decoder    (24 kHz, SSTV freq)
                  :1239 AudioMux → easypal-decoder (24 kHz, EasyPal freq)
                  :1240 AudioMux → psk31-decoder   (24 kHz, PSK31 freq)
         │
         ├── cw-decoder      WebSocket :8765  /ws/cw
         ├── sstv-decoder    WebSocket :8766  /ws/sstv
         ├── easypal-decoder WebSocket :8767  /ws/easypal
         └── psk31-decoder   WebSocket :8768  /ws/psk31
                  │
                  ▼
            nginx (ui) :8080
```

rtl-bridge performs all IQ decimation centrally: one `AudioDecimator` per target frequency mixes the carrier to DC and decimates 100× (2.4 Msps → 24 kHz) using a two-stage Chebyshev IIR filter. Decoders receive pre-decimated complex64 audio — no raw IQ handling in the decoder services.

The browser receives only processed output — the FFT worker runs in a Web Worker for the waterfall, while decoded characters and image frames arrive as JSON over WebSocket.

## Features

- **Spectrum & waterfall** — 2.4 MHz wide, 400-row scrolling history, 20m band markers
- **CW decoder** — adaptive speed (5–40 WPM), Schmitt trigger envelope, noise gate, IndexedDB session history; hover any character to see the Morse dots and dashes
- **PSK31 decoder** — FFT carrier scan ±2 kHz, DBPSK demodulation, varicode decode; tabbed alongside CW log
- **SSTV monitor** — Robot 36/72, Scottie, Martin mode detection and frame capture, gallery with IndexedDB persistence
- **EasyPal monitor** — DRM Mode B OFDM demodulation, 16-QAM, Viterbi FEC, JPEG frame reassembly
- **Dynamic page title** — reflects current signal state (scanning / CW active / PSK31 active / SSTV active / offline)

## Hardware

| Component | Notes |
|---|---|
| RTL-SDR dongle (RTL2832U + R820T/R820T2) | Any RTL-SDR will work |
| HF upconverter (125 MHz LO) | Required to receive HF on a VHF/UHF dongle |
| Bias-T capable dongle or separate power | Powers the upconverter |

The decoder is tuned to 14.175 MHz centre (20m band) with 2.4 MHz of bandwidth. Target frequencies: CW 14.029 MHz, PSK31 14.070 MHz, SSTV 14.230 MHz, EasyPal 14.233 MHz.

## Quick start

### Prerequisites

- Docker and Docker Compose
- RTL-SDR dongle with HF upconverter connected via USB

### Run

```bash
git clone https://github.com/milesburton/nightwatch-dx.git
cd nightwatch-dx
docker compose up -d --build
```

Open `http://localhost:8080` in your browser.

### Configuration

See [docs/configuration.md](docs/configuration.md) for all environment variables.

SDR parameters (set in `docker/rtl-bridge/Dockerfile`):

| Parameter | Value | Description |
|---|---|---|
| Centre frequency | 139.175 MHz | SDR tunes here; upconverter shifts this to 14.175 MHz RF |
| Sample rate | 2.4 Msps | Covers ±1.2 MHz around centre |
| Gain | 19.7 dB | R820T gain in tenths of dB — adjust to suit your antenna |

## Development

### Python services

```bash
pip install -r requirements-dev.txt
pytest services/ -v
```

111 tests covering signal chain components across all four decoders plus the rtl-bridge AudioDecimator.

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
complex64@24kHz (from rtl-bridge AudioMux, CW freq mixed to DC)
  → narrow Kaiser FIR bandpass ±150 Hz
  → magnitude envelope
  → asymmetric IIR smoother (fast attack, faster decay)
  → adaptive Schmitt trigger (p10/p90 window)
  → run-length → adaptive dit estimator → MORSE_CODE lookup
  → JSON events over WebSocket
```

### PSK31 decoder

```
complex64@24kHz (from rtl-bridge AudioMux, PSK31 freq mixed to DC)
  → FFT carrier scan ±2 kHz (finds strongest carrier, re-runs every 5s)
  → fine mix to DC at carrier offset
  → Kaiser FIR matched filter (45 Hz lowpass)
  → symbol clock (768 samples/symbol at 31.25 baud)
  → differential BPSK: |Δφ| > π/2 → bit 0, else bit 1
  → varicode decode (two consecutive 0-bits = char boundary)
  → JSON events over WebSocket
```

### SSTV decoder

```
complex64@24kHz (from rtl-bridge AudioMux, SSTV freq mixed to DC)
  → FM discriminator → instantaneous frequency in Hz
  → VIS tone detection → mode-specific pixel decode
  → PNG frame over WebSocket
```

### EasyPal decoder

```
complex64@24kHz (from rtl-bridge AudioMux, EasyPal freq mixed to DC)
  → FM discriminator → resample to 12 kHz DRM rate
  → phase-integrate → complex baseband → mix −1500 Hz to DC
  → guard-interval OFDM sync → 256-point FFT
  → 29 active carriers → pilot channel equalisation → 16-QAM
  → deinterleave → Viterbi FEC → CRC MSC reassembly → JPEG → PNG
  → JSON frame over WebSocket
```

## Project structure

```
docker/
  rtl-bridge/       Dockerfile for SDR hardware + AudioMux service
  cw-decoder/       Dockerfile for CW decoder
  sstv-decoder/     Dockerfile for SSTV decoder
  easypal-decoder/  Dockerfile for EasyPal decoder
  psk31-decoder/    Dockerfile for PSK31 decoder
  ui/               Dockerfile + nginx config for web UI
services/
  rtl-bridge/       Python: rtl_tcp mux + AudioDecimator/AudioMux
  cw-decoder/       Python: CW signal chain + WebSocket server
  sstv-decoder/     Python: SSTV signal chain + WebSocket server
  easypal-decoder/  Python: EasyPal/DRM signal chain + WebSocket server
  psk31-decoder/    Python: PSK31 signal chain + WebSocket server
ui/
  src/
    components/     React panels (Waterfall, CWLog w/ PSK31 tab, SSTVGallery, EasyPalGallery)
    workers/        iqWorker.ts — FFT in Web Worker
    utils/          IndexedDB wrapper, type helpers
docs/
  architecture.md   Service architecture and data flow
  signal-chain.md   Detailed signal processing steps
  configuration.md  Environment variables reference
  local.md          Operational notes (ports, frequencies, diagnostics)
```

## CI/CD

Pushes to `main` trigger a GitHub Actions pipeline:

1. **Test** — `pytest services/ -v` on GitHub-hosted runner
2. **UI test** — TypeScript check + Vitest + Vite build
3. **Publish** — builds and pushes all six Docker images to GHCR
4. **Deploy** — self-hosted runner on the homelab server pulls new images and restarts services

## Licence

MIT
