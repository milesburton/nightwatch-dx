FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rtl-sdr \
    python3 \
    python3-pip \
    python3-numpy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/rtl-bridge/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY services/rtl-bridge/rtl_bridge.py .

# rtl_tcp runs on 1234 (internal), multiplexer exposes 1235
EXPOSE 1235

# Start rtl_tcp then the Python multiplexer
# -f 139175000 = 14.175 MHz centre of 20m band + 125 MHz LO
# -s 2400000   = 2.4 Msps — covers 13.975–14.375 MHz (whole 20m CW+SSTV segment)
# -g 42        = 42 dB (up from 25 — HF upconverter path needs more gain)
CMD rtl_tcp -a 0.0.0.0 -p 1234 -f 139175000 -s 2400000 -g 42 -T & \
    sleep 2 && \
    python3 -u rtl_bridge.py
