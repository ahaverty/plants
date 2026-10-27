FROM python:3.12-slim
WORKDIR /app
# DejaVu fonts for the notification picture's labels (plants/notify_image.py)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY plants ./plants
COPY tests ./tests
WORKDIR /app/plants
ENV PLANTS_DATA=/data PLANTS_BIND=0.0.0.0 PLANTS_PORT=8772 PYTHONUNBUFFERED=1
EXPOSE 8772
CMD ["python", "server.py"]
