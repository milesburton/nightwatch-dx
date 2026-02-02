FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Copy multiplexer code
COPY services/multiplexer/iq_multiplexer.py /app/

# Expose multiplexer port
EXPOSE 1235

# Run multiplexer
CMD ["python3", "/app/iq_multiplexer.py", "${RTL_TCP_HOST:-rtl-tcp}", "${RTL_TCP_PORT:-1234}", "${MUX_PORT:-1235}"]
