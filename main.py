def download_media_with_ytdlp(url: str, output_path: str, is_audio_only: bool = False):
    ydl_opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        'geo_bypass': True,
        # تنظیم کلاینت مجاز برای رفع باگ "The page needs to be reloaded" هنگام استفاده از کوکی
        'extractor_args': {
            'youtube': {
                'player_client': ['web_safari', 'web_embedded', '-tv_downgraded']
            }
        }
    }

    # خواندن فایل کوکی
    cookie_file = None
    if os.path.exists("cookies.txt"):
        cookie_file = "cookies.txt"
    elif os.path.exists("cookie.txt"):
        cookie_file = "cookie.txt"

    if cookie_file:
        print(f"--- [DEBUG] Using cookie file: {cookie_file} ---")
        ydl_opts['cookiefile'] = cookie_file
    else:
        print("--- [WARNING] No cookie file found! ---")

    if is_audio_only:
        ydl_opts.update({
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
