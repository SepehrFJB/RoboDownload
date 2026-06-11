#!/usr/bin/env bash
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Debian/Ubuntu servers (apt-get)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg python3 python3-pip
sudo apt-get clean

python3 - <<'PY'
import sys
ver = sys.version_info
print(f"Detected Python: {ver.major}.{ver.minor}.{ver.micro}")
if (ver.major, ver.minor) < (3, 11):
    print("ERROR: Python 3.11+ is required. Please upgrade Python and rerun installer.")
    raise SystemExit(1)
PY

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

echo "Done: ffmpeg + Python runtime + Python dependencies installed successfully."
