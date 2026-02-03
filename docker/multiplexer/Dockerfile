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

# Use ENTRYPOINT with sh to properly expand environment variables
# -u flag for unbuffered output so logs appear immediately
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["python3 -u /app/iq_multiplexer.py ${RTL_TCP_HOST:-127.0.0.1} ${RTL_TCP_PORT:-1234} ${MUX_PORT:-1235}"]
