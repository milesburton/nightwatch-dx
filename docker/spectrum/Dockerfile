FROM python:3.11-slim

WORKDIR /app
COPY services/spectrum/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/spectrum/spectrum_service.py .

EXPOSE 8767

CMD ["python3", "-u", "spectrum_service.py"]
