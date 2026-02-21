FROM python:3.11-slim
WORKDIR /app
COPY services/cw-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/cw-decoder/ .
CMD ["python", "-u", "cw_decoder.py"]
