import os
import uuid
import glob
import json
import re
import selectors
import subprocess
import threading
import time
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
DEFAULT_DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
YTDLP_RETRIES = os.environ.get("YTDLP_RETRIES", "50")
YTDLP_FRAGMENT_RETRIES = os.environ.get("YTDLP_FRAGMENT_RETRIES", "50")
YTDLP_EXTRACTOR_RETRIES = os.environ.get("YTDLP_EXTRACTOR_RETRIES", "10")
YTDLP_FILE_ACCESS_RETRIES = os.environ.get("YTDLP_FILE_ACCESS_RETRIES", "10")
YTDLP_SOCKET_TIMEOUT = os.environ.get("YTDLP_SOCKET_TIMEOUT", "30")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}


class CleanupFileResponse:
    def __init__(self, iterable, cleanup):
        self.iterable = iterable
        self.cleanup = cleanup

    def __iter__(self):
        return iter(self.iterable)

    def close(self):
        close = getattr(self.iterable, "close", None)
        if close:
            close()
        self.cleanup()


def resolve_download_timeout(data):
    mode = data.get("timeout_mode", "default")
    if mode == "none":
        return None
    if mode == "default":
        return DEFAULT_DOWNLOAD_TIMEOUT
    if mode != "custom":
        raise ValueError("Invalid timeout option")

    try:
        timeout_seconds = int(data.get("timeout_seconds", 0))
    except (TypeError, ValueError):
        raise ValueError("Custom timeout must be a number")

    if timeout_seconds < 1:
        raise ValueError("Custom timeout must be at least 1 second")
    return timeout_seconds


def format_timeout(timeout_seconds):
    if timeout_seconds is None:
        return "no"
    if timeout_seconds % 60 == 0:
        minutes = timeout_seconds // 60
        return f"{minutes} min"
    return f"{timeout_seconds} sec"


def get_error_tail(stderr):
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-5:]) if lines else "Download failed"


def parse_yt_dlp_progress(line):
    progress = {"message": line}
    percent_match = re.search(r"\[download\]\s+([0-9.]+)%", line)
    if percent_match:
        progress["percent"] = float(percent_match.group(1))

    speed_match = re.search(r"\bat\s+([^\s]+)", line)
    if speed_match:
        progress["speed"] = speed_match.group(1)

    eta_match = re.search(r"\bETA\s+([^\s]+)", line)
    if eta_match:
        progress["eta"] = eta_match.group(1)

    return progress


def update_job_progress(job, line):
    line = line.strip()
    if not line:
        return

    job["last_message"] = line
    if "[download]" in line:
        job["progress"] = {**job.get("progress", {}), **parse_yt_dlp_progress(line)}
    elif line.startswith("[Merger]"):
        job["progress"] = {**job.get("progress", {}), "message": "Merging video and audio"}
    elif line.startswith("[ExtractAudio]"):
        job["progress"] = {**job.get("progress", {}), "message": "Extracting audio"}
    elif "Retrying" in line or "Got error" in line:
        job["progress"] = {**job.get("progress", {}), "message": line}


def run_download(job_id, url, format_choice, format_id, timeout_seconds):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--continue",
        "--newline",
        "--retries", YTDLP_RETRIES,
        "--fragment-retries", YTDLP_FRAGMENT_RETRIES,
        "--extractor-retries", YTDLP_EXTRACTOR_RETRIES,
        "--file-access-retries", YTDLP_FILE_ACCESS_RETRIES,
        "--socket-timeout", YTDLP_SOCKET_TIMEOUT,
        "--retry-sleep", "http:exp=1:60:2",
        "--retry-sleep", "fragment:exp=1:60:2",
        "--retry-sleep", "extractor:linear=1:10:1",
        "-o", out_template,
    ]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        started_at = time.monotonic()
        output_lines = []
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        job["pid"] = process.pid
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        while True:
            if timeout_seconds is not None and time.monotonic() - started_at > timeout_seconds:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(cmd, timeout_seconds)

            for key, _ in selector.select(timeout=0.2):
                line = key.fileobj.readline()
                if line:
                    output_lines.append(line)
                    output_lines = output_lines[-20:]
                    update_job_progress(job, line)
                    app.logger.info("yt-dlp[%s]: %s", job_id, line.strip())

            if process.poll() is not None:
                break

        selector.close()
        returncode = process.wait()
        if returncode != 0:
            job["status"] = "error"
            job["error"] = get_error_tail("".join(output_lines))
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["progress"] = {**job.get("progress", {}), "percent": 100, "message": "Ready to save"}
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = f"Download timed out ({format_timeout(timeout_seconds)} limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        timeout_seconds = resolve_download_timeout(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "timeout_seconds": timeout_seconds,
        "progress": {"percent": 0, "message": "Starting download"},
        "last_message": "Starting download",
    }

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, timeout_seconds))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress"),
        "last_message": job.get("last_message"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    file_path = job["file"]
    response = send_file(file_path, as_attachment=True, download_name=job["filename"])

    def cleanup_download():
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            app.logger.warning("Failed to remove download %s: %s", file_path, e)
        jobs.pop(job_id, None)

    response.response = CleanupFileResponse(response.response, cleanup_download)
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
