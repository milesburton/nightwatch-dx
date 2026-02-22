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

# rtl_tcp runs on 1234 (internal), TCP mux on 1235, WebSocket IQ on 1236
# Audio mux ports: 1237 (CW), 1238 (SSTV), 1239 (EasyPal)
EXPOSE 1235 1236 1237 1238 1239

# Start rtl_tcp then the Python multiplexer
# -f 139175000 = 14.175 MHz centre of 20m band + 125 MHz LO
# -s 2400000   = 2.4 Msps — covers 13.975–14.375 MHz (whole 20m CW+SSTV segment)
# -g 197       = 19.7 dB — R820T gain in tenths of dB.
#                R820T steps: 0,9,14,27,37,77,87,125,144,157,166,197,207,229,
#                254,280,297,328,338,364,372,386,402,421 (tenths dB).
# rtl_biast -b 1 must run first to power the NooElec HF upconverter via bias-T,
# then release the device so rtl_tcp can claim it.
CMD rtl_biast -b 1 && \
    rtl_tcp -a 0.0.0.0 -p 1234 -f 139175000 -s 2400000 -g 197 -T & \
    sleep 2 && \
    python3 -u rtl_bridge.py
