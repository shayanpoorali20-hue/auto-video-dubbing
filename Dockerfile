FROM python:3.10-slim

# نصب ffmpeg و nodejs برای حل چالش‌های جاوااسکریپت یوتیوب
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# کپی کردن فایل نیازمندی‌ها
COPY requirements.txt .

# نصب پکیج‌های پایتون و مطمئن شدن از آخرین آپدیت yt-dlp
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --upgrade --no-cache-dir yt-dlp

# کپی کردن تمام فایل‌های پروژه (شامل main.py و cookies.txt)
COPY . .

EXPOSE 10000

# اجرای سرور FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
