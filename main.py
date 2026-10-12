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

# حداکثر حجم مجاز برای ارسال ویدیو در Bot API (50MB)
TELEGRAM_MAX_VIDEO_MB = 50

# تایم‌اوت ffmpeg (ثانیه)
FFMPEG_TIMEOUT = 1800


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
        logging.error(f"تلگرام خطا داد: {res}")
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
        send_telegram_message("❌ <b>خطا:</b> فایل ویدیو یافت نشد.")
        return

    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if file_size_mb > TELEGRAM_MAX_VIDEO_MB:
        send_telegram_message(
            f"⚠️ <b>حجم ویدیو {file_size_mb:.2f} مگابایت است و از حد مجاز تلگرام "
            f"({TELEGRAM_MAX_VIDEO_MB}MB) بیشتر است.</b>\n"
            f"فایل روی سرور ساخته شد اما قابل ارسال از طریق Bot API نیست."
        )
        return

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

        with urllib.request.urlopen(req, timeout=600) as response:
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
    if seconds is None or seconds < 0:
        seconds = 0.0

    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3600000
    minutes = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    milliseconds = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def create_srt_file(subtitles: list, srt_path: str):
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, sub in enumerate(subtitles, start=1):
            start_str = seconds_to_srt_time(sub.start_seconds)
            end_str = seconds_to_srt_time(sub.end_seconds)
            f.write(f"{idx}\n{start_str} --> {end_str}\n{sub.persian_text}\n\n")


def get_video_size(video_path: str) -> str:
    """ابعاد ویدیو رو با ffprobe می‌گیره. اگه نشد، پیش‌فرض 1080x1920 برمی‌گردونه."""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                video_path
            ],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL
        )
        size = probe.stdout.strip()
        if "x" in size:
            return size
    except Exception as e:
        logging.warning(f"ffprobe خطا داد: {e}")
    return "1080x1920"


def run_ffmpeg(cmd: list, timeout: int = FFMPEG_TIMEOUT):
    logging.info(f"اجرای ffmpeg: {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg بعد از {timeout} ثانیه تایم‌اوت شد و متوقف شد.")

    elapsed = time.time() - t0
    logging.info(f"⏱ زمان اجرای ffmpeg: {elapsed:.1f} ثانیه")

    stderr_text = result.stderr.decode(errors="ignore") if result.stderr else ""
    if result.returncode != 0:
        tail = stderr_text[-2000:]
        logging.error(f"ffmpeg با کد {result.returncode} خارج شد:\n{tail}")
        raise RuntimeError(f"ffmpeg خطا داد (کد {result.returncode}):\n{tail}")
    return stderr_text


# ---------------------------------------------------------------
# پروسه اصلی رندر ویدیو و چسباندن زیرنویس در پس‌زمینه
# ---------------------------------------------------------------
def background_subtitle_process(video_url: str, subtitles: list):
    if not render_lock.acquire(blocking=False):
        logging.warning("یک پروسه رندر دیگر در حال اجراست.")
        send_telegram_message("⚠️ یک پروسه رندر دیگر در حال اجراست. لطفاً بعداً تلاش کنید.")
        return

    start_time = time.time()
    msg_id = send_telegram_message("⏳ <b>شروع پروسه ساخت و چسباندن زیرنویس...</b>\nدر حال بررسی فایل‌ها...")

    job_dir = tempfile.mkdtemp(dir=TEMP_DIR)
    video_path = os.path.join(job_dir, "original_video.mp4")
    srt_path = os.path.join(job_dir, "subs.srt")
    output_path = os.path.join(job_dir, "final_subtitled_video.mp4")

    try:
        # 1) دانلود ویدیو
        if not os.path.exists(video_path):
            update_telegram_message(msg_id, "⬇️ <b>در حال دانلود ویدیو از لینک...</b>")
            download_media(video_url, video_path)

        if not os.path.exists(video_path):
            raise RuntimeError("دانلود ویدیو ناموفق بود؛ فایل خروجی ساخته نشد.")

        # 2) ساخت فایل SRT
        create_srt_file(subtitles, srt_path)

        # 3) تشخیص ابعاد واقعی ویدیو
        video_size = get_video_size(video_path)
        try:
            v_w, v_h = map(int, video_size.split("x"))
        except Exception:
            v_w, v_h = 1080, 1920
            video_size = "1080x1920"
        logging.info(f"ابعاد ویدیو: {v_w}x{v_h}")

        # 4) اندازه فونت = ۲.۵٪ ارتفاع ویدیو (نصف حالت معمول)
        font_size = max(20, int(v_h * 0.025))

        # 5) استایل: وسط صفحه، بک‌گراند مشکی مستطیلی
        style_params = ",".join([
            "FontName=Vazir",
            f"FontSize={font_size}",
            "PrimaryColour=&H00FFFFFF",   # متن سفید
            "OutlineColour=&H00000000",   # رنگ حاشیه کادر (مشکی)
            "BackColour=&H00000000",      # بک‌گراند مشکی کاملاً اوپک
            "BorderStyle=3",              # کادر مستطیلی به جای outline خطی
            "Outline=2",                  # پدینگ داخلی کادر
            "Shadow=0",
            "Alignment=5",                # وسط-وسط
            "MarginL=30",
            "MarginR=30",
            "MarginV=0",
        ])

        # 6) مسیرها
        fonts_dir = os.path.abspath("fonts")
        srt_path_escaped = srt_path.replace("\\", "/").replace(":", "\\:")

        if os.path.isdir(fonts_dir):
            fonts_dir_escaped = fonts_dir.replace("\\", "/").replace(":", "\\:")
            subtitles_filter = (
                f"subtitles='{srt_path_escaped}'"
                f":fontsdir='{fonts_dir_escaped}'"
                f":original_size={video_size}"
                f":force_style='{style_params}'"
            )
        else:
            logging.warning(f"پوشه فونت پیدا نشد: {fonts_dir} — از فونت پیش‌فرض استفاده می‌شود.")
            subtitles_filter = (
                f"subtitles='{srt_path_escaped}'"
                f":original_size={video_size}"
                f":force_style='{style_params}'"
            )

        update_telegram_message(
            msg_id,
            "🎬 <b>در حال حک کردن زیرنویس روی ویدیو (FFmpeg)...</b>\n[████████░░] 80%"
        )
        logging.info("شروع رندر ویدیو با زیرنویس توسط FFmpeg")

        # 7) دستور ffmpeg
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", video_path,
            "-vf", subtitles_filter,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "copy",
            output_path
        ]

        run_ffmpeg(ffmpeg_cmd, timeout=FFMPEG_TIMEOUT)

        if not os.path.exists(output_path):
            raise RuntimeError("فایل خروجی پس از اجرای ffmpeg ساخته نشد.")

        output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        total_time = round(time.time() - start_time, 1)

        update_telegram_message(
            msg_id,
            f"✨ <b>زیرنویس با موفقیت روی ویدیو قرار گرفت!</b>\n"
            f"⏱ زمان کل: {total_time} ثانیه\n"
            f"📦 حجم خروجی: {output_size_mb:.2f} مگابایت"
        )

        caption = (
            f"✨ <b>ویدیوی زیرنویس‌شده آماده شد!</b>\n"
            f"⏱ زمان ساخت: {total_time} ثانیه\n"
            f"📦 حجم: {output_size_mb:.2f} مگابایت"
        )
        send_telegram_video(output_path, caption)

    except Exception as e:
        total_time = round(time.time() - start_time, 1)
        logging.exception("❌ خطا در رندر زیرنویس")
        err_text = str(e)[:1500]
        update_telegram_message(
            msg_id,
            f"❌ <b>خطا در پردازش ویدیو!</b>\n"
            f"⏱ بعد از {total_time} ثانیه\n"
            f"<code>{err_text}</code>"
        )
    finally:
        render_lock.release()
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception as cleanup_err:
            logging.warning(f"خطا در پاک‌سازی پوشه موقت: {cleanup_err}")


# ---------------------------------------------------------------
# اندپوینت‌های FastAPI
# ---------------------------------------------------------------

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Subtitle Pipeline Server is Ready"}


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: dict):
    """اندپوینت برای دانلود ویدیو و استخراج فایل صوتی جهت تحویل به جمینای"""
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url ارسال نشده است.")

    job_dir = tempfile.mkdtemp(dir=TEMP_DIR)
    temp_video_path = os.path.join(job_dir, "original_video.mp4")
    final_wav_path = os.path.join(job_dir, "audio.wav")

    try:
        download_media(video_url, temp_video_path)

        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", temp_video_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            final_wav_path
        ]
        run_ffmpeg(cmd, timeout=600)

        return FileResponse(path=final_wav_path, filename="audio.wav", media_type="audio/wav")
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process-subtitles")
def process_subtitles(data: ProcessSubtitlesRequest, background_tasks: BackgroundTasks):
    """اندپوینت دریافت زیرنویس‌ها و شروع رندر نهایی ویدیو"""
    background_tasks.add_task(background_subtitle_process, data.video_url, data.subtitles)

    return {
        "status": "started",
        "message": "پروسه ساخت و چسباندن زیرنویس آغاز شد. نتیجه در تلگرام ارسال خواهد شد."
    }
