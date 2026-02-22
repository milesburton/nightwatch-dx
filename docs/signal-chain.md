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
- CW sub-band: 14.029 MHz → offset **−146 kHz** from RF centre
- SSTV frequency: 14.230 MHz → offset **+55 kHz** from RF centre
- Coverage: 13.975–14.375 MHz (full 20m CW + digital + SSTV segment)

## CW decoder signal chain

```
Raw IQ uint8 (2.4 Msps)
  │
  ├─ [mix] multiply by complex exponential at FREQ_OFFSET_HZ (−146 kHz)
  │         shifts CW tone to DC (0 Hz)
  │
  ├─ [FIR decimate ÷10] Kaiser lowpass, cutoff 1200 Hz
  │         2.4 Msps → 240 kHz
  │
  ├─ [FIR decimate ÷10] Kaiser lowpass, cutoff 1200 Hz
  │         240 kHz → 24 kHz audio rate
  │
  ├─ [magnitude] |I + jQ| → real envelope signal
  │
  ├─ [asymmetric IIR envelope smoother]
  │         attack τ = 0.5 ms (α ≈ 0.248)
  │         decay  τ = 0.2 ms (α ≈ 0.488)  — faster so gaps register cleanly
  │
  ├─ [adaptive threshold]
  │         3-second rolling window of envelope samples
  │         threshold = p90 of window
  │         SNR gate: if p90/p5 < 1.8× → set threshold unreachably high (noise only)
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
```

## Waterfall / spectrum (browser)

```
Raw IQ uint8 (via WebSocket from rtl-bridge)
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
         Band markers: 14.000 CW, 14.025 QRP, 14.070 FT8,
                       14.100 Bcn, 14.175 Ctr, 14.230 SSTV
```

## RTL-SDR gain reference (R820T2, tenths of dB)

Valid `-g` values for `rtl_tcp`:

```
0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229,
254, 280, 297, 328, 338, 364, 372, 386, 402, 421
```

Current setting: **197** (19.7 dB). Increase if signals are weak; decrease if waterfall is saturated (all yellow/white).
