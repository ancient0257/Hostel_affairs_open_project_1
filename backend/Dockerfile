FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command overridden per-service in docker-compose
CMD ["python", "sensor_publisher.py"]
