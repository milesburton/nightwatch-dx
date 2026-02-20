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
CMD rtl_tcp -a 0.0.0.0 -p 1234 -f 139150000 -s 240000 -g 25 -T & \
    sleep 2 && \
    python3 -u rtl_bridge.py
