# Configuration

All runtime configuration is passed via environment variables in `docker-compose.yml`.

## rtl-bridge

| Variable | Default | Description |
|---|---|---|
| `RTL_TCP_HOST` | `127.0.0.1` | Host where `rtl_tcp` is listening |
| `RTL_TCP_PORT` | `1234` | Port of the upstream `rtl_tcp` process |
| `MUX_PORT` | `1235` | TCP multiplexer port (rtl_tcp-compatible, used by browser IQ stream) |
| `WS_PORT` | `1236` | WebSocket IQ stream port (browser waterfall) |
| `AUDIO_CW_PORT` | `1237` | AudioMux port for CW decoder |
| `AUDIO_SSTV_PORT` | `1238` | AudioMux port for SSTV decoder |
| `AUDIO_EP_PORT` | `1239` | AudioMux port for EasyPal decoder |
| `AUDIO_PSK31_PORT` | `1240` | AudioMux port for PSK31 decoder |

**rtl_tcp flags** (set in `docker/rtl-bridge/Dockerfile`):

| Flag | Value | Meaning |
|---|---|---|
| `-f` | `139175000` | Centre frequency: 139.175 MHz (→ 14.175 MHz after upconverter) |
| `-s` | `2400000` | Sample rate: 2.4 Msps |
| `-g` | `197` | Gain: 19.7 dB (R820T2 gain step in tenths of dB) |
| `-T` | — | Enable bias-T (powers upconverter) |

**Target frequencies** (constants in `services/rtl-bridge/rtl_bridge.py`):

| Constant | Value | Offset from RF centre |
|---|---|---|
| `RF_CENTER_HZ` | 14,175,000 Hz | — |
| `CW_FREQ_HZ` | 14,029,000 Hz | −146 kHz |
| `SSTV_FREQ_HZ` | 14,230,000 Hz | +55 kHz |
| `EP_FREQ_HZ` | 14,233,000 Hz | +58 kHz |
| `PSK31_FREQ_HZ` | 14,070,000 Hz | −105 kHz |

## cw-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the AudioMux |
| `MUX_PORT` | `1237` | AudioMux port (receives complex64@24kHz) |
| `WS_PORT` | `8765` | WebSocket port for decoded character events |

## sstv-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the AudioMux |
| `MUX_PORT` | `1238` | AudioMux port (receives complex64@24kHz) |
| `WS_PORT` | `8766` | WebSocket port for decoded frame events |

## easypal-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the AudioMux |
| `MUX_PORT` | `1239` | AudioMux port (receives complex64@24kHz) |
| `WS_PORT` | `8767` | WebSocket port for decoded frame events |

## psk31-decoder

| Variable | Default | Description |
|---|---|---|
| `MUX_HOST` | `rtl-bridge` | Hostname of the AudioMux |
| `MUX_PORT` | `1240` | AudioMux port (receives complex64@24kHz) |
| `WS_PORT` | `8768` | WebSocket port for decoded character events |

## Changing the frequency

To tune to a different part of the spectrum, update the `-f` flag in `docker/rtl-bridge/Dockerfile`. Account for the 125 MHz upconverter offset:

```
rtl_tcp_freq = target_RF_freq + 125_000_000
```

For example, to tune to 40m (7.1 MHz centre): `-f 132100000`

Also update:
- `RF_CENTER_HZ` and the per-decoder `*_FREQ_HZ` constants in `services/rtl-bridge/rtl_bridge.py`
- Band markers in `ui/src/components/WaterfallPanel.tsx` (`BAND_MARKERS` array)

## AudioMux wire protocol

Decoder services connect to an AudioMux port and receive:

1. A 12-byte magic header: `b"AUD0\x00\x00\x00\x00\x00\x00\x00\x00"`
2. A continuous stream of raw `complex64` samples at 24,000 samples/second

The magic header lets decoders verify they are connected to an AudioMux rather than a raw rtl_tcp stream.
