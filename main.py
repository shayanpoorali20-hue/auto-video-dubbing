import os
import tempfile
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Video Dubbing & Audio Extraction Service")

# مدل‌های ورودی درخواست‌ها
class ExtractAudioRequest(BaseModel):
    video_url: str

class DubbingRequest(BaseModel):
    video_url: str
    audio_segments: list  # لیست وویس‌های فارسی برای سینک


def download_media(url: str, output_path: str):
    """
    دانلود کامل فایل ویدیو (پشتیبانی کامل از اینستاگرام و پلتفرم‌های عمومی)
    """
    ydl_opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    # در صورت وجود فایل کوکی (برای پیج‌های پرایوت یا محدودشده اینستاگرام)
    cookie_file = None
    if os.path.exists("cookies.txt"):
        cookie_file = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        cookie_file = "cookie.txt"

    if cookie_file:
        print(f"--- [DEBUG] Using cookie file: {cookie_file} ---")
        ydl_opts['cookiefile'] = cookie_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


@app.get("/")
def read_root():
    return {"status": "ok", "message": "Service is running smoothly for Instagram & general media."}


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: ExtractAudioRequest):
    """
    ۱. دانلود ویدیو (اینستاگرام)
    ۲. استخراج فایل صوتی WAV با FFmpeg برای تحویل به Gemini
    """
    temp_dir = tempfile.mkdtemp()
    temp_video_path = os.path.join(temp_dir, "input_video.mp4")
    final_wav_path = os.path.join(temp_dir, "audio.wav")

    try:
        print(f"--- Downloading video from: {data.video_url} ---")
        download_media(data.video_url, temp_video_path)

        if not os.path.exists(temp_video_path):
            raise HTTPException(status_code=500, detail="دانلود ویدیو با مشکل مواجه شد.")

        print("--- Extracting WAV audio using FFmpeg ---")
        cmd = [
            "ffmpeg", "-y", "-i", temp_video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            final_wav_path
        ]
        subprocess.run(cmd, check=True)

        if not os.path.exists(final_wav_path):
            raise HTTPException(status_code=500, detail="استخراج فایل صوتی ناموفق بود.")

        return FileResponse(
            path=final_wav_path,
            filename="audio.wav",
            media_type="audio/wav"
        )

    except Exception as e:
        print(f"--- ERROR in /get-audio-for-gemini: {str(e)} ---")
        raise HTTPException(status_code=500, detail=str(e))


# در صورت نیاز به اندپوینت دوم برای فرآیند دوبله و رندر ویدیو
@app.post("/download-video")
def download_video_only(data: ExtractAudioRequest):
    """
    اندپوینت ساده برای دریافت خود فایل ویدیو MP4
    """
    temp_dir = tempfile.mkdtemp()
    temp_video_path = os.path.join(temp_dir, "video.mp4")

    try:
        download_media(data.video_url, temp_video_path)
        return FileResponse(
            path=temp_video_path,
            filename="video.mp4",
            media_type="video/mp4"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
