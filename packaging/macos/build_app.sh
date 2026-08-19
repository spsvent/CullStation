#!/usr/bin/env bash
# Build, sign, notarize & package CullStation.app -> CullStation-<version>.dmg
# RUN ON THE APPLE-SILICON MAC MINI — PyInstaller does not cross-compile.
#
# One-time setup (see INSTALL.md for detail):
#   xcode-select --install
#   # a "Developer ID Application" cert from your Apple Developer account must be in the login keychain
#   xcrun notarytool store-credentials "AC_CULL" \
#         --apple-id you@example.com --team-id <TEAM_ID> --password <app-specific-password>
#
# Usage:
#   ./build_app.sh                  # unsigned build (local testing only)
#   SIGN=1 DEV_ID="Developer ID Application: Justin Nunn (TEAMID)" ./build_app.sh
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${CULL_VERSION:-1.0.0}"
APP="dist/CullStation.app"
DMG="CullStation-${VERSION}.dmg"
SIGN="${SIGN:-0}"
DEV_ID="${DEV_ID:-Developer ID Application}"      # full identity incl. (TEAMID) when SIGN=1
NOTARY_PROFILE="${NOTARY_PROFILE:-AC_CULL}"
ENTITLEMENTS="$(pwd)/entitlements.plist"

ARCH="$(uname -m)"
echo "==> CullStation $VERSION   sign=$SIGN   build-arch=$ARCH"
if [ "$ARCH" != "arm64" ]; then
  echo "NOTE: building a $ARCH app — it runs on Apple-Silicon Macs (the M4 mini) under Rosetta."
  echo "      Build on an Apple-Silicon Mac for a native arm64 app."
fi

# 1) build venv with build + runtime deps
python3 -m venv .build-venv
PY=.build-venv/bin/python
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements-build.txt

# 2) static ffmpeg/ffprobe (arm64, videotoolbox)
if [ ! -f vendor/ffmpeg ] || [ ! -f vendor/ffprobe ]; then ./fetch_ffmpeg.sh; fi

# 3) PyInstaller -> dist/CullStation.app
rm -rf build dist
CULL_VERSION="$VERSION" "$PY" -m PyInstaller CullStation.spec --noconfirm --clean
[ -d "$APP" ] || { echo "BUILD FAILED: $APP not produced"; exit 1; }
echo "built $APP"

if [ "$SIGN" != "1" ]; then
  cat <<EOF
Unsigned build complete: $APP
Test it:  open "$APP"   (first launch may be Gatekeeper-blocked until signed+notarized)
To ship:  SIGN=1 DEV_ID="Developer ID Application: NAME (TEAMID)" ./build_app.sh
EOF
  exit 0
fi

# 4) codesign INSIDE-OUT (never --deep): every nested Mach-O first, the app bundle last.
#    Process substitution (not a pipe) so the loop runs in THIS shell — a failed sign aborts.
#
# --timestamp contacts Apple's timestamp service for EVERY binary, and that service
# intermittently answers "The timestamp service is not available." One hiccup across ~40
# nested Mach-Os used to abort the whole build and force a rebuild from scratch, so each
# signature gets a few tries. Timestamping is NOT optional: notarization rejects a build
# without a secure timestamp, so we retry rather than fall back to --timestamp=none.
sign_retry() {   # sign_retry <file-or-bundle> [extra codesign args…]
  local target="$1"; shift
  local tries="${SIGN_TRIES:-4}" n=1 delay=5
  while :; do
    if codesign --force --timestamp "$@" --sign "$DEV_ID" "$target"; then
      return 0
    fi
    if [ "$n" -ge "$tries" ]; then
      echo "FATAL: could not sign $target after $tries attempts" >&2
      echo "       If this says 'timestamp service is not available', check the network" >&2
      echo "       (VPN/firewall):  nc -zv timestamp.apple.com 443" >&2
      return 1
    fi
    echo "   … sign failed (attempt $n/$tries), retrying in ${delay}s: $target" >&2
    sleep "$delay"; n=$((n + 1)); delay=$((delay * 2))
  done
}

echo "==> signing nested binaries (hardened runtime + secure timestamp)"
while IFS= read -r -d '' f; do
  if file "$f" | grep -q 'Mach-O'; then
    sign_retry "$f" --options runtime || exit 1
  fi
done < <(find "$APP/Contents" -type f \( -name '*.dylib' -o -name '*.so' -o -perm -u+x \) -print0)

# The two binaries the whole app depends on MUST be signed — assert it regardless of layout.
for b in ffmpeg ffprobe; do
  p="$(find "$APP/Contents" -name "$b" -type f -print -quit)"
  if [ -z "$p" ] || ! codesign --verify --strict "$p"; then
    echo "FATAL: bundled $b is missing or unsigned" >&2; exit 1
  fi
done

echo "==> signing the app bundle (with entitlements)"
sign_retry "$APP" --options runtime --entitlements "$ENTITLEMENTS" || exit 1
codesign --verify --strict --verbose=2 "$APP"

# 5) notarize + STAPLE the .app itself (so it works offline even if copied out of the DMG)
echo "==> notarizing the app (a few minutes)…"
ditto -c -k --keepParent "$APP" "dist/CullStation.zip"
xcrun notarytool submit "dist/CullStation.zip" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
rm -f "dist/CullStation.zip"

# 6) build the drag-to-Applications DMG from the stapled app
echo "==> building $DMG"
rm -f "$DMG"
STAGE="$(mktemp -d)"; ditto "$APP" "$STAGE/CullStation.app"; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "CullStation" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
sign_retry "$DMG" || exit 1
codesign --verify --verbose=2 "$DMG"

# 7) notarize + staple the DMG too
echo "==> notarizing the dmg…"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl -a -vvv -t open --context context:primary-signature "$DMG" 2>&1 | sed 's/^/  /' || true

echo "✅ done — $DMG  (signed, notarized, stapled).  Hand this DMG to users."
