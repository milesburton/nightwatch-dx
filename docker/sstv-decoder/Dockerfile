FROM python:3.11-slim

WORKDIR /app
COPY services/sstv-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/sstv-decoder/sstv_decoder.py .

EXPOSE 8766

CMD ["python3", "-u", "sstv_decoder.py"]
