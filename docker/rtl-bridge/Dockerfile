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
# Audio mux ports: 1237 (CW), 1238 (SSTV), 1239 (EasyPal), 1240 (PSK31)
EXPOSE 1235 1236 1237 1238 1239 1240

# Start rtl_tcp then the Python multiplexer
# -f 139131000 = 14.131 MHz RF centre + 125 MHz LO offset
#                Covers 13.531–14.731 MHz at 1.2 Msps (±600 kHz):
#                CW 14.029, PSK31 14.070, SSTV 14.230, EasyPal 14.233 all within band.
# -s 1200000   = 1.2 Msps — stable RTL-SDR rate, half the data vs 2.4 Msps.
#                RTL-SDRs are modified TV tuners; 2.4 Msps is near their USB limit
#                and causes sample drops. 1.2 Msps is rock-solid on all hardware.
# -g 77        = 7.7 dB — R820T gain in tenths of dB.
#                R820T steps: 0,9,14,27,37,77,87,125,144,157,166,197,207,229,
#                254,280,297,328,338,364,372,386,402,421 (tenths dB).
#                Was 144 (14.4 dB) — reduced further because narrower 1.2 Msps
#                bandwidth concentrates noise, causing the display to saturate.
# rtl_biast -b 1 must run first to power the NooElec HF upconverter via bias-T,
# then release the device so rtl_tcp can claim it.
CMD rtl_biast -b 1 && \
    rtl_tcp -a 0.0.0.0 -p 1234 -f 139131000 -s 1200000 -g 77 -T & \
    sleep 2 && \
    python3 -u rtl_bridge.py
