#!/usr/bin/env bash
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Debian/Ubuntu servers (apt-get)." >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "ERROR: Please run this script as root or install sudo." >&2
    exit 1
  fi
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> Updating packages and installing ffmpeg, nodejs, python3, python3-venv..."
$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends ffmpeg nodejs python3 python3-pip python3-venv
$SUDO apt-get clean

python3 - <<'PY'
import sys
ver = sys.version_info
print(f"Detected Python: {ver.major}.{ver.minor}.{ver.micro}")
if (ver.major, ver.minor) < (3, 11):
    print("ERROR: Python 3.11+ is required. Please upgrade Python and rerun installer.")
    raise SystemExit(1)
PY

echo "==> Setting up Python virtual environment (.venv)..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "==> Installing Python dependencies inside .venv..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo ""
echo "==============================================================="
echo "Done: ffmpeg + nodejs + Python .venv + all dependencies installed!"
echo ""
echo "To run the bot:"
echo "  source .venv/bin/activate && python bot.py"
echo ""
echo "Or run in background with PM2:"
echo "  pm2 start ./.venv/bin/python --name robodownload -- bot.py"
echo "==============================================================="
