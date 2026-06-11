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
- Daily success limits (user/admin/global) with configurable reset window
- Telegram file-id cache for faster repeated sends
- Cookie manager in admin panel (per platform)
- Admin tools: stats, user inspect, broadcast, managers, block list, force-sub management
- Automatic cleanup for temporary downloaded files
- Automatic DB backup (local + owner delivery)
- Docker-ready deployment

## Stack
- Python 3.14.x (recommended)
- aiogram 3.x
- yt-dlp
- gallery-dl
- SQLite (aiosqlite)
- ffmpeg

## Setup (Easy)
1. Install Python 3.14.x (recommended; 3.11+ supported) and ffmpeg.
2. Copy `.env.example` to `.env` and fill values.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run:
   ```bash
   python bot.py
   ```

## Setup (Server, one-shot install)
On Debian/Ubuntu servers, install Python runtime + `ffmpeg` + Python dependencies together:
```bash
chmod +x install_server.sh
./install_server.sh
```
The installer exits with an error if detected Python is lower than `3.11`.

## Setup (Docker)
1. Create `.env` from `.env.example`.
2. Build and run:
   ```bash
   docker compose up -d --build
   ```
   Docker image uses Python 3.14 (`python:3.14-slim`).

## Environment Variables
- `BOT_TOKEN`: Telegram bot token (required)
- `ADMIN_IDS`: comma-separated Telegram user IDs for admin panel access
- `DOWNLOAD_DIR`: temporary media directory
- `DB_PATH`: SQLite DB path
- `BACKUP_DIR`: local backup directory
- `GROUP_WELCOME_PHOTO_PATH`: optional photo path for the group welcome message
- `MAX_CONCURRENT_DOWNLOADS`: global concurrent jobs
- `MAX_FILE_SIZE_MB`: max file size allowed for upload
- `YOUTUBE_MAX_DURATION_MINUTES`: max allowed YouTube duration
- `DAILY_USER_SUCCESS_LIMIT`: daily successful downloads per normal user
- `DAILY_ADMIN_SUCCESS_LIMIT`: daily successful downloads per normal admin
- `DAILY_GLOBAL_SUCCESS_LIMIT`: daily successful downloads for whole bot
- `DAILY_LIMIT_RESET_TIME`: daily reset time (`HH:MM`)
- `DAILY_LIMIT_RESET_TZ`: timezone name for daily reset (e.g. `Asia/Tehran`)
- `DAILY_OWNER_UNLIMITED`: owner unlimited daily quota (`1/0`)
- `COOKIE_ALERT_ENABLED`: enable cookie-expiry style alerts (`1/0`)
- `COOKIE_ALERT_THRESHOLD`: failures threshold inside window to trigger alert
- `COOKIE_ALERT_WINDOW_MINUTES`: alert counting window (minutes)
- `COOKIE_ALERT_COOLDOWN_MINUTES`: minimum minutes between repeated alerts
- `BACKUP_ENABLED`: enable automatic backups (`1/0`)
- `BACKUP_DAILY_TIME`: backup time (`HH:MM`)
- `BACKUP_TZ`: timezone name for backup scheduling
- `BACKUP_KEEP_COUNT`: maximum local backup files to keep
- `REQUEST_TTL_SECONDS`: TTL for URL action buttons
- `USER_COOLDOWN_SECONDS`: cooldown between user jobs
- `FFMPEG_BINARY`: ffmpeg executable path/name
- `YTDLP_JS_RUNTIMES`: JS runtimes for yt-dlp (YouTube challenge solving), e.g. `node`
- `YTDLP_REMOTE_COMPONENTS`: remote components for yt-dlp, e.g. `ejs:github`
- `PROBE_WORKER_THREADS`: metadata/probe worker threads
- `DOWNLOAD_WORKER_THREADS`: download worker threads
- `DOWNLOAD_TIMEOUT_SECONDS`: timeout per download job
- `LOG_LEVEL`: e.g. `INFO`, `DEBUG`

## Commands
- `/start` start + register user
- `/lang` change bot language
- Admin panel is shown to admins after `/start`

## Notes
- This bot supports: YouTube, Instagram, TikTok, Twitter/X, SoundCloud.
- Instagram restricted/private content may require valid cookies.
- Cookies are managed from the bot admin panel per platform.
- Group mode requirements:
  - Disable BotFather privacy mode (`/setprivacy` -> `Disable`) so the bot can read normal group messages/links.
  - Ensure the bot can send messages/media in target groups.
- Respect platform Terms of Service and copyright law in your jurisdiction.
