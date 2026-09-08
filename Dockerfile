FROM python:3.11-slim

# FFmpeg ve gerekli sistem araçlarını kur
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render ve diger bulut servisleri icin dinamik PORT destegi
ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn backend:app --host 0.0.0.0 --port ${PORT:-10000}"]
