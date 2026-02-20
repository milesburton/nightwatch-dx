FROM python:3.11-slim

WORKDIR /app
COPY services/cw-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/cw-decoder/cw_decoder.py .

EXPOSE 8765

CMD ["python3", "-u", "cw_decoder.py"]
