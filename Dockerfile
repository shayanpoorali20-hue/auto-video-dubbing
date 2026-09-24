FROM python:3.11-slim

# نصب FFmpeg، Rubberband و Node.js (برای حل چالش جاوااسکریپت yt-dlp)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    rubberband-cli \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
