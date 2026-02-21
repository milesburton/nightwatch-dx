FROM python:3.11-slim
WORKDIR /app
COPY services/sstv-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/sstv-decoder/ .
CMD ["python", "-u", "sstv_decoder.py"]
