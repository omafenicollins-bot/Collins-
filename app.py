import os
import re
import uuid
import json
import time
import shutil
import threading
import subprocess
import requests
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
HISTORY_FILE = Path(__file__).parent / "history.json"

PLATFORM_RESOLUTIONS = {
    "youtube_shorts": (720, 1280),
    "tiktok":         (720, 1280),
    "instagram_reels":(720, 1280),
    "youtube":        (1280, 720),
}

TOPIC_KEYWORD_MAP = [
    ("mamba",      "snake"),
    ("snake",      "snake"),
    ("lion",       "lion africa"),
    ("elephant",   "elephant wildlife"),
    ("shark",      "shark ocean"),
    ("eagle",      "eagle bird"),
    ("tiger",      "tiger wildlife"),
    ("crocodile",  "crocodile"),
    ("gorilla",    "gorilla wildlife"),
    ("wolf",       "wolf wildlife"),
    ("motivation", "city night"),
    ("success",    "city night"),
    ("discipline", "gym training"),
    ("hustle",     "city night"),
]
PIXABAY_FALLBACKS = ["wildlife", "nature", "animal", "city"]

IMPORTANT_WORDS = {
    "incredible","amazing","deadly","powerful","dangerous","fastest",
    "shocking","massive","ancient","terrifying","beautiful","stunning",
    "never","always","every","must","first","last","best","worst",
    "real","truth","secret","vital","critical","death","kill","alive",
    "win","lose","fear","brave","strong","weak","dark","light",
}

def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []
    except Exception:
        return []

def save_history(entries):
    HISTORY_FILE.write_text(json.dumps(entries, indent=2))

def append_history(entry):
    entries = load_history()
    entries.insert(0, entry)
    save_history(entries[:50])

def job_dir(job_id):
    return OUTPUTS_DIR / job_id

def job_status_path(job_id):
    return job_dir(job_id) / "status.json"

def load_job(job_id):
    p = job_status_path(job_id)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None

def save_job(job_id, data):
    d = job_dir(job_id)
    d.mkdir(exist_ok=True)
    job_status_path(job_id).write_text(json.dumps(data, indent=2))

def update_job(job_id, **fields):
    data = load_job(job_id) or {}
    data.update(fields)
    save_job(job_id, data)

STYLE_PROMPTS = {
    "nature": "You are a nature documentary narrator. Rewrite this script to be vivid and exciting. Maximum 80 words. Return ONLY the script.",
    "news":   "You are a news anchor. Rewrite this script to be clear and informative. Maximum 80 words. Return ONLY the script.",
}

def call_openrouter(prompt, max_tokens=200):
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://visionai.app",
            "X-Title": "VisionAI",
        },
        json={
            "model": "meta-llama/llama-3.1-8b-instruct:free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()

def rewrite_script(topic, script, style):
    prompt = f"{STYLE_PROMPTS.get(style, STYLE_PROMPTS['nature'])}\n\nTopic: {topic}\nScript: {script}"
    try:
        return call_openrouter(prompt, 200)
    except Exception:
        return script

def extract_keyword(topic):
    topic_lower = topic.lower()
    for substring, keyword in TOPIC_KEYWORD_MAP:
        if substring in topic_lower:
            return keyword
    try:
        return call_openrouter(
            f"Extract 1-2 keywords for stock video search from: \"{topic}\"\n"
            "Return ONLY the keywords, nothing else.", 10
        ).strip().strip('"').strip("'")
    except Exception:
        return topic.split()[0]

def pixabay_search(query, count=2):
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query,
                    "video_type": "film", "per_page": 8,
                    "safesearch": "true"},
            timeout=20,
        )
        r.raise_for_status()
        urls = []
        for hit in r.json().get("hits", []):
            videos = hit.get("videos", {})
            for tier_name in ["small", "medium", "large"]:
                tier = videos.get(tier_name, {})
                if isinstance(tier, dict) and tier.get("url"):
                    urls.append(tier["url"])
                    break
            if len(urls) >= count:
                break
        return urls
    except Exception:
        return []

def fetch_videos(topic, style, count=2):
    keyword = extract_keyword(topic)
    urls = pixabay_search(keyword, count)
    if not urls:
        for fallback in PIXABAY_FALLBACKS:
            urls = pixabay_search(fallback, count)
            if urls:
                break
    return urls

def download_video(url, dest):
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception:
        return False
def generate_voiceover(text, output_path):
    from gtts import gTTS
    import time
    for attempt in range(3):
        try:
            time.sleep(5)
            tts = gTTS(text=text, lang="en", slow=False)
            tts.save(output_path)
            return
        except Exception as e:
            if attempt == 2:
                raise e
            time.sleep(10)


def get_audio_duration(audio_path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return 30.0

def should_highlight(word, index):
    clean = re.sub(r'[^a-z]', '', word.lower())
    return (index % 3 == 0) or (clean in IMPORTANT_WORDS)

def build_drawtext(script_text, audio_duration, target_w, target_h):
    words = script_text.split()
    if not words:
        return "null"
    dur_per_word = audio_duration / len(words)
    font_size = 48
    y_pos = int(target_h * 0.75)
    filters = []
    for i, word in enumerate(words):
        start = i * dur_per_word
        end = start + dur_per_word
        color = "yellow" if should_highlight(word, i) else "white"
        safe_word = re.sub(r"['\:\\]", "", word)
        if safe_word:
            filters.append(
                f"drawtext=text='{safe_word}':fontsize={font_size}:fontcolor={color}:"
                f"borderw=3:bordercolor=black:x=(w-text_w)/2:y={y_pos}:"
                f"enable='between(t,{start:.2f},{end:.2f})'"
            )
    return ",".join(filters) if filters else "null"

def compile_video_ffmpeg(video_clips, audio_path, output_path, script_text, platform):
    target_w, target_h = PLATFORM_RESOLUTIONS.get(platform, (720, 1280))
    audio_duration = get_audio_duration(audio_path)
    job_tmp = Path(output_path).parent / "tmp"
    job_tmp.mkdir(exist_ok=True)

    clip_path = video_clips[0]
    base_video = str(job_tmp / "base.mp4")

    result1 = subprocess.run([
        "ffmpeg", "-y", "-i", clip_path,
        "-t", str(audio_duration),
        "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
               f"crop={target_w}:{target_h}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
        "-an", "-threads", "1", base_video
    ], capture_output=True, text=True)

    if not Path(base_video).exists():
        raise ValueError(f"FFmpeg step 1 failed: {result1.stderr[-300:]}")

    drawtext = build_drawtext(script_text, audio_duration, target_w, target_h)

    result2 = subprocess.run([
        "ffmpeg", "-y",
        "-i", base_video,
        "-i", audio_path,
        "-vf", drawtext,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
        "-c:a", "aac", "-shortest", "-threads", "1",
        output_path
    ], capture_output=True, text=True)

    if not Path(output_path).exists():
        raise ValueError(f"FFmpeg step 2 failed: {result2.stderr[-300:]}")

    shutil.rmtree(job_tmp, ignore_errors=True)

def _run_job(job_id, topic, script, style, platform):
    jdir = job_dir(job_id)
    jdir.mkdir(exist_ok=True)

    def step(label, progress):
        update_job(job_id, step=label, progress=progress)

    try:
        step("Rewriting script with AI...", 10)
        rewritten = rewrite_script(topic, script, style)

        step("Generating voiceover...", 25)
        audio_path = str(jdir / "voiceover.mp3")
        generate_voiceover(rewritten, audio_path)

        step("Searching Pixabay for footage...", 40)
        video_urls = fetch_videos(topic, style, count=2)
        if not video_urls:
            update_job(job_id, status="error",
                       error="No video footage found. Try a broader topic.")
            return

        step("Downloading video clip...", 55)
        downloaded = []
        for i, url in enumerate(video_urls):
            clip_path = str(jdir / f"clip_{i}.mp4")
            if download_video(url, clip_path):
                downloaded.append(clip_path)
                break
        if not downloaded:
            update_job(job_id, status="error", error="Could not download footage.")
            return

        step("Compiling video...", 70)
        output_path = str(jdir / "output.mp4")
        compile_video_ffmpeg(downloaded, audio_path, output_path, rewritten, platform)

        entry = {
            "job_id": job_id,
            "topic": topic,
            "style": style,
            "platform": platform,
            "rewritten_script": rewritten,
            "download_url": f"/download/{job_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        append_history(entry)

        update_job(job_id, status="done", step="Video ready!", progress=100,
                   rewritten_script=rewritten,
                   download_url=f"/download/{job_id}",
                   finished_at=datetime.now(timezone.utc).isoformat())

    except Exception as e:
        update_job(job_id, status="error", error=str(e))

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    topic    = (data.get("topic") or "").strip()
    script   = (data.get("script") or "").strip()
    style    = data.get("style", "nature")
    platform = data.get("platform", "youtube_shorts")

    if platform not in PLATFORM_RESOLUTIONS:
        platform = "youtube_shorts"
    if not topic or not script:
        return jsonify({"error": "Topic and script are required."}), 400

    job_id = uuid.uuid4().hex[:8]
    save_job(job_id, {
        "job_id": job_id, "status": "processing",
        "step": "Starting...", "progress": 5,
        "topic": topic, "style": style, "platform": platform,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "error": None, "download_url": None,
    })
    threading.Thread(target=_run_job,
                     args=(job_id, topic, script, style, platform),
                     daemon=True).start()
    return jsonify({"job_id": job_id, "status": "processing"}), 202

@app.route("/status/<job_id>")
def status(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/download/<job_id>")
def download(job_id):
    video_path = job_dir(job_id) / "output.mp4"
    if not video_path.exists():
        return jsonify({"error": "Video not found"}), 404
    return send_file(str(video_path), as_attachment=True,
                     download_name="visionai_video.mp4")

@app.route("/history")
def history():
    entries = load_history()
    enriched = []
    for e in entries:
        e["available"] = (job_dir(e["job_id"]) / "output.mp4").exists()
        enriched.append(e)
    return jsonify(enriched)

@app.route("/history/<job_id>", methods=["DELETE"])
def delete_history(job_id):
    entries = [e for e in load_history() if e["job_id"] != job_id]
    save_history(entries)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
