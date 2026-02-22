FROM python:3.11-slim
WORKDIR /app
COPY services/easypal-decoder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/easypal-decoder/ .
CMD ["python", "-u", "easypal_decoder.py"]
