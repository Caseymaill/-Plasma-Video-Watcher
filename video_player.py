from flask import Flask, Response, request, redirect
import cv2
import yt_dlp
import threading
import time
import os
import re
import glob
import logging
import pygame

app = Flask(__name__)

# Keep terminal clean from constant /frame request logs
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

pygame.mixer.init()

# Default video on startup
current_url = "https://www.youtube.com/watch?v=E_nnWOwc-EI"
url_changed = threading.Event()

latest_jpeg = None
lock = threading.Lock()
TEMP_VIDEO = "temp_video.mp4"

def parse_vtt_time(t_str):
    parts = t_str.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return 0.0

def load_subtitles():
    subs = []
    vtt_files = glob.glob("temp_sub*.vtt")
    if not vtt_files:
        return subs

    # Grab the first file from the list using [0]
    with open(vtt_files[0], "r", encoding="utf-8") as f:
        content = f.read()

    # Match VTT timestamp blocks
    blocks = re.findall(
        r"((?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)",
        content,
        re.DOTALL
    )
    last_text = ""
    for start_t, end_t, text in blocks:
        clean_text = re.sub(r"<[^>]+>", "", text).replace("\n", " ").strip()
        if clean_text and clean_text != last_text:
            subs.append((parse_vtt_time(start_t), parse_vtt_time(end_t), clean_text))
            last_text = clean_text
    return subs

def get_caption_at(subs, current_sec):
    for start_t, end_t, text in reversed(subs):
        if start_t <= current_sec <= end_t:
            return text
    return ""

def draw_caption(frame, text):
    if not text:
        return frame
    h, w, _ = frame.shape

    words = text.split()
    lines, line = [], ""
    for word in words:
        if len(line + " " + word) <= 48:
            line = (line + " " + word).strip()
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)

    lines = lines[-2:]
    box_height = 28 * len(lines) + 12

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, h - box_height), (w, h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    for i, l in enumerate(lines):
        y = h - box_height + 24 + (i * 26)
        cv2.putText(frame, l, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 2, cv2.LINE_AA)
    return frame

def download_media(yt_url):
    pygame.mixer.music.stop()
    try:
        pygame.mixer.music.unload()
    except Exception:
        pass

    # Clean up old temporary files
    for f in glob.glob("temp_video*") + glob.glob("temp_audio*") + glob.glob("temp_sub*"):
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass

    # 1. Download video + English subtitles
    ydl_video_opts = {
        "js_runtimes": {"node": {}},
        "format": "best[height<=480][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": {
            "default": TEMP_VIDEO,
            "subtitle": "temp_sub",
        },
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en.*", "en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_video_opts) as ydl:
        ydl.download([yt_url])

    # 2. Download WebM/Opus audio directly (plays in pygame-ce without needing ffmpeg.exe!)
    ydl_audio_opts = {
        "js_runtimes": {"node": {}},
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": "temp_audio.%(ext)s",
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_audio_opts) as ydl:
            ydl.download([yt_url])
    except Exception as e:
        print(f"[Audio Note] Could not download audio track ({e})")

def video_worker():
    global latest_jpeg, current_url

    while True:
        url_changed.clear()
        print(f"\n[Downloading] {current_url} ...")
        try:
            download_media(current_url)
        except Exception as e:
            print(f"[Error] Could not load that link: {e}")
            url_changed.wait()
            continue

        subs = load_subtitles()
        print(f"[Loaded] {len(subs)} caption lines found.")

        cap = cv2.VideoCapture(TEMP_VIDEO)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_delay = 1.0 / fps

        audio_files = glob.glob("temp_audio.*")
        has_audio = False
        if audio_files:
            try:
                pygame.mixer.music.load(audio_files[0])
                pygame.mixer.music.play()
                has_audio = True
                print(f"[Audio] Playing synced audio ({audio_files[0]})!")
            except Exception as e:
                print(f"[Audio Note] Pygame could not play {audio_files[0]}: {e}")

        start_clock = time.time()

        while not url_changed.is_set():
            elapsed_video = time.time() - start_clock
            target_frame = int(elapsed_video * fps)
            current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))

            if target_frame > current_frame + 2:
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

            success, frame = cap.read()
            if not success:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                if has_audio:
                    pygame.mixer.music.play()
                start_clock = time.time()
                continue

            frame = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
            caption = get_caption_at(subs, elapsed_video)
            frame = draw_caption(frame, caption)

            _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 55])
            with lock:
                latest_jpeg = buffer.tobytes()

            time.sleep(max(0.005, frame_delay / 2))

        pygame.mixer.music.stop()
        cap.release()

# Web Remote Control Page
@app.route("/", methods=["GET", "POST"])
def remote():
    global current_url
    if request.method == "POST":
        new_link = request.form.get("url", "").strip()
        if new_link:
            current_url = new_link
            url_changed.set()
        return redirect("/")

    return f"""
    <html>
      <body style="font-family: sans-serif; background: #18181b; color: white; padding: 40px;">
        <h2>Plasma Video Remote (Audio + Captions)</h2>
        <p>Now playing: <b>{current_url}</b></p>
        <form method="POST">
          <input type="text" name="url" placeholder="Paste YouTube URL here..." style="width: 400px; padding: 10px; font-size: 16px;" required>
          <button type="submit" style="padding: 10px 20px; font-size: 16px; cursor: pointer;">Load Video</button>
        </form>
      </body>
    </html>
    """

@app.route("/frame")
def get_frame():
    with lock:
        data = latest_jpeg
    if data is None:
        return "Loading...", 503
    return Response(data, mimetype="image/jpeg")

if __name__ == "__main__":
    threading.Thread(target=video_worker, daemon=True).start()
    print("Open http://localhost:8080 in your browser to change videos!")
    app.run(host="0.0.0.0", port=8080, threaded=True)
