import os
import re
import json
import shutil
import time
import logging
import tempfile
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import requests

# تنظیمات Logging برای مشاهده دقیق جزئیات در Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI(title="Instagram Auto Subtitle Service")

# پوشه موقت کاری برای کل پروژه
TEMP_DIR = tempfile.mkdtemp()

# قفل اختصاصی برای جلوگیری از اجرای همزمان چند رندر سنگین
render_lock = threading.Lock()

# ==========================================
# 🔑 اطلاعات تلگرام
# ==========================================
TELEGRAM_BOT_TOKEN = "8956121858:AAF1ZQD-KCKSCbd-GOfGc2CziHpBFBONhxA"
TELEGRAM_CHAT_ID = "5080371184"


class SubtitleItem(BaseModel):
    line_index: int
    time_code: str
    start_seconds: float
    end_seconds: float
    original_text: str
    persian_text: str


class ProcessSubtitlesRequest(BaseModel):
    video_url: str
    subtitles: list[SubtitleItem]


# ---------------------------------------------------------------
# توابع کمکی ارتباط با تلگرام
# ---------------------------------------------------------------
def send_telegram_message(text: str) -> int:
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return res["result"]["message_id"]
    except Exception as e:
        logging.error(f"خطا در ارسال پیام به تلگرام: {e}")
    return None


def update_telegram_message(message_id: int, text: str):
    if not message_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.warning(f"خطا در آپدیت پیام تلگرام: {e}")


def send_telegram_video(video_path: str, caption: str):
    if not os.path.exists(video_path):
        send_telegram_message(f"❌ <b>خطا:</b> فایل ویدیو یافت نشد.")
        return

    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    send_telegram_message(f"📦 <b>شروع آپلود ویدیوی زیرنویس‌شده...</b>\nحجم فایل: {file_size_mb:.2f} مگابایت")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
    
    try:
        body = []
        body.extend([
            f'--{boundary}'.encode(),
            'Content-Disposition: form-data; name="chat_id"'.encode(),
            ''.encode(),
            str(TELEGRAM_CHAT_ID).encode()
        ])
        body.extend([
            f'--{boundary}'.encode(),
            'Content-Disposition: form-data; name="caption"'.encode(),
            ''.encode(),
            caption.encode('utf-8')
        ])
        body.extend([
            f'--{boundary}'.encode(),
            'Content-Disposition: form-data; name="parse_mode"'.encode(),
            ''.encode(),
            'HTML'.encode()
        ])
        
        with open(video_path, 'rb') as f:
            video_bytes = f.read()
        
        filename = os.path.basename(video_path)
        body.extend([
            f'--{boundary}'.encode(),
            f'Content-Disposition: form-data; name="video"; filename="{filename}"'.encode(),
            'Content-Type: video/mp4'.encode(),
            ''.encode(),
            video_bytes
        ])
        body.append(f'--{boundary}--'.encode())
        body.append(''.encode())
        
        payload = b'\r\n'.join(body)
        req = urllib.request.Request(url, data=payload, headers=headers)

        with urllib.request.urlopen(req, timeout=300) as response:
            res = json.loads(response.read().decode())
            if res.get("ok"):
                send_telegram_message("🚀 <b>ویدیو با زیرنویس با موفقیت ارسال شد!</b>")
            else:
                send_telegram_message(f"❌ <b>خطای تلگرام:</b> {res.get('description')}")
    except Exception as e:
        send_telegram_message(f"❌ <b>خطای آپلود:</b> <code>{str(e)}</code>")


# ---------------------------------------------------------------
# توابع کمکی دانلود ویدیو و پردازش SRT
# ---------------------------------------------------------------
def download_media(url: str, output_path: str):
    ydl_opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }
    if os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        ydl_opts['cookiefile'] = "cookie.txt"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def seconds_to_srt_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def create_srt_file(subtitles: list, srt_path: str):
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, sub in enumerate(subtitles, start=1):
            start_str = seconds_to_srt_time(sub.start_seconds)
            end_str = seconds_to_srt_time(sub.end_seconds)
            f.write(f"{idx}\n{start_str} --> {end_str}\n{sub.persian_text}\n\n")


# ---------------------------------------------------------------
# پروسه اصلی رندر ویدیو و چسباندن زیرنویس در پس‌زمینه
# ---------------------------------------------------------------
def background_subtitle_process(video_url: str, subtitles: list):
    if not render_lock.acquire(blocking=False):
        logging.warning("یک پروسه رندر دیگر در حال اجراست.")
        return

    start_time = time.time()
    msg_id = send_telegram_message("⏳ <b>شروع پروسه ساخت و چسباندن زیرنویس...</b>\nدر حال بررسی فایل‌ها...")

    try:
        video_path = os.path.join(TEMP_DIR, "original_video.mp4")
        srt_path = os.path.join(TEMP_DIR, "subs.srt")
        output_path = os.path.join(TEMP_DIR, "final_subtitled_video.mp4")

        # اگر ویدیو از قبل برای استخراج صوت دانلود شده بود، از همان استفاده کن، وگرنه دانلود کن
        if not os.path.exists(video_path):
            download_media(video_url, video_path)

        # ساخت فایل SRT
        create_srt_file(subtitles, srt_path)

        update_telegram_message(msg_id, "🎬 <b>در حال حک کردن زیرنویس روی ویدیو (FFmpeg)...</b>\n[████████░░] 80%")
        logging.info("شروع رندر ویدیو با زیرنویس توسط FFmpeg")

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=30'",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy",
            output_path
        ]

        subprocess.run(ffmpeg_cmd, check=True)

        total_time = round(time.time() - start_time, 1)
        update_telegram_message(msg_id, f"✨ <b>زیرنویس با موفقیت روی ویدیو قرار گرفت!</b>\n⏱ زمان کل: {total_time} ثانیه")
        
        caption = f"✨ <b>ویدیوی زیرنویس‌شده آماده شد!</b>\n⏱ زمان ساخت: {total_time} ثانیه"
        send_telegram_video(output_path, caption)

    except Exception as e:
        total_time = round(time.time() - start_time, 1)
        logging.error(f"❌ خطا در رندر زیرنویس: {str(e)}")
        update_telegram_message(msg_id, f"❌ <b>خطا در پردازش ویدیو!</b>\n<code>{str(e)}</code>")
    finally:
        render_lock.release()


# ---------------------------------------------------------------
# اندپوینت‌های FastAPI
# ---------------------------------------------------------------

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Subtitle Pipeline Server is Ready"}


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: dict):
    """اندپوینت برای دانلود ویدیو از اینستاگرام/لینک و استخراج فایل صوتی جهت تحویل به جمینای"""
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url ارسال نشده است.")

    temp_video_path = os.path.join(TEMP_DIR, "original_video.mp4")
    final_wav_path = os.path.join(TEMP_DIR, "audio.wav")

    try:
        # اگر ویدیو وجود نداشت، دانلودش کن
        if not os.path.exists(temp_video_path):
            download_media(video_url, temp_video_path)
        
        # استخراج فایل صوتی WAV برای جمینای
        cmd = [
            "ffmpeg", "-y", "-i", temp_video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            final_wav_path
        ]
        subprocess.run(cmd, check=True)

        return FileResponse(path=final_wav_path, filename="audio.wav", media_type="audio/wav")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process-subtitles")
def process_subtitles(data: ProcessSubtitlesRequest, background_tasks: BackgroundTasks):
    """اندپوینت دریافت زیرنویس‌ها و شروع رندر نهایی ویدیو"""
    background_tasks.add_task(background_subtitle_process, data.video_url, data.subtitles)
    
    return {
        "status": "started",
        "message": "پروسه ساخت و چسباندن زیرنویس آغاز شد. نتیجه در تلگرام ارسال خواهد شد."
    }
