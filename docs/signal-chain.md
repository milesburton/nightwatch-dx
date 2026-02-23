# Signal Chain

## Hardware path

```
Antenna
  │
  ▼
NooElec Ham It Up Plus (HF upconverter)
  Powers via RTL-SDR bias-T (rtl_biast -b 1)
  Mixes HF signal up by 125 MHz
  Input:  0–30 MHz HF
  Output: 125–155 MHz (in RTL-SDR range)
  │
  ▼
RTL-SDR (R820T2 tuner, RTL2832U demodulator)
  Tuned to 139.175 MHz → sees 14.175 MHz (20m centre)
  Gain: 19.7 dB (-g 197 in rtl_tcp tenths-of-dB units)
  Sample rate: 2.4 Msps
  Output: uint8 I/Q interleaved (offset binary, 127.5 = 0)
```

**Frequency mapping:**
- RF centre: SDR centre (139.175 MHz) − LO offset (125 MHz) = **14.175 MHz**
- CW sub-band:    14.029 MHz → offset **−146 kHz** from RF centre
- PSK31 sub-band: 14.070 MHz → offset **−105 kHz** from RF centre
- SSTV frequency: 14.230 MHz → offset  **+55 kHz** from RF centre
- EasyPal:        14.233 MHz → offset  **+58 kHz** from RF centre
- Coverage: 12.975 – 15.375 MHz (14.175 ± 1.2 MHz)

## Common front-end: AudioDecimator (rtl-bridge)

All four decoders receive pre-decimated audio from rtl-bridge rather than raw IQ. rtl-bridge runs one `AudioDecimator` instance per decoder frequency:

```
uint8 IQ @ 2.4 Msps
  │
  ├─ [mix] multiply by exp(-j·2π·offset/2.4M·n) to shift target freq to DC
  │
  ├─ [Chebyshev IIR stage 1] order 8, 0.05 dB passband ripple
  │         cutoff = 0.1 × Nyquist (120 kHz), decimate ×10
  │         2.4 Msps → 240 kHz  (IIR state persists across chunks)
  │
  └─ [Chebyshev IIR stage 2] same filter
            240 kHz → 24 kHz  complex64 output
            → TCP AudioMux port (:1237 / :1238 / :1239 / :1240)
```

Each `AudioDecimator` runs in its own thread (inside `AudioMux.decimation_worker`). This replaces the per-decoder FIR decimation that previously consumed ~100% CPU.

## CW decoder signal chain

Input: `complex64@24kHz` from AudioMux `:1237` (14.029 MHz mixed to DC)

```
complex64@24kHz
  │
  ├─ [Kaiser FIR bandpass] ±150 Hz around DC
  │         Rejects adjacent SSB and other CW signals
  │
  ├─ [magnitude] |I + jQ| → real envelope signal
  │
  ├─ [asymmetric IIR envelope smoother]
  │         attack τ = 0.5 ms  (α ≈ 0.248) — tracks tone onset
  │         decay  τ = 0.2 ms  (α ≈ 0.488) — faster so gaps register cleanly
  │
  ├─ [adaptive threshold]
  │         3-second rolling window of envelope samples
  │         SNR gate: if p90/p5 < 1.8× → threshold set unreachably high (noise only)
  │         threshold = p90 of window
  │
  ├─ [Schmitt trigger] hysteresis ±10%
  │         high_thr = threshold × 1.10  → mark (tone on)
  │         low_thr  = threshold × 0.90  → space (tone off)
  │
  ├─ [run-length encoding] consecutive mark/space samples → (state, duration) pairs
  │
  └─ [MorseDecoder]
         Adaptive dit estimation (EWMA on observed tone durations)
         Thresholds:
           tone < 0.4× dit   → noise, ignore
           0.4–2.0× dit      → dit
           > 2.0× dit        → dah
           gap 1× dit        → intra-element (no event)
           gap 3× dit        → character boundary → flush symbol buffer
           gap 7× dit        → word boundary → flush + emit word_space
         Unrecognised sequences → [.-.--] bracket notation
         → JSON {"type":"char","char":"A","freq":14029000,"ts":"…"} over WebSocket
```

## PSK31 decoder signal chain

Input: `complex64@24kHz` from AudioMux `:1240` (14.070 MHz mixed to DC)

```
complex64@24kHz
  │
  ├─ [FFT carrier scan]  4096-point FFT, search ±2000 Hz (±341 bins)
  │         Finds peak bin; first call sets offset directly; subsequent calls
  │         apply α=0.5 smooth update to track drift
  │         Re-runs every 5 seconds
  │         SNR = peak_power / mean_power; if < 3× → suppress output
  │
  ├─ [fine mix to DC]  multiply by exp(-j·2π·carrier_offset/24000·n)
  │
  ├─ [Kaiser FIR matched filter]  45 Hz lowpass (passes ±15.6 Hz PSK31 spectrum)
  │
  ├─ [symbol clock]  sample every 768 samples (24000 / 31.25 baud)
  │         Initial timing alignment: peak of |z|² over first symbol period
  │
  ├─ [differential BPSK decode]
  │         Δφ[n] = angle(z[n] · conj(z[n-1]))
  │         |Δφ| > π/2 → bit 0 (phase transition)
  │         |Δφ| ≤ π/2 → bit 1 (no transition)
  │
  └─ [varicode decode]
         Accumulate bits; two consecutive 0-bits = character boundary
         Look up G3PLX varicode table → ASCII character
         12-bit overflow guard (reset on runaway)
         → JSON {"type":"char","char":"A","freq":14070000,"ts":"…"} over WebSocket
```

## SSTV decoder signal chain

Input: `complex64@24kHz` from AudioMux `:1238` (14.230 MHz mixed to DC)

```
complex64@24kHz
  │
  ├─ [FM discriminator]
  │         cross = Q[n]·I[n-1] − I[n]·Q[n-1]
  │         dot   = I[n]·I[n-1] + Q[n]·Q[n-1]
  │         freq  = arctan2(cross, dot) × AUDIO_RATE / 2π   (Hz)
  │
  ├─ [VIS detector state machine]
  │         Leader:  1900 Hz ± 50 Hz for 300 ms
  │         Break:   1200 Hz for 10 ms
  │         Start:   1900 Hz for 300 ms
  │         VIS bits: 30 ms each, 1100/1300 Hz = 1/0
  │         Stop:    1200 Hz for 30 ms
  │         → VIS code → mode identification
  │
  └─ [pixel decode]
         Robot 36: Y/C1/C2 lines, YUV→RGB conversion
         Generic fallback: frequency→greyscale for unknown modes
         → Pillow Image → PNG → base64 data URL
         → JSON {"type":"frame","imageUrl":"data:image/png;base64,…","mode":"Robot 36"} over WebSocket
```

## EasyPal decoder signal chain

Input: `complex64@24kHz` from AudioMux `:1239` (14.233 MHz mixed to DC)

```
complex64@24kHz
  │
  ├─ [FM discriminator]  same as SSTV → instantaneous frequency in Hz
  │
  ├─ [decimate 2×]  24 kHz → 12 kHz DRM internal rate
  │
  ├─ [phase integrate]
  │         phase[n] = phase[n-1] + 2π·freq[n] / 12000
  │         sig[n] = exp(j·phase[n])   (reconstructs complex carrier)
  │
  ├─ [mix −1500 Hz]  phase accumulator LO shifts DRM centre to DC
  │
  ├─ [DC block IIR]  leaky integrator α=0.9999
  │
  ├─ [OFDM sync]  guard-interval correlation (GI = 64, FFT = 256)
  │         Detects symbol boundaries; locks to DRM Mode B frame structure
  │
  ├─ [256-point FFT]  per symbol; extract carriers k = −10 … +18 (29 total)
  │
  ├─ [pilot channel estimation]  time pilots at k = −9,−3,+4,+8,+12
  │         Per-carrier complex equalisation (amplitude + phase correction)
  │
  ├─ [16-QAM hard decision]  nearest constellation point per carrier
  │
  ├─ [deinterleave]  frequency + time deinterleaver (DRM Mode B spec)
  │
  ├─ [Viterbi FEC]  rate-1/6 convolutional, punctured to rate-1/2
  │
  └─ [MSC reassembly]  CRC-16 segment validation → JPEG reconstruction
         → Pillow Image → PNG → base64 data URL
         → JSON {"type":"frame","imageUrl":"data:image/png;base64,…"} over WebSocket
```

## Waterfall / spectrum (browser)

```
Raw IQ uint8 (via WebSocket from rtl-bridge :1236)
  │
  ├─ [iqWorker.ts] Web Worker (dedicated thread)
  │
  ├─ [FFT] 2048-bin Hann-windowed FFT
  │         fftshift → frequency-ordered bins
  │         magnitude → power in dB
  │
  ├─ Spectrum canvas (instantaneous, 80 px tall)
  │         Colour: cyan line, dB grid lines, band marker labels
  │
  └─ Waterfall canvas (400 row history)
         Colour LUT: black → blue → cyan → yellow → white
         Dynamic range: −120 dBFS to −50 dBFS
         Band markers: 14.000 CW, 14.025 QRP, 14.070 PSK31,
                       14.100 Bcn, 14.175 Ctr, 14.230 SSTV
```

## RTL-SDR gain reference (R820T2, tenths of dB)

Valid `-g` values for `rtl_tcp`:

```
0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229,
254, 280, 297, 328, 338, 364, 372, 386, 402, 421
```

Current setting: **197** (19.7 dB). Increase if signals are weak; decrease if waterfall is saturated (all yellow/white).
