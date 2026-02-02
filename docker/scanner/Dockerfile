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

# Copy scanner code
COPY services/scanner/scanner.py /app/

# Create data directories
RUN mkdir -p /app/logs /app/data

# Run scanner
CMD ["python3", "/app/scanner.py", "5"]
