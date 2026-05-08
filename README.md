# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites - download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads - paste multiple URLs at once
- Automatic URL deduplication
- Download timeout controls: default 5 minutes, custom minutes, or no timeout
- Live download progress with percent, speed, ETA, and retry/status messages
- More resilient long-video downloads with yt-dlp retry/backoff settings
- Temporary server files are removed after the browser download response completes
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend

## Quick Start

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone git@github.com:yjunsu75/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

Or with Docker Compose:

```bash
docker compose up -d --build
```

Open **http://localhost:8800** when using the included Compose file.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Choose a timeout mode if the video may take a long time
6. Click **Download** on individual videos, or **Download All**
7. Watch progress, speed, ETA, and retry/status messages on each card

## Long Downloads

ReClip starts downloads as background jobs and polls their status from the browser. For long videos, use **Custom minutes** or **No timeout** from the timeout selector before starting the download.

The backend also passes retry/backoff options to `yt-dlp` so temporary upstream HTTP errors, failed fragments, and slow responses have a better chance of recovering. Progress is streamed from `yt-dlp` and exposed through `/api/status/<job_id>`.

Downloaded files are stored temporarily in `downloads/` while the browser download is prepared. After `/api/file/<job_id>` finishes sending the file, ReClip removes the temporary file and clears the in-memory job entry.

Several retry-related settings can be tuned with environment variables:

```bash
DOWNLOAD_TIMEOUT=300
YTDLP_RETRIES=50
YTDLP_FRAGMENT_RETRIES=50
YTDLP_EXTRACTOR_RETRIES=10
YTDLP_FILE_ACCESS_RETRIES=10
YTDLP_SOCKET_TIMEOUT=30
```

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
