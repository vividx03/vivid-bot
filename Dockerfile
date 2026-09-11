FROM python:3.10-slim

# System dependencies aur FFmpeg install karein
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies install karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baaki code copy karein
COPY . .

# Bot start command
CMD ["python", "bot.py"]
