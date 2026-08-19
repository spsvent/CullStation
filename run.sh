#!/usr/bin/env bash
# Launch CullStation. Uses a local venv so it works on Homebrew Python (PEP 668).
# The app keeps the Mac awake only while actually encoding (VideoWorker + caffeinate).
set -e
cd "$(dirname "$0")"

# Mac defaults (override via env). On the Linux build box these are set elsewhere.
if [ "$(uname)" = "Darwin" ]; then
  export CULL_SOURCE="${CULL_SOURCE:-/Volumes/Marketing Media}"
  export CULL_ARCHIVE="${CULL_ARCHIVE:-/Volumes/Marketing Media/Marketing Archive}"
  export CULL_VIDEO_ENCODER="${CULL_VIDEO_ENCODER:-hevc_videotoolbox}"
fi

BASE_PY="${PYBIN:-python3}"
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found — video needs it (brew install ffmpeg)"

# Warn early if the configured encoder isn't in this ffmpeg build (else it fails per-file later).
ENC="${CULL_VIDEO_ENCODER:-hevc_videotoolbox}"
if command -v ffmpeg >/dev/null && ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "$ENC"; then
  echo "WARNING: encoder '$ENC' not available in this ffmpeg — set CULL_VIDEO_ENCODER=libx265"
fi

# Self-contained venv (sidesteps Homebrew's externally-managed-environment).
if [ ! -d .venv ]; then
  echo "creating venv…"; "$BASE_PY" -m venv .venv
fi
PY=.venv/bin/python
"$PY" -c "import fastapi,uvicorn,PIL" 2>/dev/null || { echo "installing deps…"; "$PY" -m pip install -q -r requirements.txt; }

exec "$PY" app.py
