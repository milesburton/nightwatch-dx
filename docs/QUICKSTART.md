# Quick Start Guide

## Prerequisites

- RTL-SDR Blog V3 dongle
- NooElec Ham It Up v1.3 upconverter
- Docker and Docker Compose installed
- USB port with adequate power (400mA+)

## Installation

### 1. Clone Repository

```bash
git clone https://github.com/milesburton/gmktec-sdr-project.git
cd gmktec-sdr-project
```

### 2. Build and Start Services

```bash
# Build all Docker images
docker-compose build

# Start the stack
docker-compose up -d

# View logs
docker-compose logs -f scanner
```

## Basic Usage

### View Scanner Activity

```bash
# Live scanner logs
docker-compose logs -f scanner

# Last 100 lines
docker-compose logs --tail=100 scanner
```

### Run CW Decoder

```bash
# Start decoder on 14.030 MHz at 20 WPM
docker-compose run --rm decoder python3 /app/cw_decoder.py 14.030 20

# Or connect to running container
docker-compose up -d decoder
docker-compose exec decoder python3 /app/cw_decoder.py 14.000 20
```

### Check System Status

```bash
# All services
docker-compose ps

# Individual service health
docker-compose logs multiplexer | grep "Stats:"
```

## Stopping Services

```bash
# Stop all
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

## Troubleshooting

### No Signal Detected

1. Check USB connection: `lsusb | grep Realtek`
2. Verify rtl-tcp running: `docker-compose logs rtl-tcp`
3. Check multiplexer connection: `docker-compose logs multiplexer`

### Permission Denied (USB)

```bash
# Add udev rule (Linux)
sudo bash -c 'echo "SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0bda\", ATTRS{idProduct}==\"2838\", MODE=\"0666\"" > /etc/udev/rules.d/99-rtlsdr.rules'
sudo udevadm control --reload-rules
```

### High CPU Usage

- Reduce sample rate in docker-compose.yml
- Lower gain setting
- Increase scan dwell time

## Next Steps

- Read [ARCHITECTURE.md](ARCHITECTURE.md) for system design details
- Explore configuration in `config/`
- Check signal lock logs in `data/`
- Add custom decoders (see development guide)

---

For detailed documentation, see [README.md](../README.md)
