import os
import re
import json
import shutil
import time
import logging
import tempfile
import subprocess
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

# تنظیمات Logging برای مشاهده دقیق جزئیات در Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = FastAPI(title="Instagram Auto Dubbing Service")

# پوشه موقت کاری برای کل پروژه
TEMP_DIR = tempfile.mkdtemp()

# ==========================================
# 🔑 اطلاعات تلگرام خودت را اینجا وارد کن
# ==========================================
TELEGRAM_BOT_TOKEN = "8291063125:AAHObys2HV7GrzzSTVxOd86eQvbpMsxN5Q0"  # توکن ربات تلگرام
TELEGRAM_CHAT_ID = "6387774646"      # چت آیدی تلگرام شما


class InitProjectRequest(BaseModel):
    video_url: str
    subtitles: list  # آرایه‌ای از تایم‌کدها و متن‌ها


# ---------------------------------------------------------------
# توابع کمکی ارتباط با تلگرام
# ---------------------------------------------------------------
def send_telegram_message(text: str) -> int:
    """ارسال یک پیام جدید به تلگرام و برگرداندن message_id برای ویرایش‌های بعدی"""
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
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
    """ویرایش پیام لودینگ قبلی در تلگرام (برای نوار پیشرفت)"""
    if not message_id or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
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
    """ارسال مستقیم فایل ویدیوی نهایی به تلگرام"""
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    
    if not os.path.exists(video_path):
        send_telegram_message(f"❌ ویدیو در مسیر یافت نشد: {video_path}")
        return

    try:
        with open(video_path, 'rb') as video_file:
            files = {'video': video_file}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}
            logging.info("در حال ارسال ویدیوی نهایی به تلگرام...")
            res = requests.post(url, data=data, files=files, timeout=180).json()
            if res.get("ok"):
                logging.info("ویدیو با موفقیت به تلگرام ارسال شد!")
            else:
                send_telegram_message(f"❌ خطا در ارسال ویدیو: {res.get('description')}")
    except Exception as e:
        logging.error(f"خطا در ارسال ویدیو: {e}")
        send_telegram_message(f"❌ خطای غیرمنتظره در ارسال فایل ویدیو: {e}")


# ---------------------------------------------------------------
# توابع کمکی دانلود و FFmpeg (کد خودت)
# ---------------------------------------------------------------
def download_media(url: str, output_path: str):
    """دانلود مستقیم ویدیو از اینستاگرام"""
    ydl_opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    cookie_file = None
    if os.path.exists("cookies.txt"):
        cookie_file = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        cookie_file = "cookie.txt"

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def get_audio_duration(file_path):
    cmd = ["ffmpeg", "-i", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise ValueError(f"طول فایل خوانده نشد: {file_path}")
    h, m, s = map(float, match.groups())
    return h * 3600 + m * 60 + s


def build_atempo_chain(speed):
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)


def parse_time_code(tc):
    m = re.match(r"\[\s*(\d+):(\d+)\s*-\s*(\d+):(\d+)\s*\]", tc)
    if not m:
        raise ValueError(f"فرمت time_code نامعتبر است: {tc}")
    m1, s1, m2, s2 = map(int, m.groups())
    return float(m1 * 60 + s1), float(m2 * 60 + s2)


def adjust_audio_speed(input_audio, output_audio, target_duration, min_speed=0.7, max_speed=3.0, fade=0.15):
    current = get_audio_duration(input_audio)
    raw_speed = current / target_duration
    speed = max(min_speed, min(raw_speed, max_speed))

    if 0.5 <= speed <= 2.0:
        speed_filter = f"rubberband=tempo={speed:.6f}:pitch=1"
    else:
        speed_filter = build_atempo_chain(speed)

    fade_start = max(0.0, target_duration - fade)
    filter_str = f"{speed_filter},afade=t=out:st={fade_start:.3f}:d={fade:.3f}"

    cmd = [
        "ffmpeg", "-y", "-i", input_audio,
        "-filter:a", filter_str,
        "-t", f"{target_duration:.3f}",
        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
        output_audio
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------
# تابع پردازش اصلی رندر در پس‌زمینه
# ---------------------------------------------------------------
def background_dubbing_process():
    """پروسه سنگین ادیت، تنظیم سرعت، میکس FFmpeg و ارسال خروجی به تلگرام"""
    start_time = time.time()
    
    # پیام اولیه در تلگرام
    msg_id = send_telegram_message("⏳ <b>شروع پروسه رندر ویدیو...</b>\nدر حال تایید و خواندن فایل‌ها...")

    try:
        video_path = os.path.join(TEMP_DIR, "original_video.mp4")
        sub_path = os.path.join(TEMP_DIR, "subtitles.json")
        output_path = os.path.join(TEMP_DIR, "final_dubbed_video.mp4")

        if not os.path.exists(video_path) or not os.path.exists(sub_path):
            raise Exception("فایل ویدیو یا زیرنویس اصلی پیدا نشد.")

        with open(sub_path, "r", encoding="utf-8") as f:
            subtitles = json.load(f)

        # مرحله ۱: تنظیم سرعت ویس‌ها
        elapsed = round(time.time() - start_time, 1)
        update_telegram_message(msg_id, f"🎙 <b>در حال تنظیم سرعت ویس‌ها (Rubberband)...</b>\n⏱ زمان طی شده: {elapsed} ثانیه\n[███░░░░░░░] 30%")
        logging.info("مرحله ۱: تنظیم سرعت تک‌تک ویس‌ها")

        audio_segments = []
        for i, sub in enumerate(subtitles):
            raw_audio = os.path.join(TEMP_DIR, f"audio_line_{i}.wav")
            if not os.path.exists(raw_audio):
                continue

            tc = sub.get("time_code") if isinstance(sub, dict) else sub[0]
            start, end = parse_time_code(tc)
            
            target_dur = end - start
            proc_audio = os.path.join(TEMP_DIR, f"processed_{i}.wav")
            adjust_audio_speed(raw_audio, proc_audio, target_dur)
            audio_segments.append({"start": start, "file": proc_audio})

        if not audio_segments:
            raise Exception("هیچ فایل صوتی پردازش‌شده‌ای برای میکس پیدا نشد.")

        # مرحله ۲: ترکیب فایل‌های صوتی با FFmpeg
        elapsed = round(time.time() - start_time, 1)
        update_telegram_message(msg_id, f"🎬 <b>در حال رندر و سینک صوتی روی ویدیو (FFmpeg)...</b>\nاین مرحله ممکنه ۲ تا ۵ دقیقه طول بکشه.\n⏱ زمان طی شده: {elapsed} ثانیه\n[██████░░░░] 60%")
        logging.info("مرحله ۲: شروع فرمان FFmpeg filter_complex")

        inputs = ["-i", video_path]
        filter_parts = []

        for i, item in enumerate(audio_segments):
            inputs += ["-i", item["file"]]
            delay_ms = int(round(item["start"] * 1000))
            filter_parts.append(
                f"[{i+1}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,adelay={delay_ms}:all=1[a{i}]"
            )

        mix_inputs = "".join(f"[a{i}]" for i in range(len(audio_segments)))
        filter_parts.append(
            f"{mix_inputs}amix=inputs={len(audio_segments)}:duration=longest:normalize=0[aout]"
        )
        filter_complex = ";".join(filter_parts)

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_path
        ]
        subprocess.run(cmd, check=True)

        # مرحله ۳: اتمام و ارسال
        total_time = round(time.time() - start_time, 1)
        update_telegram_message(msg_id, f"🚀 <b>رندر با موفقیت انجام شد!</b>\n⏱ زمان کل ساخت: {total_time} ثانیه\nدر حال آپلود ویدیو در تلگرام...")
        
        caption = f"✨ <b>ویدیوی دوبله‌شده آماده شد!</b>\n⏱ زمان ساخت: {total_time} ثانیه"
        send_telegram_video(output_path, caption)

    except Exception as e:
        total_time = round(time.time() - start_time, 1)
        logging.error(f"❌ خطا در رندر: {str(e)}")
        update_telegram_message(msg_id, f"❌ <b>خطا در پردازش ویدیو!</b>\nمتن خطا: <code>{str(e)}</code>\n⏱ زمان طی شده: {total_time} ثانیه")


# ---------------------------------------------------------------
# اندپوینت‌های FastAPI
# ---------------------------------------------------------------

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Dubbing Pipeline Server is Ready"}


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: dict):
    """گرفتن لینک اینستاگرام، پاک کردن فایل‌های قبلی، دانلود و تحویل فایل wav جدید به Gemini"""
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url ارسال نشده است.")

    temp_video_path = os.path.join(TEMP_DIR, "original_video.mp4")
    final_wav_path = os.path.join(TEMP_DIR, "audio.wav")

    if os.path.exists(temp_video_path):
        os.remove(temp_video_path)
    if os.path.exists(final_wav_path):
        os.remove(final_wav_path)

    try:
        download_media(video_url, temp_video_path)
        
        cmd = [
            "ffmpeg", "-y", "-i", temp_video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            final_wav_path
        ]
        subprocess.run(cmd, check=True)

        return FileResponse(path=final_wav_path, filename="audio.wav", media_type="audio/wav")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/init-project")
def init_project(data: InitProjectRequest):
    """مرحله ۱: ذخیره دیتای زیرنویس JSON و اطمینان از دانلود ویدیو اصلی"""
    try:
        video_path = os.path.join(TEMP_DIR, "original_video.mp4")
        if not os.path.exists(video_path):
            download_media(data.video_url, video_path)

        sub_path = os.path.join(TEMP_DIR, "subtitles.json")
        with open(sub_path, "w", encoding="utf-8") as f:
            json.dump(data.subtitles, f, ensure_ascii=False)

        return {"status": "success", "message": "اطلاعات زیرنویس و ویدیوی اصلی ذخیره شدند."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-audio")
async def upload_audio(line_index: int = Form(...), file: UploadFile = File(...)):
    """مرحله ۲: دریافت تک‌تک فایل‌های صوتی از n8n در طول لوپ"""
    try:
        audio_path = os.path.join(TEMP_DIR, f"audio_line_{line_index}.wav")
        with open(audio_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        return {"status": "success", "message": f"فایل صوتی خط {line_index} ذخیره شد."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process-dubbing")
def process_dubbing(background_tasks: BackgroundTasks):
    """
    مرحله ۳: انتهای کار؛ بلافاصله 200 OK می‌دهد تا n8n ارور ۵۰۲ نگیرد،
    و پروسه ادیت و ارسال ویدیو را در پس‌زمینه سرور اجرا می‌کند.
    """
    background_tasks.add_task(background_dubbing_process)
    
    return {
        "status": "started",
        "message": "پروسه ادیت و سینک صوتی آغاز شد. نوار پیشرفت و فایل نهایی در تلگرام ارسال می‌شود."
    }
