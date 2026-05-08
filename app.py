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

# 다운로드 제한 시간과 yt-dlp 재시도 정책은 운영 환경에서 환경변수로 조정할 수 있다.
DEFAULT_DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
YTDLP_RETRIES = os.environ.get("YTDLP_RETRIES", "50")
YTDLP_FRAGMENT_RETRIES = os.environ.get("YTDLP_FRAGMENT_RETRIES", "50")
YTDLP_EXTRACTOR_RETRIES = os.environ.get("YTDLP_EXTRACTOR_RETRIES", "10")
YTDLP_FILE_ACCESS_RETRIES = os.environ.get("YTDLP_FILE_ACCESS_RETRIES", "10")
YTDLP_SOCKET_TIMEOUT = os.environ.get("YTDLP_SOCKET_TIMEOUT", "30")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 간단한 self-hosted 앱이라 작업 상태는 메모리에만 보관한다.
# 컨테이너/서버가 재시작되면 진행 중인 작업 정보는 사라진다.
jobs = {}


class CleanupFileResponse:
    # send_file 응답이 완전히 닫힌 뒤 임시 파일을 삭제하기 위한 응답 래퍼.
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
    # 프론트에서 전달한 timeout_mode를 subprocess timeout 값으로 변환한다.
    # None은 Python subprocess에서 "타임아웃 없음"을 의미한다.
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
    # yt-dlp 에러는 길 수 있으므로 사용자에게 보여줄 마지막 몇 줄만 남긴다.
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-5:]) if lines else "Download failed"


def parse_yt_dlp_progress(line):
    # --newline 옵션으로 한 줄씩 출력되는 yt-dlp 진행률에서 퍼센트/속도/ETA를 뽑는다.
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
    # yt-dlp 출력 한 줄을 현재 job 상태에 반영한다.
    # 브라우저는 /api/status/<job_id>를 폴링해 이 값을 화면에 표시한다.
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
    # 실제 다운로드는 요청 스레드를 막지 않도록 백그라운드 스레드에서 실행된다.
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    # 긴 영상에서 일시적인 HTTP 500/fragment 실패가 자주 발생하므로 재시도와 backoff를 강화한다.
    # --newline은 진행률을 줄 단위로 읽기 위해 필요하다.
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

    # MP3는 yt-dlp의 오디오 추출 기능을 쓰고, MP4는 영상+음성을 받은 뒤 ffmpeg로 병합한다.
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

        # capture_output=True를 쓰면 다운로드가 끝날 때까지 진행률을 볼 수 없다.
        # Popen으로 stdout/stderr를 스트리밍해서 job 상태와 Docker 로그에 실시간 반영한다.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        job["pid"] = process.pid

        # readline이 무기한 블로킹되지 않게 selector로 출력 가능 여부를 확인한다.
        # 그래야 출력이 없는 재시도 대기 상태에서도 timeout_seconds를 계속 검사할 수 있다.
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
                    # 에러 발생 시 원인을 보여주기 위해 최근 출력 일부만 보관한다.
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

        # yt-dlp는 병합 전 중간 파일을 만들 수 있으므로 job_id로 시작하는 결과 파일을 찾는다.
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

        # 브라우저에 내려줄 최종 파일만 남기고 중간 파일은 삭제한다.
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

        # OS별 파일명 금지 문자를 제거해 브라우저 다운로드 파일명으로 사용한다.
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
    # 단일 HTML 템플릿 기반 UI를 렌더링한다.
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    # 다운로드 전에 yt-dlp 메타데이터를 조회해 제목/썸네일/길이/품질 목록을 만든다.
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

        # 같은 해상도 후보가 여러 개면 bitrate(tbr)가 가장 높은 포맷만 품질 옵션으로 보여준다.
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
    # 다운로드 요청은 즉시 job_id를 반환하고, 실제 작업은 별도 스레드에서 진행한다.
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # 사용자가 선택한 타임아웃 옵션을 검증한다.
    try:
        timeout_seconds = resolve_download_timeout(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex[:10]

    # 프론트가 폴링할 초기 job 상태를 만든다.
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
    # 브라우저가 주기적으로 호출해 진행률/완료/실패 상태를 확인한다.
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
    # 다운로드가 끝난 파일을 브라우저에 attachment로 전송한다.
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    file_path = job["file"]
    response = send_file(file_path, as_attachment=True, download_name=job["filename"])

    def cleanup_download():
        # 브라우저로 파일 응답이 닫힌 뒤 서버에 남은 임시 파일과 job 상태를 정리한다.
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
    # Docker에서는 HOST=0.0.0.0, 로컬 실행에서는 기본 127.0.0.1을 사용한다.
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
