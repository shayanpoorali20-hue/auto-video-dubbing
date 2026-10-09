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
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import requests

# ==========================================================
# 📚 کتابخانه‌های پردازش صدا
# ==========================================================
import numpy as np
import soundfile as sf
import noisereduce as nr

# nara_wpe اختیاری است (ممکن است روی Render نصب نشود)
try:
    from nara_wpe import wpe as nara_wpe_fn
    NARA_WPE_AVAILABLE = True
except Exception:
    try:
        from nara_wpe.wpe import wpe as nara_wpe_fn
        NARA_WPE_AVAILABLE = True
    except Exception:
        nara_wpe_fn = None
        NARA_WPE_AVAILABLE = False

# pedalboard اختیاری است
try:
    from pedalboard import Pedalboard, NoiseGate, Compressor, Reverb, Limiter
    from pedalboard.io import AudioFile
    PEDALBOARD_AVAILABLE = True
except Exception:
    PEDALBOARD_AVAILABLE = False


# ==========================================================
# ⚙️ تنظیمات Logging
# ==========================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
if not NARA_WPE_AVAILABLE:
    logging.warning("nara_wpe در دسترس نیست — مرحله De-reverb رد می‌شود.")
if not PEDALBOARD_AVAILABLE:
    logging.warning("pedalboard در دسترس نیست — مرحله افکت‌ها رد می‌شود.")


# ==========================================================
# 🎛️ پارامترهای قابل تنظیم پردازش صدا
# ==========================================================
# --- Noisereduce ---
NR_STATIONARY          = False
NR_PROP_DECREASE       = 0.7    # شدت حذف نویز (0.0 تا 1.0) - برای TTS کم بگذار
NR_STD_THRESH          = 1.5    # آستانه انحراف معیار
NR_N_FFT               = 1024
NR_WIN_LENGTH          = 256
NR_FREQ_SMOOTH_HZ      = 50
NR_TIME_SMOOTH_MS      = 32
NR_TIME_CONSTANT_S     = 0.5

# --- Nara_wpe (De-reverb) ---
WPE_TAPS               = 10
WPE_DELAY              = 3
WPE_ITERATIONS         = 5
WPE_PSD_CONTEXT        = 0
WPE_STATISTICS_MODE    = "full"

# --- Speed change (Rubberband) ---
SPEED_MIN              = 0.7
SPEED_MAX              = 3.0
SPEED_FADE_SECONDS     = 0.05   # فید انتهایی کوتاه برای جلوگیری از خش

# --- Pedalboard Effects ---
GATE_THRESHOLD_DB      = -40.0
GATE_RATIO             = 2.0
GATE_ATTACK_MS         = 5.0
GATE_RELEASE_MS        = 100.0

COMP_THRESHOLD_DB      = -18.0
COMP_RATIO             = 4.0
COMP_ATTACK_MS         = 5.0
COMP_RELEASE_MS        = 250.0

REVERB_ROOM_SIZE       = 0.2
REVERB_DAMPING         = 0.5
REVERB_WET_LEVEL       = 0.08   # خیلی کم — چون WPE قبلاً کار کرده
REVERB_WIDTH           = 0.5

LIMITER_THRESHOLD_DB   = -1.0
LIMITER_RELEASE_MS     = 100.0

# --- Final mix ---
LOUDNORM_I             = -16.0
LOUDNORM_TP            = -1.5
LOUDNORM_LRA           = 11.0


# ==========================================================
# 🔑 اطلاعات تلگرام
# ==========================================================
TELEGRAM_BOT_TOKEN = "8956121858:AAF1ZQD-KCKSCbd-GOfGc2CziHpBFBONhxA"
TELEGRAM_CHAT_ID   = "5080371184"


# ==========================================================
# 🚀 اپلیکیشن FastAPI
# ==========================================================
app = FastAPI(title="Instagram Auto Dubbing Service")
TEMP_DIR = tempfile.mkdtemp()
render_lock = threading.Lock()


class InitProjectRequest(BaseModel):
    video_url: str
    subtitles: list


# ==========================================================
# 📨 توابع تلگرام
# ==========================================================
def send_telegram_message(text: str) -> int:
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return res["result"]["message_id"]
        logging.error(f"خطای ارسال پیام تلگرام: {res.get('description')}")
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
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.warning(f"خطا در آپدیت پیام تلگرام: {e}")


def send_telegram_video(video_path: str, caption: str):
    if not os.path.exists(video_path):
        send_telegram_message(f"❌ <b>خطا:</b> فایل ویدیو یافت نشد:\n<code>{video_path}</code>")
        return

    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    send_telegram_message(f"📦 <b>شروع آپلود ویدیو...</b>\nحجم: {file_size_mb:.2f} مگابایت")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    try:
        body = []
        body.extend([
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="chat_id"',
            b"",
            str(TELEGRAM_CHAT_ID).encode(),
        ])
        body.extend([
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="caption"',
            b"",
            caption.encode("utf-8"),
        ])
        body.extend([
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="parse_mode"',
            b"",
            b"HTML",
        ])

        with open(video_path, "rb") as f:
            video_bytes = f.read()

        filename = os.path.basename(video_path)
        body.extend([
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="video"; filename="{filename}"'.encode(),
            b"Content-Type: video/mp4",
            b"",
            video_bytes,
        ])
        body.append(f"--{boundary}--".encode())
        body.append(b"")

        payload = b"\r\n".join(body)
        req = urllib.request.Request(url, data=payload, headers=headers)

        with urllib.request.urlopen(req, timeout=300) as response:
            res = json.loads(response.read().decode())
            if res.get("ok"):
                send_telegram_message("🚀 <b>ویدیو با موفقیت ارسال شد!</b>")
            else:
                send_telegram_message(f"❌ <b>تلگرام فایل را رد کرد:</b>\n<code>{res.get('description')}</code>")

    except urllib.error.HTTPError as e:
        send_telegram_message(f"❌ <b>خطای HTTP تلگرام:</b> {e.code}\n<code>{e.read().decode()}</code>")
    except Exception as e:
        send_telegram_message(f"❌ <b>خطای غیرمنتظره هنگام آپلود:</b>\n<code>{str(e)}</code>")


# ==========================================================
# 🎬 توابع کمکی پایه (دانلود، ffmpeg)
# ==========================================================
def download_media(url: str, output_path: str):
    ydl_opts = {
        "outtmpl": output_path,
        "quiet": False,
        "no_warnings": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    cookie_file = None
    if os.path.exists("cookies.txt"):
        cookie_file = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        cookie_file = "cookie.txt"
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

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


# ==========================================================
# 🔊 مراحل پردازش صدا
# ==========================================================
def stage_denoise(input_path: str, output_path: str) -> bool:
    """مرحله ۱: حذف نویز با noisereduce"""
    try:
        data, rate = sf.read(input_path, dtype="float32")

        def _reduce(y):
            return nr.reduce_noise(
                y=y,
                sr=rate,
                stationary=NR_STATIONARY,
                prop_decrease=NR_PROP_DECREASE,
                n_std_thresh_stationary=NR_STD_THRESH,
                time_constant_s=NR_TIME_CONSTANT_S,
                freq_mask_smooth_hz=NR_FREQ_SMOOTH_HZ,
                time_mask_smooth_ms=NR_TIME_SMOOTH_MS,
                n_fft=NR_N_FFT,
                win_length=NR_WIN_LENGTH,
                use_tqdm=False,
            )

        if data.ndim == 1:
            cleaned = _reduce(data)
        else:
            cleaned = np.zeros_like(data)
            for ch in range(data.shape[1]):
                cleaned[:, ch] = _reduce(data[:, ch])

        sf.write(output_path, cleaned, rate)
        return True
    except Exception as e:
        logging.error(f"خطا در حذف نویز: {e}")
        return False


def stage_dereverb(input_path: str, output_path: str) -> bool:
    """مرحله ۲: حذف ورب با nara_wpe (اختیاری)"""
    if not NARA_WPE_AVAILABLE:
        return False
    try:
        data, rate = sf.read(input_path, dtype="float32")
        # nara_wpe نیاز به آرایه (channels, samples) دارد
        if data.ndim == 1:
            signal = data[np.newaxis, :]
        else:
            signal = data.T

        dereverbed = nara_wpe_fn(
            signal,
            taps=WPE_TAPS,
            delay=WPE_DELAY,
            iterations=WPE_ITERATIONS,
            psd_context=WPE_PSD_CONTEXT,
            statistics_mode=WPE_STATISTICS_MODE,
        )

        # بازگشت به (samples,) یا (samples, channels)
        if dereverbed.shape[0] == 1:
            out = dereverbed[0]
        else:
            out = dereverbed.T

        # جلوگیری از NaN و کلیپینگ
        out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
        peak = np.max(np.abs(out)) or 1.0
        if peak > 1.0:
            out = out / peak

        sf.write(output_path, out.astype(np.float32), rate)
        return True
    except Exception as e:
        logging.error(f"خطا در حذف ورب: {e}")
        return False


def stage_speed_change(input_audio: str, output_audio: str, target_duration: float) -> None:
    """مرحله ۳: تغییر سرعت با Rubberband + adeclick + fade"""
    current = get_audio_duration(input_audio)
    raw_speed = current / target_duration
    speed = max(SPEED_MIN, min(raw_speed, SPEED_MAX))

    if 0.5 <= speed <= 2.0:
        speed_filter = f"rubberband=tempo={speed:.6f}:pitch=1:formant=1:pitchq=quality"
    else:
        speed_filter = build_atempo_chain(speed)

    fade = SPEED_FADE_SECONDS
    fade_start = max(0.0, target_duration - fade)
    filter_str = f"{speed_filter},adeclick,afade=t=out:st={fade_start:.3f}:d={fade:.3f}"

    cmd = [
        "ffmpeg", "-y", "-i", input_audio,
        "-filter:a", filter_str,
        "-t", f"{target_duration:.3f}",
        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
        output_audio,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stage_effects(input_path: str, output_path: str) -> bool:
    """مرحله ۴: افکت‌های نهایی با pedalboard (اختیاری)"""
    if not PEDALBOARD_AVAILABLE:
        return False
    try:
        board = Pedalboard([
            NoiseGate(
                threshold_db=GATE_THRESHOLD_DB,
                ratio=GATE_RATIO,
                attack_ms=GATE_ATTACK_MS,
                release_ms=GATE_RELEASE_MS,
            ),
            Compressor(
                threshold_db=COMP_THRESHOLD_DB,
                ratio=COMP_RATIO,
                attack_ms=COMP_ATTACK_MS,
                release_ms=COMP_RELEASE_MS,
            ),
            Reverb(
                room_size=REVERB_ROOM_SIZE,
                damping=REVERB_DAMPING,
                wet_level=REVERB_WET_LEVEL,
                width=REVERB_WIDTH,
            ),
            Limiter(
                threshold_db=LIMITER_THRESHOLD_DB,
                release_ms=LIMITER_RELEASE_MS,
            ),
        ])

        with AudioFile(input_path) as f:
            audio = f.read(f.frames)
            sr = f.samplerate

        effected = board.process(audio, sr)

        with AudioFile(output_path, "w", sr) as f:
            f.write(effected)

        return True
    except Exception as e:
        logging.error(f"خطا در اعمال افکت‌ها: {e}")
        return False


def enhance_audio_pipeline(raw_audio: str, final_audio: str, target_duration: float, idx: int) -> None:
    """
    Pipeline کامل روی یک فایل صوتی:
      raw_audio → [denoise] → [dereverb] → [speed change] → [effects] → final_audio
    طول نهایی دقیقاً برابر target_duration است.
    """
    current = raw_audio
    temp_files_to_cleanup = []

    # ۱. حذف نویز
    p1 = os.path.join(TEMP_DIR, f"tmp_{idx}_denoise.wav")
    if stage_denoise(current, p1):
        current = p1
        temp_files_to_cleanup.append(p1)
    else:
        logging.info(f"[خط {idx}] حذف نویز رد شد.")

    # ۲. حذف ورب
    p2 = os.path.join(TEMP_DIR, f"tmp_{idx}_dereverb.wav")
    if stage_dereverb(current, p2):
        current = p2
        temp_files_to_cleanup.append(p2)
    else:
        logging.info(f"[خط {idx}] حذف ورب رد شد.")

    # ۳. تغییر سرعت (خروجی: exact target_duration، 44100 stereo)
    p3 = os.path.join(TEMP_DIR, f"tmp_{idx}_speed.wav")
    stage_speed_change(current, p3, target_duration)
    current = p3
    temp_files_to_cleanup.append(p3)

    # ۴. افکت‌های نهایی
    p4 = os.path.join(TEMP_DIR, f"tmp_{idx}_effects.wav")
    if stage_effects(current, p4):
        current = p4
        temp_files_to_cleanup.append(p4)
    else:
        logging.info(f"[خط {idx}] افکت‌ها رد شدند.")

    # کپی نهایی
    shutil.copy(current, final_audio)

    # پاکسازی فایل‌های میانی
    for f in temp_files_to_cleanup:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


# ==========================================================
# 🎥 پردازش اصلی رندر (Background)
# ==========================================================
def background_dubbing_process():
    if not render_lock.acquire(blocking=False):
        logging.warning("یک رندر دیگر فعال است. درخواست لغو شد.")
        return

    start_time = time.time()
    msg_id = send_telegram_message("⏳ <b>شروع پروسه رندر ویدیو...</b>\nدر حال خواندن فایل‌ها...")

    try:
        video_path  = os.path.join(TEMP_DIR, "original_video.mp4")
        sub_path    = os.path.join(TEMP_DIR, "subtitles.json")
        output_path = os.path.join(TEMP_DIR, "final_dubbed_video.mp4")

        if not os.path.exists(video_path) or not os.path.exists(sub_path):
            raise Exception("فایل ویدیو یا زیرنویس اصلی در TEMP_DIR پیدا نشد.")

        with open(sub_path, "r", encoding="utf-8") as f:
            subtitles = json.load(f)

        total_lines = len(subtitles)

        # ---------- مرحله ۱: پردازش خط به خط ----------
        logging.info("مرحله ۱: پردازش کامل هر خط صوتی")
        audio_segments = []

        for i, sub in enumerate(subtitles):
            raw_audio = os.path.join(TEMP_DIR, f"audio_line_{i}.wav")
            if not os.path.exists(raw_audio):
                logging.warning(f"[خط {i}] فایل صوتی یافت نشد — رد شد.")
                continue

            tc = sub.get("time_code") if isinstance(sub, dict) else sub[0]
            try:
                start, end = parse_time_code(tc)
            except Exception as e:
                logging.warning(f"[خط {i}] time_code نامعتبر ({tc}) — رد شد: {e}")
                continue

            target_dur = end - start
            if target_dur <= 0.1:
                logging.warning(f"[خط {i}] مدت زمان خیلی کوتاه ({target_dur}s) — رد شد.")
                continue

            proc_audio = os.path.join(TEMP_DIR, f"processed_{i}.wav")

            try:
                enhance_audio_pipeline(raw_audio, proc_audio, target_dur, i)
            except Exception as e:
                logging.error(f"[خط {i}] خطا در pipeline: {e} — استفاده از تنظیم سرعت ساده.")
                try:
                    stage_speed_change(raw_audio, proc_audio, target_dur)
                except Exception as e2:
                    logging.error(f"[خط {i}] حتی fallback هم شکست خورد: {e2}")
                    continue

            audio_segments.append({"start": start, "file": proc_audio})

            # پیشرفت هر ۵ خط
            if (i + 1) % 5 == 0 or (i + 1) == total_lines:
                elapsed = round(time.time() - start_time, 1)
                pct = int(30 * (i + 1) / total_lines)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                update_telegram_message(
                    msg_id,
                    f"🎙 <b>پردازش صدا: {i+1}/{total_lines}</b>\n"
                    f"⏱ {elapsed}s\n[{bar}] {pct}%"
                )

        if not audio_segments:
            raise Exception("هیچ فایل صوتی پردازش‌شده‌ای برای میکس پیدا نشد.")

        # ---------- مرحله ۲: میکس نهایی ----------
        elapsed = round(time.time() - start_time, 1)
        update_telegram_message(
            msg_id,
            f"🎬 <b>میکس نهایی روی ویدیو (FFmpeg)...</b>\n"
            f"⏱ {elapsed}s\n[██████░░░░] 60%"
        )
        logging.info("مرحله ۲: فرمان FFmpeg filter_complex")

        inputs = ["-i", video_path]
        filter_parts = []

        for i, item in enumerate(audio_segments):
            inputs += ["-i", item["file"]]
            delay_ms = int(round(item["start"] * 1000))
            filter_parts.append(
                f"[{i+1}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
                f"adelay={delay_ms}:all=1[a{i}]"
            )

        mix_inputs = "".join(f"[a{i}]" for i in range(len(audio_segments)))
        filter_parts.append(
            f"{mix_inputs}amix=inputs={len(audio_segments)}:duration=longest:normalize=0,"
            f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}[aout]"
        )
        filter_complex = ";".join(filter_parts)

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_path,
        ]
        subprocess.run(cmd, check=True)

        # ---------- مرحله ۳: ارسال ----------
        total_time = round(time.time() - start_time, 1)
        update_telegram_message(
            msg_id,
            f"✨ <b>رندر کامل شد!</b>\n⏱ زمان کل: {total_time} ثانیه\nدر حال ارسال فایل..."
        )
        caption = f"✨ <b>ویدیوی دوبله‌شده آماده شد!</b>\n⏱ زمان ساخت: {total_time} ثانیه"
        send_telegram_video(output_path, caption)

    except Exception as e:
        total_time = round(time.time() - start_time, 1)
        logging.error(f"❌ خطا در رندر: {e}")
        update_telegram_message(
            msg_id,
            f"❌ <b>خطا در پردازش ویدیو!</b>\n"
            f"<code>{str(e)}</code>\n⏱ {total_time}s"
        )
    finally:
        render_lock.release()


# ==========================================================
# 🌐 Endpoints
# ==========================================================
@app.get("/")
def health_check():
    return {
        "status": "ok",
        "message": "Dubbing Pipeline Server is Ready",
        "features": {
            "noisereduce": True,
            "nara_wpe": NARA_WPE_AVAILABLE,
            "pedalboard": PEDALBOARD_AVAILABLE,
        },
    }


@app.post("/get-audio-for-gemini")
def get_audio_for_gemini(data: dict):
    """n8n: دانلود ویدیو و استخراج WAV برای آنالیز Gemini"""
    video_url = data.get("video_url")
    if not video_url:
        raise HTTPException(status_code=400, detail="video_url ارسال نشده است.")

    temp_video_path = os.path.join(TEMP_DIR, "original_video.mp4")
    final_wav_path  = os.path.join(TEMP_DIR, "audio.wav")

    if os.path.exists(temp_video_path):
        os.remove(temp_video_path)
    if os.path.exists(final_wav_path):
        os.remove(final_wav_path)

    try:
        download_media(video_url, temp_video_path)

        cmd = [
            "ffmpeg", "-y", "-i", temp_video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            final_wav_path,
        ]
        subprocess.run(cmd, check=True)

        return FileResponse(path=final_wav_path, filename="audio.wav", media_type="audio/wav")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/init-project")
def init_project(data: InitProjectRequest):
    """n8n: ذخیره زیرنویس‌ها و اطمینان از وجود ویدیو"""
    try:
        video_path = os.path.join(TEMP_DIR, "original_video.mp4")
        if not os.path.exists(video_path):
            download_media(data.video_url, video_path)

        sub_path = os.path.join(TEMP_DIR, "subtitles.json")
        with open(sub_path, "w", encoding="utf-8") as f:
            json.dump(data.subtitles, f, ensure_ascii=False)

        return {"status": "success", "message": "اطلاعات ذخیره شدند."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-audio")
async def upload_audio(line_index: int = Form(...), file: UploadFile = File(...)):
    """n8n: دریافت فایل WAV هر خط"""
    try:
        audio_path = os.path.join(TEMP_DIR, f"audio_line_{line_index}.wav")
        with open(audio_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return {"status": "success", "message": f"خط {line_index} ذخیره شد."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process-dubbing")
def process_dubbing(background_tasks: BackgroundTasks):
    """n8n: شروع رندر نهایی"""
    background_tasks.add_task(background_dubbing_process)
    return {
        "status": "started",
        "message": "پروسه ادیت آغاز شد. نتیجه در تلگرام ارسال می‌شود.",
    }
