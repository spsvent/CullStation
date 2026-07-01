"""CullStation configuration. All paths overridable by env vars so the same code
runs on the Linux build box (NFS /mnt/...) and on the marketing Mac (/Volumes/...)."""
import os, sys, platform

_MAC = platform.system() == "Darwin"

def _int(name, default):
    """Parse an int env var; fall back (with a warning) instead of crashing import."""
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        print(f"[config] {name}={v!r} is not an integer; using {default}", file=sys.stderr)
        return default

# Marketing Media share root + the photo archive (where convert_dt.py writes JPEGs + manifest).
# Defaults are platform-aware: the packaged Mac app finds the SMB mount with no env needed.
SOURCE_ROOT  = os.environ.get("CULL_SOURCE",  "/Volumes/Marketing Media" if _MAC else "/mnt/marketing")
ARCHIVE_ROOT = os.environ.get("CULL_ARCHIVE",
                              os.path.join(SOURCE_ROOT, "Marketing Archive") if _MAC else "/mnt/archive")
MANIFEST     = os.environ.get("CULL_MANIFEST", os.path.join(ARCHIVE_ROOT, "_manifest.jsonl"))
# Re-convert worker writes here (its OWN file) so two processes never append to one file
# on NFS (no torn lines). The app merges this over the main manifest, re-convert wins.
RECONVERT_MANIFEST = os.environ.get("CULL_RECONVERT_MANIFEST", os.path.join(ARCHIVE_ROOT, "_manifest_reconvert.jsonl"))
REVIEW_FILE  = os.environ.get("CULL_REVIEW",   os.path.join(ARCHIVE_ROOT, "_review_decisions.json"))
# Reject -> re-develop darker: the app drops a request here; the worker on the conversion box picks it up.
RECONVERT_DIR = os.environ.get("CULL_RECONVERT", os.path.join(ARCHIVE_ROOT, "_reconvert_queue"))
# "Convert RAWs from year N and older": the app writes the cutoff policy; photo_convert_worker.py
# on the box converts everything <= N and writes back live status.
CONVERT_POLICY = os.environ.get("CULL_CONVERT_POLICY", os.path.join(ARCHIVE_ROOT, "_convert_policy.json"))
CONVERT_STATUS = os.environ.get("CULL_CONVERT_STATUS", os.path.join(ARCHIVE_ROOT, "_convert_status.json"))
# photo_convert_worker writes its OWN shard (single writer => no NFS append race); app merges it.
CONVERT_MANIFEST = os.environ.get("CULL_CONVERT_MANIFEST", os.path.join(ARCHIVE_ROOT, "_manifest_convert.jsonl"))
RECONVERT_STEP_EV = float(os.environ.get("CULL_RECONVERT_STEP", "-0.4"))   # darker per reject
RECONVERT_MIN_EV  = float(os.environ.get("CULL_RECONVERT_MIN", "-2.4"))    # floor

# Video transcode (ProRes -> HEVC)
VIDEO_SRC    = os.environ.get("CULL_VIDEO_SRC", os.path.join(SOURCE_ROOT, "Video Archive"))
VIDEO_OUT    = os.environ.get("CULL_VIDEO_OUT", os.path.join(SOURCE_ROOT, "Video Archive (HEVC)"))
VIDEO_STATE  = os.environ.get("CULL_VIDEO_STATE", os.path.join(ARCHIVE_ROOT, "_video_state.json"))
VIDEO_ENCODER= os.environ.get("CULL_VIDEO_ENCODER", "hevc_videotoolbox")  # Mac HW; "libx265" to test on Linux
VT_QUALITY   = os.environ.get("CULL_VT_QUALITY", "60")
X265_CRF     = os.environ.get("CULL_X265_CRF", "19")

# ffmpeg/ffprobe binaries. The packaged Mac .app sets these to the bundled static builds
# (absolute paths); otherwise the ones on PATH are used. caffeinate is always system-provided.
FFMPEG    = os.environ.get("CULL_FFMPEG",  "ffmpeg")
FFPROBE   = os.environ.get("CULL_FFPROBE", "ffprobe")
CAFFEINATE = "/usr/bin/caffeinate"

# Original RAW tree (for the verified-delete step) + a holding area for staged deletes.
RAW_ROOT     = os.environ.get("CULL_RAW_ROOT", os.path.join(SOURCE_ROOT, "Raw photo backup"))
HOLDING_ROOT = os.environ.get("CULL_HOLDING", os.path.join(SOURCE_ROOT, "_RAW_pending_delete"))
DELETION_LOG = os.environ.get("CULL_DELLOG", os.path.join(ARCHIVE_ROOT, "_deletion_log.json"))

# Expected total for the photo run (for the progress bar); 0 = unknown/auto
PHOTO_TARGET = _int("CULL_PHOTO_TARGET", 29008)

PORT = _int("CULL_PORT", 8765)
HOST = os.environ.get("CULL_HOST", "127.0.0.1")
# macOS `caffeinate` prevents sleep while processing; auto-detected at runtime.
