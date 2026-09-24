FROM python:3.11-slim

# نصب ابزارهای مورد نیاز سیستم‌عامل
RUN apt-get update && apt-get install -y \
    ffmpeg \
    rubberband-cli \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کل پروژه
COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]