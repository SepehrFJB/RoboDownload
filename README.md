# Telegram Multi-Platform Downloader Bot

Advanced Telegram bot for downloading media from multiple platforms with `yt-dlp` and `gallery-dl`.

[Try the live bot](https://t.me/RoboDownloadBot)

## Features
- YouTube video download with quality options (`1080p`, `720p`, `480p`, `360p`, `240p`)
- Audio extraction to MP3
- Instagram hybrid download:
  - videos via `yt-dlp`
  - photos/carousels via `gallery-dl`
- SoundCloud download (audio)
- TikTok download (video or MP3)
- Twitter/X download (video or MP3)
- Persian/English UI (`/lang`), default language: Persian
- Background queue + concurrent workers
- Per-user cooldown + one-active-job guard
- Daily success limits (user/admin/global)
- Telegram file-id cache for faster repeated sends
- 3-tier cookie resolution hierarchy (Personal user cookies -> Global bot cookies -> Direct request)
- User & Admin cookie management with step-by-step guide and cached tutorial image
- Admin tools: stats, user inspect, broadcast, managers, block list, force-sub management
- Rotating UTF-8 file logger (`logs/robodownload.log`)
- Automatic DB & Log backup (local retention + daily owner delivery + manual export button)
- Automatic cleanup for temporary downloaded files
- Docker-ready deployment

## Stack
- Python 3.11+ (Python 3.12 / 3.13 / 3.14 supported)
- aiogram 3.x
- yt-dlp
- gallery-dl
- SQLite (aiosqlite)
- ffmpeg

## Setup (Recommended / Standard)
1. Install Python 3.11+ and `ffmpeg`.
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```
3. Copy `.env.example` to `.env` and fill in `BOT_TOKEN` and `ADMIN_IDS`:
   ```bash
   # Windows:
   copy .env.example .env
   # Linux/macOS:
   cp .env.example .env
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Run:
   ```bash
   python bot.py
   ```

## Setup (Optional: Server One-Shot Script)
On Debian/Ubuntu servers, install Python runtime + `ffmpeg` + virtual environment + dependencies all together:
```bash
chmod +x install_server.sh
./install_server.sh
```

## Setup (Optional: Docker)
1. Create `.env` from `.env.example`.
2. Build and run in background:
   ```bash
   docker compose up -d --build
   ```
   Docker image uses Python 3.14 (`python:3.14-slim`) with `ffmpeg` and `nodejs` preinstalled.

## Environment Variables
- `BOT_TOKEN`: Telegram bot token (required)
- `ADMIN_IDS`: comma-separated Telegram user IDs for admin panel access (required)
- `GROUP_WELCOME_PHOTO_PATH`: optional photo path for the group welcome message (default: `assets/robodownloadbot.png`)
- `COOKIE_TUTORIAL_PHOTO_PATH`: optional photo path for the cookie tutorial guide (default: `assets/cookie_tutorial.jpg`)
- `MAX_CONCURRENT_DOWNLOADS`: global concurrent jobs (default: 3)
- `MAX_FILE_SIZE_MB`: max file size allowed for upload (default: 49)
- `YOUTUBE_MAX_DURATION_MINUTES`: max allowed YouTube duration (default: 30)
- `DAILY_USER_SUCCESS_LIMIT`: daily successful downloads per normal user (default: 50)
- `DAILY_ADMIN_SUCCESS_LIMIT`: daily successful downloads per normal admin (default: 50)
- `DAILY_GLOBAL_SUCCESS_LIMIT`: daily successful downloads for whole bot (default: 5000)
- `COOKIE_ALERT_ENABLED`: enable cookie-expiry style alerts (default: 1)
- `COOKIE_ALERT_THRESHOLD`: failures threshold inside window to trigger alert (default: 3)
- `COOKIE_ALERT_WINDOW_MINUTES`: alert counting window (minutes, default: 60)
- `COOKIE_ALERT_COOLDOWN_MINUTES`: minimum minutes between repeated alerts (default: 300)
- `REQUEST_TTL_SECONDS`: TTL for URL action buttons (default: 900)
- `USER_COOLDOWN_SECONDS`: cooldown between user jobs (default: 10)
- `FFMPEG_BINARY`: ffmpeg executable path/name (default: ffmpeg)
- `YTDLP_JS_RUNTIMES`: JS runtimes for yt-dlp challenge solving (default: node)
- `YTDLP_REMOTE_COMPONENTS`: remote components for yt-dlp (default: ejs:github)
- `PROBE_WORKER_THREADS`: metadata/probe worker threads (default: 4)
- `DOWNLOAD_WORKER_THREADS`: download worker threads (default: 6)
- `DOWNLOAD_TIMEOUT_SECONDS`: timeout per download job (default: 600)

## Commands
- `/start` start + register user
- `/lang` change bot language
- Admin panel is shown to admins after `/start`

## Notes
- This bot supports: YouTube, Instagram, TikTok, Twitter/X, SoundCloud.
- Instagram restricted/private content or age-restricted videos may require valid cookies.
- Cookies can be configured individually by users (via "Cookie Settings" button) or globally by admins.
- Group mode requirements:
  - Disable BotFather privacy mode (`/setprivacy` -> `Disable`) so the bot can read normal group messages/links.
  - Ensure the bot can send messages/media in target groups.
- Respect platform Terms of Service and copyright law in your jurisdiction.
