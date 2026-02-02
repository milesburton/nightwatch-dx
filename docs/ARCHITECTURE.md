# System Architecture

## Overview

The GMKtec SDR Project uses a multi-tier architecture that separates hardware I/O, sample distribution, and signal processing into distinct, loosely-coupled services.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Physical Layer                           │
│  RTL-SDR V3 + NooElec Ham It Up v1.3 Upconverter            │
│  (HF → 125 MHz upconversion, powered via bias-tee)          │
└────────────────────┬────────────────────────────────────────┘
                     │ USB
                     ↓
┌─────────────────────────────────────────────────────────────┐
│                   I/Q Streaming Layer                        │
│  rtl_tcp                                                     │
│  - Reads from RTL-SDR hardware                               │
│  - Streams raw I/Q samples over TCP (port 1234)              │
│  - Single client connection                                  │
└────────────────────┬────────────────────────────────────────┘
                     │ TCP (2.4 MSPS, ~4.8 MB/s)
                     ↓
┌─────────────────────────────────────────────────────────────┐
│                 Distribution Layer                           │
│  I/Q Multiplexer                                             │
│  - Connects to rtl_tcp as sole client                        │
│  - Rebroadcasts samples to multiple TCP clients (port 1235)  │
│  - Forwards client commands (freq/gain) to rtl_tcp           │
│  - Thread-safe client management                             │
└────────────────────┬────────────────────────────────────────┘
                     │ TCP (multiple connections)
       ┌─────────────┼─────────────┬─────────────┐
       ↓             ↓             ↓             ↓
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│   Band     │ │    CW      │ │   FT8      │ │  Future    │
│  Scanner   │ │  Decoder   │ │  Decoder   │ │  Decoders  │
│            │ │            │ │  (planned) │ │            │
└────────────┘ └────────────┘ └────────────┘ └────────────┘
     │              │              │              │
     ↓              ↓              ↓              ↓
  JSON Logs    Text Output   WSJT-X API     Custom Output
```

## Service Descriptions

### rtl_tcp (I/Q Streaming)
**Purpose**: Low-level hardware interface and I/Q sample streaming

**Responsibilities**:
- Initialize RTL-SDR hardware
- Enable bias-tee for upconverter power
- Stream continuous I/Q samples at 2.4 MSPS
- Accept single TCP client connection
- Handle frequency/gain commands

**Implementation**: C binary from rtl-sdr package or Docker container

**Configuration**:
- Center frequency: 139.150 MHz (14.150 MHz + 125 MHz LO)
- Sample rate: 2.4 MSPS
- Gain: 25 dB (manual)
- Port: 1234

### I/Q Multiplexer (Sample Distribution)
**Purpose**: Enable multiple clients to share single rtl_tcp stream

**Responsibilities**:
- Maintain persistent connection to rtl_tcp
- Accept multiple client connections simultaneously
- Broadcast each I/Q sample to all connected clients
- Forward client commands to rtl_tcp
- Handle client connect/disconnect events
- Track statistics (throughput, client count)

**Implementation**: Python 3.11, threading

**Key Classes**:
- `IQMultiplexer`: Main server class
- Threading: accept_clients, broadcast_samples, print_stats

**Performance**:
- Minimal latency (< 1ms added delay)
- Supports 10+ concurrent clients
- ~5-10 MB memory footprint

### Band Scanner (Signal Detection)
**Purpose**: Continuous monitoring of 20M band frequencies

**Responsibilities**:
- Scan 9 frequencies sequentially
- Perform FFT-based power detection
- Lock onto strong signals (> -6 dB) for extended monitoring
- Log signal characteristics and lock sessions
- Calculate signal-to-noise ratio

**Implementation**: Python 3.11, numpy, scipy

**Key Classes**:
- `RTLTCPScanner`: Connect to multiplexer and perform FFT
- `BandScanner`: Orchestrate scanning loop and signal locking

**Algorithm**:
1. Connect to multiplexer (port 1235)
2. For each frequency:
   - Tune to upconverted frequency (HF + 125 MHz)
   - Read 0.5s of I/Q samples
   - Perform 2048-point FFT with Hanning window
   - Calculate max/avg power in target span
3. If signal > LOCK_THRESHOLD:
   - Lock on frequency
   - Monitor every 2 seconds
   - Log to JSON file
4. Release lock after 30-300 seconds or signal drops

**Thresholds**:
- Detection: -6 dB (3 dB above -9 dB noise floor)
- Lock: -6 dB
- Strong: -4 dB

### CW Decoder (Morse Code)
**Purpose**: Decode CW (Morse code) transmissions

**Responsibilities**:
- Detect CW envelope using magnitude calculation
- Identify dots and dashes based on timing
- Decode Morse characters
- Output decoded text in real-time

**Implementation**: Python 3.11, numpy, scipy

**Key Classes**:
- `CWDecoder`: Main decoder class
- Morse dictionary: Full alphanumeric + punctuation

**Algorithm**:
1. Connect to multiplexer
2. Read continuous I/Q samples
3. Calculate envelope (magnitude of complex samples)
4. Lowpass filter envelope (Butterworth, fc=0.1)
5. Threshold detection (tone on/off)
6. Measure tone/space durations
7. Classify as dit (.), dah (-), or space
8. Decode characters using Morse dictionary

**Parameters**:
- WPM: 20 (configurable)
- Dit duration: 60ms @ 20 WPM
- Timing tolerance: 50%

## Data Flow

### Normal Scanning Mode

```
RTL-SDR Hardware
    → rtl_tcp (2.4 MSPS I/Q stream)
    → Multiplexer (broadcast to all clients)
    → Scanner
        → FFT (2048 points, Hanning window)
        → Power calculation (max/avg over 10 kHz span)
        → Threshold check
        → [If strong] Signal lock + JSON logging
```

### Simultaneous Scanning + Decoding

```
RTL-SDR Hardware
    → rtl_tcp (2.4 MSPS I/Q stream)
    → Multiplexer
        ├→ Scanner (scanning 14.000-14.300 MHz)
        └→ CW Decoder (locked on 14.030 MHz)
            → Envelope detection
            → Morse decoding
            → Text output
```

## Communication Protocols

### rtl_tcp Protocol

Binary protocol over TCP. Commands are 5 bytes:

```
[CMD_TYPE: 1 byte][VALUE: 4 bytes big-endian uint32]
```

Commands:
- `0x01`: Set frequency (Hz)
- `0x02`: Set sample rate (Hz)
- `0x03`: Set gain mode (0=auto, 1=manual)
- `0x04`: Set gain (tenths of dB, e.g., 250 = 25.0 dB)

I/Q samples: Continuous stream of unsigned 8-bit I/Q pairs

### Multiplexer Protocol

Same as rtl_tcp protocol. Clients send commands, multiplexer forwards to rtl_tcp and broadcasts samples.

## Deployment

### Docker Compose Orchestration

```yaml
services:
  rtl-tcp → multiplexer → scanner
                      → decoder (manual)
```

Dependencies ensure proper startup order:
1. rtl-tcp (waits for hardware)
2. multiplexer (waits for rtl-tcp)
3. scanner/decoder (wait for multiplexer)

### Resource Allocation

| Service | CPU | Memory | Network I/O |
|---------|-----|--------|-------------|
| rtl-tcp | 5% | 10 MB | 4.8 MB/s TX |
| multiplexer | 10% | 15 MB | 4.8 MB/s RX, N×4.8 MB/s TX |
| scanner | 15% | 20 MB | 4.8 MB/s RX |
| decoder | 10% | 15 MB | 4.8 MB/s RX |

## Scalability

### Current Limitations
- rtl_tcp: Single hardware, ~1 GHz tuning range (HF with upconverter)
- Multiplexer: Network bandwidth (4.8 MB/s per client)
- Scanner: Sequential scanning (not true parallel)

### Future Improvements
- Multiple RTL-SDR devices for parallel band coverage
- GPU-accelerated FFT for faster scanning
- Distributed processing across multiple hosts
- Additional decoders (FT8, PSK31, RTTY)

## Security Considerations

- All services run on localhost (127.0.0.1)
- No authentication (local-only deployment)
- Privileged Docker required for USB access
- Consider: VPN/SSH tunnel for remote access

## Monitoring & Observability

- Systemd journal logs (native installation)
- Docker logs (containerized deployment)
- JSON log files for signal lock sessions
- Multiplexer statistics (10-second intervals)

---

Last updated: 2026-02-02
