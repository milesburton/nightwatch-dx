FROM python:3.11-slim
WORKDIR /app
COPY services/psk31-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/store.py .
COPY services/psk31-decoder/ .
CMD ["python", "-u", "psk31_decoder.py"]
