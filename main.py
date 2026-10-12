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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI(title="Instagram Auto Subtitle Service")

TEMP_DIR = tempfile.mkdtemp()
render_lock = threading.Lock()

# ==========================================
# 🔑 اطلاعات تلگرام
# ==========================================
TELEGRAM_BOT_TOKEN = "8956121858:AAF1ZQD-KCKSCbd-GOfGc2CziHpBFBONhxA"
TELEGRAM_CHAT_ID = "5080371184"

TELEGRAM_MAX_VIDEO_MB = 50
FFMPEG_TIMEOUT = 1800

# ---------- تنظیمات ظاهر زیرنویس ----------
# هر کدوم رو خواستی تغییر بده
FONT_NAME = "Vazir"                 # اسم فونت داخل فایل ttf
FONT_SIZE_RATIO = 0.035             # نسبت ارتفاع فونت به ارتفاع ویدیو (0.02 = خیلی کوچک، 0.04 = بزرگ)
SUBTITLE_ALIGNMENT = 5              # 5=وسط-وسط ، 2=پایین-وسط ، 8=بالا-وسط
BOX_PADDING = 8                     # پدینگ داخلی کادر مشکی
BACK_COLOR = "&H00000000"           # بک‌گراند کادر (00=اوپک کامل، 80=نیمه‌شفاف)
PRIMARY_COLOR = "&H00FFFFFF"        # رنگ متن (سفید)
# --------------------------------------------


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
# توابع تلگرام
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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}
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
            f"⚠️ <b>حجم ویدیو {file_size_mb:.2f} مگابایت و از حد مجاز تلگرام "
            f"({TELEGRAM_MAX_VIDEO_MB}MB) بیشتر است.</b>"
        )
        return

    send_telegram_message(f"📦 <b>شروع آپلود ویدیو...</b>\nحجم: {file_size_mb:.2f} مگابایت")

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
                send_telegram_message("🚀 <b>ویدیو با موفقیت ارسال شد!</b>")
            else:
                send_telegram_message(f"❌ <b>خطای تلگرام:</b> {res.get('description')}")
    except Exception as e:
        send_telegram_message(f"❌ <b>خطای آپلود:</b> <code>{str(e)}</code>")


# ---------------------------------------------------------------
# دانلود و ابزار
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


def get_video_size(video_path: str):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", video_path],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL
        )
        size = probe.stdout.strip()
        if "x" in size:
            w, h = map(int, size.split("x"))
            return w, h
    except Exception as e:
        logging.warning(f"ffprobe خطا داد: {e}")
    return 1080, 1920


# ---------------------------------------------------------------
# ساخت فایل ASS (جایگزین SRT)
# ---------------------------------------------------------------
def seconds_to_ass_time(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    h = total_cs // 360000
    m = (total_cs % 360000) // 6000
    s = (total_cs % 6000) // 100
    cs = total_cs % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def create_ass_file(subtitles, ass_path, video_w, video_h):
    """ساخت فایل ASS با PlayRes برابر ابعاد واقعی ویدیو تا FontSize درست کار کند."""
    font_size = max(14, int(video_h * FONT_SIZE_RATIO))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{font_size},"
        f"{PRIMARY_COLOR},"       # PrimaryColour (متن)
        "&H000000FF,"             # SecondaryColour
        "&H00000000,"             # OutlineColour
        f"{BACK_COLOR},"          # BackColour (کادر مشکی)
        "0,0,0,0,"                # Bold, Italic, Underline, StrikeOut
        "100,100,0,0,"            # ScaleX, ScaleY, Spacing, Angle
        "3,"                      # BorderStyle=3 → کادر مستطیلی
        f"{BOX_PADDING},"         # Outline → پدینگ داخلی کادر
        "0,"                      # Shadow
        f"{SUBTITLE_ALIGNMENT},"  # Alignment
        "40,40,250,"               # MarginL, MarginR, MarginV
        "1\n"                     # Encoding
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for sub in subtitles:
            start = seconds_to_ass_time(sub.start_seconds)
            end = seconds_to_ass_time(sub.end_seconds)
            raw = sub.persian_text.replace("\r", "").replace("\n", "\\N")
            # کاراکتر RTL Embedding برای اینکه libass متن فارسی رو درست بچینه
            RLE = "\u202B"
            PDF = "\u202C"
            text = f"{RLE}{raw}{PDF}"
            f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")

    return font_size


# ---------------------------------------------------------------
# اجرای ffmpeg
# ---------------------------------------------------------------
def run_ffmpeg(cmd: list, timeout: int = FFMPEG_TIMEOUT):
    logging.info(f"اجرای ffmpeg: {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg بعد از {timeout} ثانیه تایم‌اوت شد.")

    logging.info(f"⏱ زمان اجرای ffmpeg: {time.time() - t0:.1f} ثانیه")

    stderr_text = result.stderr.decode(errors="ignore") if result.stderr else ""
    if result.returncode != 0:
        tail = stderr_text[-2000:]
        logging.error(f"ffmpeg با کد {result.returncode} خارج شد:\n{tail}")
        raise RuntimeError(f"ffmpeg خطا داد (کد {result.returncode}):\n{tail}")
    return stderr_text


# ---------------------------------------------------------------
# پروسه اصلی رندر
# ---------------------------------------------------------------
def background_subtitle_process(video_url: str, subtitles: list):
    if not render_lock.acquire(blocking=False):
        logging.warning("یک پروسه رندر دیگر در حال اجراست.")
        send_telegram_message("⚠️ یک پروسه رندر دیگر در حال اجراست.")
        return

    start_time = time.time()
    msg_id = send_telegram_message("⏳ <b>شروع پروسه ساخت زیرنویس...</b>")

    job_dir = tempfile.mkdtemp(dir=TEMP_DIR)
    video_path = os.path.join(job_dir, "original_video.mp4")
    ass_path = os.path.join(job_dir, "subs.ass")
    output_path = os.path.join(job_dir, "final_subtitled_video.mp4")

    try:
        # 1) دانلود
        if not os.path.exists(video_path):
            update_telegram_message(msg_id, "⬇️ <b>در حال دانلود ویدیو...</b>")
            download_media(video_url, video_path)

        if not os.path.exists(video_path):
            raise RuntimeError("دانلود ویدیو ناموفق بود.")

        # 2) ابعاد واقعی ویدیو
        v_w, v_h = get_video_size(video_path)
        logging.info(f"ابعاد ویدیو: {v_w}x{v_h}")

        # 3) ساخت ASS با PlayRes = ابعاد ویدیو
        font_size = create_ass_file(subtitles, ass_path, v_w, v_h)
        logging.info(f"فایل ASS ساخته شد. FontSize={font_size}, Alignment={SUBTITLE_ALIGNMENT}")

        update_telegram_message(
            msg_id,
            f"🎬 <b>در حال حک کردن زیرنویس روی ویدیو...</b>\n"
            f"📐 ابعاد: {v_w}×{v_h}\n"
            f"🔤 اندازه فونت: {font_size}px"
        )

        # 4) فیلتر subtitles با فایل ASS
        ass_path_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        fonts_dir = os.path.abspath("fonts")
        if os.path.isdir(fonts_dir):
            fonts_dir_escaped = fonts_dir.replace("\\", "/").replace(":", "\\:")
            subtitles_filter = f"subtitles='{ass_path_escaped}':fontsdir='{fonts_dir_escaped}'"
        else:
            logging.warning(f"پوشه فونت پیدا نشد: {fonts_dir}")
            subtitles_filter = f"subtitles='{ass_path_escaped}'"

        # 5) اجرا
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
            raise RuntimeError("فایل خروجی ساخته نشد.")

        output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        total_time = round(time.time() - start_time, 1)

        update_telegram_message(
            msg_id,
            f"✨ <b>زیرنویس با موفقیت چسبید!</b>\n"
            f"⏱ زمان کل: {total_time} ثانیه\n"
            f"📦 حجم: {output_size_mb:.2f} مگابایت"
        )

        caption = (
            f"✨ <b>ویدیوی زیرنویس‌شده آماده شد!</b>\n"
            f"⏱ زمان: {total_time}s | 📦 {output_size_mb:.1f}MB"
        )
        send_telegram_video(output_path, caption)

    except Exception as e:
        total_time = round(time.time() - start_time, 1)
        logging.exception("❌ خطا در رندر زیرنویس")
        update_telegram_message(
            msg_id,
            f"❌ <b>خطا در پردازش!</b>\n⏱ بعد از {total_time}s\n<code>{str(e)[:1000]}</code>"
        )
    finally:
        render_lock.release()
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------
# اندپوینت‌ها
# ---------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Subtitle Pipeline Server is Ready"}


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: dict):
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url ارسال نشده.")

    job_dir = tempfile.mkdtemp(dir=TEMP_DIR)
    temp_video_path = os.path.join(job_dir, "original_video.mp4")
    final_wav_path = os.path.join(job_dir, "audio.wav")

    try:
        download_media(video_url, temp_video_path)
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", temp_video_path,
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "44100", "-ac", "2",
            final_wav_path
        ]
        run_ffmpeg(cmd, timeout=600)
        return FileResponse(path=final_wav_path, filename="audio.wav", media_type="audio/wav")
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process-subtitles")
def process_subtitles(data: ProcessSubtitlesRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(background_subtitle_process, data.video_url, data.subtitles)
    return {
        "status": "started",
        "message": "پروسه آغاز شد. نتیجه در تلگرام ارسال خواهد شد."
    }
