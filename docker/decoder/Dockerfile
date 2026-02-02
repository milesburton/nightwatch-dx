FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install --no-cache-dir \
    numpy \
    scipy

# Copy decoder code
COPY services/decoder/cw_decoder.py /app/

# Create logs directory
RUN mkdir -p /app/logs

# Default command (can be overridden)
CMD ["python3", "/app/cw_decoder.py", "14.000", "20"]
