# Configuration

All runtime configuration is passed via environment variables in `docker-compose.yml`.

## rtl-bridge

| Variable | Default | Description |
|---|---|---|
| `RTL_TCP_HOST` | `127.0.0.1` | Host where `rtl_tcp` is listening |
| `RTL_TCP_PORT` | `1234` | Port of the upstream `rtl_tcp` process |
| `MUX_PORT` | `1235` | TCP multiplexer port (rtl_tcp-compatible) |
| `WS_PORT` | `1236` | WebSocket IQ stream port (browser) |

**rtl_tcp flags** (set in `docker/rtl-bridge/Dockerfile`):

| Flag | Value | Meaning |
|---|---|---|
| `-f` | `139175000` | Centre frequency: 139.175 MHz (→ 14.175 MHz after upconverter) |
| `-s` | `2400000` | Sample rate: 2.4 Msps |
| `-g` | `197` | Gain: 19.7 dB (R820T2 gain step in tenths of dB) |
| `-T` | — | Enable bias-T (powers upconverter) |

## cw-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the TCP multiplexer |
| `MUX_PORT` | `1235` | TCP multiplexer port |
| `WS_PORT` | `8765` | WebSocket port for decoded character events |

## sstv-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the TCP multiplexer |
| `MUX_PORT` | `1235` | TCP multiplexer port |
| `WS_PORT` | `8766` | WebSocket port for decoded frame events |

## Changing the frequency

To tune to a different part of the spectrum, update the `-f` flag in `docker/rtl-bridge/Dockerfile`. Account for the 125 MHz upconverter offset:

```
rtl_tcp_freq = target_RF_freq + 125_000_000
```

For example, to tune to 40m (7.1 MHz centre): `-f 132100000`

Also update the band markers in `ui/src/components/WaterfallPanel.tsx` (`BAND_MARKERS` array) and the `RF_CENTER_HZ` / `CW_FREQ_HZ` constants in the decoder services.
