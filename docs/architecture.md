# Architecture

dx-watch is built around six Docker services that communicate over a Docker bridge network.

## Service overview

```
RTL-SDR USB dongle
        │
        ▼
┌─────────────────────────────────────────────┐
│                 rtl-bridge                  │
│                                             │
│  rtl_tcp (upstream, internal :1234)         │
│  Python multiplexer                         │
│                                             │
│  :1235  TCP mux  ──────────────────────────►│── backend decoders (legacy/direct)
│  :1236  WebSocket ─────────────────────────►│── browser iqWorker (FFT/waterfall)
│                                             │
│  AudioDecimator × 4  (100× decimation each) │
│  :1237  CW audio      (14.029 MHz → DC)     │
│  :1238  SSTV audio    (14.230 MHz → DC)     │
│  :1239  EasyPal audio (14.233 MHz → DC)     │
│  :1240  PSK31 audio   (14.070 MHz → DC)     │
└──────┬──────┬──────┬──────┬─────────────────┘
       │      │      │      │
       ▼      ▼      ▼      ▼
  ┌────────┐ ┌──────────┐ ┌──────────────┐ ┌──────────────┐
  │  cw-   │ │  sstv-   │ │  easypal-    │ │  psk31-      │
  │ decoder│ │ decoder  │ │  decoder     │ │  decoder     │
  │ :8765  │ │ :8766    │ │  :8767       │ │  :8768       │
  │/ws/cw  │ │/ws/sstv  │ │ /ws/easypal  │ │ /ws/psk31    │
  └────┬───┘ └────┬─────┘ └──────┬───────┘ └──────┬───────┘
       │          │               │                 │
       └──────────┴───────────────┴─────────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │        nginx  :8080       │
                    │  WaterfallPanel           │
                    │  CWLogPanel (CW + PSK31)  │
                    │  SSTVGalleryPanel         │
                    │  EasyPalGalleryPanel      │
                    │  ServerStatusPanel        │
                    └──────────────────────────┘
```

## Services

### rtl-bridge

Runs `rtl_biast` (bias-T to power the upconverter), then `rtl_tcp`, then a Python multiplexer that:

- Accepts multiple simultaneous TCP connections using the rtl_tcp wire protocol on `:1235`
- Broadcasts raw IQ bytes non-blocking to all connected clients (slow clients drop chunks)
- Serves a WebSocket endpoint on `:1236` for the browser iqWorker (raw uint8 IQ)
- Runs four `AudioMux` instances, one per decoder frequency:
  - Each `AudioMux` owns an `AudioDecimator` that mixes its target frequency to DC and decimates 100× (2.4 Msps → 24 kHz) using a two-stage Chebyshev IIR filter
  - Serves the resulting `complex64@24kHz` stream to decoder services over TCP
  - Uses a small queue; drops oldest chunk if a decoder falls behind

**Internal ports:** 1234 (rtl_tcp upstream), 1235 (TCP mux), 1236 (WebSocket IQ), 1237–1240 (AudioMux)

**AudioDecimator design:**
- Stage 1: Chebyshev IIR order-8 (0.05 dB ripple), cutoff 0.1 × Nyquist, decimate ×10 → 240 kHz
- Stage 2: same filter, decimate ×10 → 24 kHz
- IIR state is preserved across chunks for continuous phase

### cw-decoder

Connects to AudioMux on `:1237` and receives `complex64@24kHz` with the CW sub-band already mixed to DC. Runs the CW signal chain:

1. Narrow Kaiser FIR bandpass ±150 Hz around DC
2. Magnitude envelope
3. Asymmetric IIR envelope smoother (fast attack, faster decay to cleanly register gaps)
4. Adaptive Schmitt trigger (threshold from p10/p90 of 3-second window; SNR gate)
5. Run-length encoding → adaptive dit estimator (EWMA, 5–40 WPM)
6. Morse symbol decode; unrecognised sequences bracketed as `[.--.-]`

Broadcasts JSON over WebSocket on `:8765`, proxied by nginx to `/ws/cw`.

### sstv-decoder

Connects to AudioMux on `:1238` (SSTV sub-band mixed to DC). Runs the SSTV chain:

1. FM discriminator → instantaneous frequency in Hz
2. VIS tone detector state machine (leader / break / start / bits / stop)
3. Robot 36 pixel decode (YUV → RGB); generic greyscale fallback for other modes
4. PNG encode via Pillow → base64 data URL

Broadcasts JSON with base64 PNG data URLs over WebSocket on `:8766`, proxied to `/ws/sstv`.

### easypal-decoder

Connects to AudioMux on `:1239` (EasyPal sub-band mixed to DC). Runs the DRM Mode B chain:

1. FM discriminator → instantaneous frequency in Hz
2. Resample 24 kHz → 12 kHz DRM internal rate; phase-integrate back to complex signal
3. Mix −1500 Hz to centre EasyPal carrier at DC; DC-block IIR
4. Guard-interval correlation OFDM sync
5. 256-point FFT; extract 29 active carriers (k = −10 … +18)
6. Pilot-based channel estimation + equalisation
7. 16-QAM hard-decision demapping
8. Frequency + time deinterleave
9. Rate-1/2 Viterbi FEC decode (rate-1/6 convolutional with puncturing)
10. CRC-16 MSC segment reassembly → JPEG → PNG

Broadcasts JSON with base64 PNG data URLs over WebSocket on `:8767`, proxied to `/ws/easypal`.

### psk31-decoder

Connects to AudioMux on `:1240` (PSK31 sub-band mixed to DC). Runs the BPSK31 chain:

1. FFT carrier scan ±2 kHz (4096-point, finds peak bin; re-runs every 5 s to track drift)
2. Fine mix to DC at detected carrier offset
3. Kaiser FIR matched filter (45 Hz lowpass — passes full PSK31 ±15.6 Hz spectrum)
4. Symbol clock: one sample per 768-sample period (31.25 baud at 24 kHz)
5. Differential BPSK: `|Δφ| > π/2` → bit 0 (transition), else bit 1
6. Varicode decode: two consecutive 0-bits = character boundary; look up G3PLX table
7. SNR gate: if carrier SNR < 3× noise, suppress output

Broadcasts the same JSON schema as cw-decoder (`char`, `word_space` events) over WebSocket on `:8768`, proxied to `/ws/psk31`.

### ui

Nginx serving the Vite/React SPA. Proxies:

| Path | Backend | Content |
|---|---|---|
| `/ws/iq` | rtl-bridge :1236 | Raw IQ uint8 for waterfall FFT |
| `/ws/cw` | cw-decoder :8765 | JSON char/word_space events |
| `/ws/sstv` | sstv-decoder :8766 | JSON frame events (base64 PNG) |
| `/ws/easypal` | easypal-decoder :8767 | JSON frame events (base64 PNG) |
| `/ws/psk31` | psk31-decoder :8768 | JSON char/word_space events |

## Browser

The browser runs a single Web Worker (`iqWorker.ts`) that connects to `/ws/iq`, receives raw IQ bytes, and computes FFT bins for the waterfall and spectrum display. All signal decoding runs in the Python backend — the browser only renders what the backend sends.

CW and PSK31 sessions are persisted in IndexedDB (`sdr-monitor`, `cw-sessions` store) with a `mode` field to distinguish them. SSTV and EasyPal frames are persisted in separate stores (`sstv-frames`, `easypal-frames`).

## Data Flow Summary

| Path | Protocol | Content |
|---|---|---|
| RTL-SDR → rtl_tcp | USB | Raw IQ uint8 |
| rtl_tcp → multiplexer | TCP internal | Raw IQ uint8 |
| multiplexer → browser | WebSocket :1236 | Raw IQ uint8 |
| AudioMux → cw-decoder | TCP :1237 | complex64@24kHz (CW freq at DC) |
| AudioMux → sstv-decoder | TCP :1238 | complex64@24kHz (SSTV freq at DC) |
| AudioMux → easypal-decoder | TCP :1239 | complex64@24kHz (EasyPal freq at DC) |
| AudioMux → psk31-decoder | TCP :1240 | complex64@24kHz (PSK31 freq at DC) |
| cw-decoder → browser | WebSocket :8765 | JSON char/word_space/status events |
| psk31-decoder → browser | WebSocket :8768 | JSON char/word_space events |
| sstv-decoder → browser | WebSocket :8766 | JSON frame events (base64 PNG) |
| easypal-decoder → browser | WebSocket :8767 | JSON frame events (base64 PNG) |

## CI/CD

Pushes to `main` trigger a GitHub Actions pipeline:

1. **test** — `pytest services/ -v` on GitHub-hosted Ubuntu runner
2. **ui-test** — TypeScript check + Vitest unit tests + Vite production build
3. **publish** — builds and pushes six Docker images to GHCR (parallel with GHA cache)
4. **deploy** — job runs on the **self-hosted runner** (persistent agent on the homelab server); does `docker compose pull && docker compose up -d --remove-orphans`

The self-hosted runner connects outbound to GitHub's API and polls for jobs — no inbound ports required.
