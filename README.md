# CullStation

Local dashboard for the SkyPark NAS cull — runs on the marketing Mac mini.

- **Overview** — mount status, NAS free-space gauge, live photo-conversion progress + ETA, video summary.
- **Photo Review** — judge the darktable RAW→JPEG conversions (the dark/low-confidence "REVIEW" ones especially) before any original is deleted. Four per-photo actions, keyboard-driven (`A`/`R`/`M`/`D`, `J`/`K` to move, `Enter` to zoom) + bulk:
  - **✓ Approve** — keep the JPEG, the RAW becomes eligible for deletion.
  - **↻ Darker** — too bright: re-develop one stop darker and re-queue for review (each click −0.4 EV). Routed to the conversion box via a shared queue + worker; the RAW is kept until you Approve a version.
  - **⬇ RAW** — download the original to edit manually (Photoshop / darktable / RawTherapee / Resolve); marks it *manual* so the RAW is kept.
  - **🗑 Delete** — delete the RAW, no JPEG kept.
- **Deletion** — the space payoff: a verified, **two-phase** delete of originals. Only files that are auto-verified (PASS) or human-approved are eligible. *Stage* moves them to a holding folder (recoverable); *Purge* permanently frees the space (requires typing `PURGE`).
- **Video** — ProRes→HEVC transcode queue (hardware `hevc_videotoolbox`), pausable/resumable, with an off-hours **scheduler** and live per-file progress. Keeps the Mac awake only while encoding.

## Run on the Mac

```sh
brew install ffmpeg python      # ffmpeg for video; python3 for the app
# mount the share in Finder:  Go ▸ Connect to Server ▸ smb://192.168.1.100  → "Marketing Media"
./run.sh                        # creates a venv, installs deps, starts the server
```

Open <http://127.0.0.1:8765>. `run.sh` warns if the configured encoder isn't in your ffmpeg build.

## Package as a Mac app (for non-technical users)

`packaging/macos/` builds **`CullStation.app`** — a self-contained menu-bar app that bundles its
own Python + deps + a static ffmpeg (hardware HEVC). The user installs nothing else: drag the
`.dmg` to Applications, click the 🎞️ menu-bar icon → *Open Dashboard*. Build it on the Apple-Silicon
Mac (PyInstaller can't cross-compile):

```sh
cd packaging/macos
SIGN=1 DEV_ID="Developer ID Application: NAME (TEAMID)" ./build_app.sh   # → CullStation-<ver>.dmg
```

Full builder + end-user steps (signing, notarization, login auto-start): **`packaging/macos/INSTALL.md`**.

## Config (env vars, all optional)

| var | default (Mac) | meaning |
|---|---|---|
| `CULL_SOURCE` | `/Volumes/Marketing Media` | share root |
| `CULL_ARCHIVE` | `…/Marketing Archive` | photo JPEGs + `_manifest.jsonl` |
| `CULL_RAW_ROOT` | `…/Raw photo backup` | original RAW tree (for verified delete) |
| `CULL_VIDEO_SRC` / `CULL_VIDEO_OUT` | `…/Video Archive` / `…(HEVC)` | video in/out (OUT must be outside SRC) |
| `CULL_VIDEO_ENCODER` | `hevc_videotoolbox` | use `libx265` to test without Apple HW |
| `CULL_VT_QUALITY` / `CULL_X265_CRF` | `60` / `19` | quality levers |
| `CULL_PORT` | `8765` | web port |

## Safety

- **Never deletes or modifies a source during conversion.** The photo pipeline reads RAW; the video worker writes HEVC to a *separate* tree and verifies each output (duration **and** HEVC codec) before keeping it.
- **Deletion is explicit and two-phase** (stage → recover-or-purge); originals are only removed on a typed-confirm Purge, and only when verified/approved.
- The server binds `127.0.0.1` by default; state-changing requests require an `X-CullStation` header (CSRF guard). Keep `CULL_HOST` at `127.0.0.1` unless you add real auth.

## Files

`app.py` (FastAPI routes) · `store.py` (manifest cache, reviews, free-space/ETA) · `video.py` (transcode worker) · `deletion.py` (staged verified-delete) · `config.py` · `static/index.html` (dashboard) · `run.sh`.
