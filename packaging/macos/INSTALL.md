# CullStation for macOS — build & install

CullStation ships as **`CullStation.app`**, a self-contained menu-bar app (🎞️). It bundles
its own Python, all dependencies, and a static **ffmpeg/ffprobe (with hardware HEVC)** — so the
end user installs **nothing else**. The web dashboard runs locally inside the app; clicking the
menu-bar icon → *Open Dashboard* opens it in the browser.

There are two audiences below: **builders** (you, once, on the Mac mini) and **end users**.

---

## A. End-user install (after you've built the signed `.dmg`)

1. Double-click **`CullStation-<version>.dmg`**.
2. Drag **CullStation** onto the **Applications** shortcut.
3. Open **CullStation** from Applications (or Launchpad). Because it's notarized, you get one
   benign prompt — *"CullStation is an app downloaded from the Internet. Open?"* → **Open**.
   (No prompt on later launches; works offline thanks to a stapled ticket.)
4. A 🎞️ icon appears in the menu bar. Click it → **Mount NAS Share…** the first time to connect
   to the Marketing Media share (enter the NAS credentials once — macOS remembers them).
5. Click **Open Dashboard**. Done.

> Auto-start at login (optional): copy `com.skypark.cullstation.plist` to
> `~/Library/LaunchAgents/` and run `launchctl load ~/Library/LaunchAgents/com.skypark.cullstation.plist`.

---

## B. Builder: produce the signed, notarized `.dmg`

**Build on a Mac** (PyInstaller can't cross-compile — you cannot build this on Linux/Windows). The
build host's architecture is what you ship; the kit auto-fetches the matching ffmpeg:

- **Apple-Silicon Mac** (recommended — e.g. your M-series MacBook or the M4 mini) → a native **arm64**
  app that runs natively on the marketing Mac mini.
- **Intel Mac** → an **x86_64** app. It still runs on the Apple-Silicon mini, but **under Rosetta**
  (slower). Fine in a pinch; prefer an Apple-Silicon build host.

You build the `.dmg` **once** on your Mac; everyone else just drags the app to Applications.

### One-time setup

1. **Xcode command-line tools**
   ```sh
   xcode-select --install
   ```
2. **Developer ID Application certificate** — in *Xcode ▸ Settings ▸ Accounts* (or developer.apple.com
   ▸ Certificates) create/download a **Developer ID Application** cert and let it install into your
   **login keychain**. Confirm and note the identity string:
   ```sh
   security find-identity -v -p codesigning
   # → "Developer ID Application: Justin Nunn (ABCDE12345)"   ← the (ABCDE12345) is your TEAM_ID
   ```
3. **App-specific password** for notarization — at <https://appleid.apple.com> ▸ *Sign-In & Security ▸
   App-Specific Passwords*, generate one (e.g. label it "notarytool"). Then store a notarytool profile:
   ```sh
   xcrun notarytool store-credentials "AC_CULL" \
     --apple-id you@example.com \
     --team-id ABCDE12345 \
     --password <the-app-specific-password>
   ```

### Build

```sh
cd CullStation/packaging/macos
SIGN=1 DEV_ID="Developer ID Application: Justin Nunn (ABCDE12345)" ./build_app.sh
```

That script:
1. creates a throwaway build venv (`requirements-build.txt`),
2. fetches the static arm64 ffmpeg/ffprobe (`fetch_ffmpeg.sh`) and verifies arch + `hevc_videotoolbox`,
3. runs PyInstaller (`CullStation.spec`) → `dist/CullStation.app`,
4. code-signs **inside-out** (every nested binary first, the app last — never `--deep`) with the
   hardened runtime + secure timestamp + `entitlements.plist`,
5. notarizes and **staples the app**,
6. builds the drag-to-Applications **DMG**, then notarizes + staples **the DMG** too.

Output: **`CullStation-<version>.dmg`** — hand this to users.

> Quick local test without signing: `./build_app.sh` (unsigned). It builds the `.app` for testing;
> Gatekeeper will block first launch until you do the signed build.

### Version bumps
```sh
CULL_VERSION=1.1.0 SIGN=1 DEV_ID="…" ./build_app.sh
```

---

## C. What the app expects at runtime

| Thing | Default on macOS | Override |
|---|---|---|
| Marketing Media share | `/Volumes/Marketing Media` | `CULL_SOURCE` |
| Photo archive + manifest | `…/Marketing Archive` | `CULL_ARCHIVE` |
| ffmpeg / ffprobe | the bundled binaries | `CULL_FFMPEG` / `CULL_FFPROBE` |
| Web port | `8765` | `CULL_PORT` |
| NAS host / share (for *Mount NAS Share…*) | `192.168.1.100` / `Marketing Media` | `CULL_NAS_HOST` / `CULL_NAS_SHARE` |

The share must be mounted for the app to read/transcode; the menu bar shows **Mounts: ✓/✗** and the
dashboard's Overview shows mount status. Nothing is ever deleted without the explicit two-phase,
typed-confirm purge in the Deletion tab.

---

## D. Troubleshooting

- **"hevc_videotoolbox MISSING"** during `fetch_ffmpeg.sh` — osxexperts rolled the version; pass the
  current URLs: `FFMPEG_URL=… FFPROBE_URL=… ./fetch_ffmpeg.sh`.
- **notarytool says *Invalid*** — get the reasons: `xcrun notarytool log <submission-id> --keychain-profile AC_CULL`.
  Usual causes: a nested binary missing `--timestamp` or `--options runtime` (re-run the signed build),
  or an unsigned bundled binary.
- **App opens but the dashboard never loads** — the embedded server didn't start; launch the app from a
  terminal to see logs: `/Applications/CullStation.app/Contents/MacOS/CullStation`.
- **Video fails on certain clips** with VideoToolbox errors (`-12903/-12905`) — a known HW limitation on
  some 10-bit sources; those clips can be skipped (mark *keep*) or transcoded with `CULL_VIDEO_ENCODER=libx265`.
- **License note** — the bundled ffmpeg is a GPL build. Redistributing the `.app` outside the company
  triggers GPL source-offer obligations for ffmpeg; for internal use on company Macs this is fine.
