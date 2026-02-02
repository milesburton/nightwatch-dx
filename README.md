# GMKtec SDR Project

**Professional HF Software-Defined Radio Monitoring System**

A complete SDR solution for monitoring the 20M amateur radio band with simultaneous scanning and decoding capabilities.

## Overview

This project provides a multi-tier SDR architecture that enables:
- **Continuous band scanning** across 9 frequencies on the 20M band (14 MHz)
- **Intelligent signal locking** on strong signals for extended monitoring
- **Simultaneous operation** of multiple decoders while scanning
- **CW (Morse code) decoding** with configurable WPM
- **Real-time signal analysis** with FFT-based power detection

## Architecture

```
RTL-SDR Hardware (RTL2838U + R820T)
         ↓
    Bias-Tee (5V DC power)
         ↓
NooElec Ham It Up v1.3 Upconverter (125 MHz LO)
         ↓
    rtl_tcp (I/Q streaming server)
         ↓
    I/Q Multiplexer (multi-client broadcaster)
         ↓
    ┌────────┴────────┬──────────────┐
    ↓                 ↓              ↓
Band Scanner    CW Decoder    Future Decoders
```

## Hardware

- **SDR**: RTL-SDR Blog V3 (RTL2838U + R820T tuner)
- **Upconverter**: NooElec Ham It Up v1.3 (125 MHz LO)
- **Bias-Tee**: Enabled for upconverter power
- **Power Draw**: ~400mA total (280mA SDR + 120mA upconverter)

## Monitored Frequencies

| Frequency | Mode | Bandwidth | Description |
|-----------|------|-----------|-------------|
| 14.000 MHz | CW | 5 kHz | CW calling frequency |
| 14.030 MHz | CW | 5 kHz | CW QRP frequency |
| 14.070 MHz | FT8 | 10 kHz | FT8 digital mode |
| 14.095 MHz | PSK31 | 5 kHz | PSK31 digital mode |
| 14.130 MHz | CW/SSB | 5 kHz | Mixed mode |
| 14.152 MHz | SSB | 5 kHz | Voice |
| 14.200 MHz | SSB | 5 kHz | Voice |
| 14.250 MHz | SSB | 5 kHz | Voice |
| 14.300 MHz | SSB | 5 kHz | Upper band edge |

## Features

### Band Scanner
- **FFT-based power detection** using numpy/scipy
- **Adaptive thresholding** calibrated for high-EMI environments
- **Signal locking**: Automatically locks onto signals > -6 dB for 30-300 seconds
- **Real-time monitoring**: 2-second sample intervals during lock
- **JSON logging**: Saves lock sessions with power statistics

### I/Q Multiplexer
- **Multi-client support**: Unlimited simultaneous connections
- **Command forwarding**: Clients can set frequency, gain, sample rate
- **Statistics tracking**: Monitors throughput and client count
- **Automatic cleanup**: Handles client disconnections gracefully

### CW Decoder
- **Envelope detection** with lowpass filtering
- **Configurable WPM** (default: 20 WPM)
- **Morse code dictionary**: Full alphanumeric + punctuation
- **Real-time decoding**: Character-by-character output
- **Timing tolerance**: 50% tolerance for varied sending speeds

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Start all services
docker-compose up -d

# View scanner logs
docker-compose logs -f scanner

# View multiplexer stats
docker-compose logs multiplexer

# Stop all services
docker-compose down
```

### Manual Installation

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for detailed setup instructions.

## Usage

### Monitoring the Scanner

```bash
# Live scanner output
docker-compose logs -f scanner

# Or via systemd (if installed natively)
sudo journalctl -u sdr-band-scanner -f
```

### Running the CW Decoder

```bash
# Connect to a running container
docker-compose exec decoder python3 /app/cw_decoder.py 14.000 20

# Or run standalone
python3 services/decoder/cw_decoder.py 14.000 20
```

### Viewing Multiplexer Statistics

```bash
docker-compose logs multiplexer
```

## Performance

### Current Performance Metrics
- **Baseline noise floor**: -9 dB (server rack environment)
- **Detection threshold**: -6 dB (3 dB above noise)
- **Signal lock threshold**: -6 dB
- **Strong signal threshold**: -4 dB
- **Typical signal strength**: +8 to +20 dB above noise

### Resource Usage
- **CPU**: ~5-10% per service (on GMKtec G2)
- **Memory**: ~15-20 MB per service
- **Network**: ~4.8 MB/s I/Q data @ 2.4 MSPS

## Project Structure

```
gmktec-sdr-project/
├── docker/
│   ├── rtl-tcp/          # RTL-SDR I/Q streaming server
│   ├── multiplexer/      # I/Q sample multiplexer
│   ├── scanner/          # Band scanner service
│   └── decoder/          # CW decoder service
├── services/
│   ├── scanner/          # Scanner source code
│   ├── decoder/          # Decoder source code
│   └── multiplexer/      # Multiplexer source code
├── docs/
│   ├── ARCHITECTURE.md   # System architecture details
│   ├── INSTALLATION.md   # Installation guide
│   └── TROUBLESHOOTING.md # Common issues and solutions
├── config/
│   └── frequencies.json  # Monitored frequency list
├── scripts/
│   └── helpers.sh        # Utility scripts
├── docker-compose.yml    # Docker Compose configuration
└── README.md             # This file
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | 127.0.0.1 | rtl_tcp server address |
| `RTL_TCP_PORT` | 1234 | rtl_tcp server port |
| `MUX_PORT` | 1235 | Multiplexer listen port |
| `SCANNER_GAIN` | 25 | SDR gain in dB |
| `LOCK_THRESHOLD` | -6 | Signal lock threshold in dB |

See [config/](config/) for detailed configuration options.

## Development

### Running Tests

```bash
# Run scanner tests
pytest services/scanner/tests/

# Run decoder tests
pytest services/decoder/tests/
```

### Adding New Decoders

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for guidelines on adding new decoders.

## Troubleshooting

### Scanner shows -100.0 dB readings
- Check rtl_tcp service is running
- Verify multiplexer is connected to rtl_tcp
- Check USB power budget (should be < 500mA per port)

### High baseline noise (-9 dB or worse)
- This is normal in high-EMI environments (server racks)
- Consider: FM band stop filter, ferrite chokes, lower gain
- See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#emi-mitigation)

### Decoder not connecting
- Ensure multiplexer service is running
- Check port 1235 is accessible
- Verify rtl_tcp is streaming data

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- Built with [rtl-sdr](https://github.com/osmocom/rtl-sdr) tools
- Uses [numpy](https://numpy.org/) and [scipy](https://scipy.org/) for signal processing
- Inspired by various amateur radio SDR projects

## Hardware Links

- [RTL-SDR Blog V3](https://www.rtl-sdr.com/buy-rtl-sdr-dvb-t-dongles/)
- [NooElec Ham It Up v1.3](https://www.nooelec.com/store/ham-it-up.html)
- [GMKtec G2 Mini PC](https://www.gmktec.com/)

## Support

For issues, questions, or contributions, please open an issue on GitHub.

---

**Built with 🛰️ by Claude Code** | Last updated: 2026-02-02
