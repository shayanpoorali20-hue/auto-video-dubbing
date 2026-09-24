import os
import re
import subprocess
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
import yt_dlp

app = FastAPI(title="Auto Dubbing Video Editor")


# ---------------------------------------------------------------
# مدل‌های ورودی برای n8n
# ---------------------------------------------------------------
class SubtitleItem(BaseModel):
    time_code: str
    text: str | None = None


class DubbingRequest(BaseModel):
    video_url: str
    subtitles: list[SubtitleItem]
    audio_urls: list[str]


class ExtractAudioRequest(BaseModel):
    video_url: str


# ---------------------------------------------------------------
# توابع کمکی دانلود با yt-dlp
# ---------------------------------------------------------------
def download_media_with_ytdlp(url: str, output_path: str, is_audio_only: bool = False):
    ydl_opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        'geo_bypass': True,
    }

    # چک کردن فایل کوکی
    cookie_file = None
    if os.path.exists("cookies.txt"):
        cookie_file = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        cookie_file = "cookie.txt"

    if cookie_file:
        print(f"--- [DEBUG] Using cookie file: {cookie_file} ---")
        ydl_opts['cookiefile'] = cookie_file

    if is_audio_only:
        ydl_opts.update({
            # اولویت با بهترین کیفیت صدا، اگر نشد هر کیفیتی که صدا داشت
            'format': 'bestaudio/best/ba*',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# ---------------------------------------------------------------
# توابع کمکی پردازش صدا و زمان‌بندی
# ---------------------------------------------------------------
def get_audio_duration(file_path):
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"فایل پیدا نشد: {file_path}")

    cmd = ["ffmpeg", "-i", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise ValueError(f"طول فایل صوتی خوانده نشد: {file_path}")
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


def process_and_merge_dubbing(video_path, audio_segments, output_path):
    processed = []

    for i, seg in enumerate(audio_segments):
        target = seg["end"] - seg["start"]
        temp = f"temp_processed_{i}.wav"
        adjust_audio_speed(seg["file"], temp, target)
        processed.append({"start": seg["start"], "file": temp})

    inputs = ["-i", video_path]
    filter_parts = []

    for i, item in enumerate(processed):
        inputs += ["-i", item["file"]]
        delay_ms = int(round(item["start"] * 1000))
        filter_parts.append(
            f"[{i+1}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,adelay={delay_ms}:all=1[a{i}]"
        )

    mix_inputs = "".join(f"[a{i}]" for i in range(len(processed)))
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(processed)}:duration=longest:normalize=0[aout]"
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

    for item in processed:
        if os.path.exists(item["file"]):
            os.remove(item["file"])


def download_file(url, save_path):
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
    else:
        raise HTTPException(status_code=400, detail=f"خطا در دانلود فایل: {url}")


# ---------------------------------------------------------------
# اندپوینت‌های اصلی API
# ---------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Dubbing API with yt-dlp is running!"}


# ۱. استخراج مستقیم فایل صوتی برای Gemini
@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: ExtractAudioRequest):
    temp_dir = tempfile.mkdtemp()
    output_wav = os.path.join(temp_dir, "extracted_audio")
    
    try:
        download_media_with_ytdlp(data.video_url, output_wav, is_audio_only=True)
        final_wav_path = output_wav + ".wav"
        
        if not os.path.exists(final_wav_path):
            raise HTTPException(status_code=500, detail="استخراج فایل صوتی ناموفق بود.")
            
        return FileResponse(
            path=final_wav_path,
            filename="audio.wav",
            media_type="audio/wav"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ۲. پردازش و میکس نهایی دابله
@app.post("/process-dubbing")
def handle_dubbing(data: DubbingRequest):
    if len(data.subtitles) != len(data.audio_urls):
        raise HTTPException(status_code=400, detail="تعداد ویس‌ها با تعداد زیرنویس‌ها برابر نیست!")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        video_file = str(temp_path / "input_video.mp4")
        output_file = str(temp_path / "output_video.mp4")

        # دانلود ویدیو با yt-dlp
        download_media_with_ytdlp(data.video_url, video_file, is_audio_only=False)

        # دانلود فایل‌های ویس
        audio_segments = []
        for i, (sub, url) in enumerate(zip(data.subtitles, data.audio_urls), start=1):
            start, end = parse_time_code(sub.time_code)
            audio_path = temp_path / f"audio_{i}.wav"
            download_file(url, audio_path)
            audio_segments.append({"start": start, "end": end, "file": str(audio_path)})

        # پردازش و دابله
        process_and_merge_dubbing(video_file, audio_segments, output_file)

        return {
            "status": "success",
            "message": "ویدیو با موفقیت رندر شد.",
            "processed_segments": len(audio_segments)
        }
