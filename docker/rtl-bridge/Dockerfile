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
EXPOSE 1235
EXPOSE 1236

# Start rtl_tcp then the Python multiplexer
# -f 139175000 = 14.175 MHz centre of 20m band + 125 MHz LO
# -s 2400000   = 2.4 Msps — covers 13.975–14.375 MHz (whole 20m CW+SSTV segment)
# -g 20        = 20.7 dB — reduced from 42 dB which saturated the ADC on 20m.
#                R820T gain steps: 0,0.9,1.4,2.7,3.7,7.7,8.7,12.5,14.4,15.7,
#                16.6,19.7,20.7,22.9,25.4,28.0,29.7,32.8,33.8,36.4,37.2,38.6,
#                40.2,42.1 dB.  Nearest step to 20 is 20.7 dB.
# rtl_biast -b 1 must run first to power the NooElec HF upconverter via bias-T,
# then release the device so rtl_tcp can claim it.
CMD rtl_biast -b 1 && \
    rtl_tcp -a 0.0.0.0 -p 1234 -f 139175000 -s 2400000 -g 20 -T & \
    sleep 2 && \
    python3 -u rtl_bridge.py
