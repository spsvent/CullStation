#!/usr/bin/env bash
# Fetch a STATIC arm64 ffmpeg + ffprobe (with hevc_videotoolbox) into ./vendor/ for bundling.
#
# Source: osxexperts.net — the only one shipping native Apple-Silicon STATIC builds (evermeet is
# Intel-only; Homebrew is dynamically linked and not relocatable). Filenames/versions roll forward,
# so override the URLs via env if they 404. Always re-verify arch + codec after download (we do below).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p vendor && cd vendor

# Match the build host's architecture so the bundled ffmpeg runs inside the .app
# (arm64 app needs arm64 ffmpeg; an x86_64 app built on an Intel Mac needs the Intel build).
WANT_ARCH="$(uname -m)"
if [ "$WANT_ARCH" = "x86_64" ]; then
  FFMPEG_URL="${FFMPEG_URL:-https://www.osxexperts.net/ffmpeg80intel.zip}"
  FFPROBE_URL="${FFPROBE_URL:-https://www.osxexperts.net/ffprobe80intel.zip}"
else
  FFMPEG_URL="${FFMPEG_URL:-https://www.osxexperts.net/ffmpeg81arm.zip}"
  FFPROBE_URL="${FFPROBE_URL:-https://www.osxexperts.net/ffprobe81arm.zip}"
fi
echo "build arch: $WANT_ARCH"

fetch() {  # url  outname
  local url="$1" out="$2" zip="${2}.zip"
  echo "↓ $url"
  curl -fL --retry 3 -o "$zip" "$url"
  rm -f "$out"
  ( bsdtar -xf "$zip" 2>/dev/null || unzip -oq "$zip" )
  if [ ! -f "$out" ]; then               # zip may hold a differently-named binary
    local inner; inner="$(bsdtar -tf "$zip" 2>/dev/null | grep -vi '/$' | head -1 || true)"
    [ -n "$inner" ] && [ -f "$inner" ] && mv "$inner" "$out"
  fi
  chmod +x "$out"; rm -f "$zip"
}

fetch "$FFMPEG_URL"  ffmpeg
fetch "$FFPROBE_URL" ffprobe

ok=1
for b in ffmpeg ffprobe; do
  xattr -dr com.apple.quarantine "$b" 2>/dev/null || true
  echo "── $b ──"
  echo "  arch:   $(lipo -archs "$b" 2>/dev/null || echo '?')"
  echo "  sha256: $(shasum -a 256 "$b" | awk '{print $1}')"
  lipo -archs "$b" 2>/dev/null | grep -qw "$WANT_ARCH" || { echo "  ✗ NOT $WANT_ARCH — wrong build for this Mac"; ok=0; }
  if otool -L "$b" | tail -n +2 | grep -vqE '/usr/lib/|/System/Library/'; then
    echo "  ⚠ links NON-system dylibs (not safe to bundle as-is):"
    otool -L "$b" | tail -n +2 | grep -vE '/usr/lib/|/System/Library/'
    ok=0
  else
    echo "  ✓ links only system libraries (relocatable / safe to bundle)"
  fi
done

# the whole point of HW transcode — must have hevc_videotoolbox
if ./ffmpeg -hide_banner -encoders 2>/dev/null | grep -q hevc_videotoolbox; then
  echo "✓ hevc_videotoolbox present"
else
  echo "✗ hevc_videotoolbox MISSING in this ffmpeg build"; ok=0
fi

[ "$ok" = 1 ] || { echo "vendored ffmpeg failed verification — fix the URLs/build before bundling"; exit 1; }
echo "vendor/ ready:"; ls -la
