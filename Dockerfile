FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg libfontconfig1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
