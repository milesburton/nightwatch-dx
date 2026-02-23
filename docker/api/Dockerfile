FROM python:3.11-slim
WORKDIR /app
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/store.py .
COPY services/api/api.py .
CMD ["python", "-u", "api.py"]
