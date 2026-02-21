# 20m Signal Decoder

Live CW and SSTV decoding from an RTL-SDR dongle, displayed in a web UI.

## Architecture

```
RTL-SDR USB
    │
    ▼
rtl-bridge (Debian + rtl_tcp + Python multiplexer)
    │ port 1235 (rtl_tcp protocol)
    ├──────────────────────┐
    ▼                      ▼
cw-decoder            sstv-decoder
(Python + websockets) (Python + websockets)
port 8765 WS          port 8766 WS
    │                      │
    └──────────┬───────────┘
               ▼
             ui (nginx)
          port 8080 HTTP
     /ws/cw  → cw-decoder:8765
     /ws/sstv → sstv-decoder:8766
```

## Stack

- **Frontend**: React 19 + TypeScript + Vite + Tailwind CSS
- **SSTV decode engine**: Ported from [sstv-toolkit](https://github.com/milesburton/sstv-toolkit) (TypeScript, runs in browser Web Worker)
- **CW decode**: Python asyncio service with envelope detection + Butterworth LPF + Morse state machine
- **SSTV live decode**: Python asyncio service with FM demod + VIS detection + frame decode
- **Transport**: WebSocket (JSON for CW chars, base64 PNG for SSTV frames)
- **Infra**: Docker Compose

## Quick start

```bash
# On the GMKtec server (192.168.1.211)
cd ~/gmktec-sdr-project
docker compose up -d --build

# Open browser
open http://192.168.1.211:8080
```

## Configuration

All settings are environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `CW_FREQ_HZ` | 14000000 | CW decode frequency (Hz) |
| `SSTV_FREQ_HZ` | 14230000 | SSTV monitor frequency (Hz) |
| `LO_OFFSET_HZ` | 125000000 | Upconverter LO offset |
| `SAMPLE_RATE` | 240000 | SDR sample rate |
| `GAIN` | 250 | SDR gain (tenths of dB) |
| `WPM` | 20 | CW decode speed |

## Hardware

- RTL-SDR Blog V3 (RTL2838U + R820T2)
- NooElec HF upconverter (125 MHz LO)
- GMKtec G2 Mini PC
