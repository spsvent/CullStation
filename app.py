#!/usr/bin/env python3
"""CullStation — local dashboard for the SkyPark NAS cull.
  Overview      mounts, NAS free space, live photo-conversion progress + ETA
  Photo Review  approve/reject the darktable RAW->JPEG conversions
  Deletion      verified, staged delete of approved originals (the space payoff)
  Video         ProRes->HEVC transcode queue (HW hevc_videotoolbox) + scheduler

Run:  pip install fastapi uvicorn pillow ;  python3 app.py    (or ./run.sh)
"""
import os, io, sys, json, time, platform, shutil, mimetypes, hashlib
from fastapi import FastAPI, Body, HTTPException, Query, Header, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
import config as C
import store
from video import VideoWorker
import deletion

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

def csrf_guard(x_cullstation: str = Header(default=None)):
    """Block cross-site requests to state-changing routes: the dashboard's own
    fetch() sends X-CullStation:1, which a CORS 'simple request' cannot forge."""
    if x_cullstation != "1":
        raise HTTPException(403, "missing X-CullStation header")

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

app = FastAPI(title="CullStation")
HERE = os.path.dirname(os.path.abspath(__file__))
# When frozen by PyInstaller the bundled data (static/) lives under sys._MEIPASS, not next to __file__.
STATIC_DIR = os.path.join(getattr(sys, "_MEIPASS", HERE), "static")
video = VideoWorker()

def _bin_ok(b):
    """A configured binary is usable if it's an existing absolute path or resolvable on PATH."""
    return os.path.isfile(b) if os.path.isabs(b) else shutil.which(b) is not None

def _safe(path, root):
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    if rp != rr and not rp.startswith(rr + os.sep):
        raise HTTPException(403, "path outside allowed root")
    return rp

def _mounted(path):
    """True only if `path` is on a different device than the local root — i.e. a live
    network mount (or a subdir of one). Avoids the false positive where a leftover local
    stub directory reads as 'mounted' just because it's non-empty. (review finding)"""
    try:
        if not os.path.exists(path):
            return False
        if os.path.ismount(path):
            return True
        return os.stat(path).st_dev != os.stat("/").st_dev
    except Exception:
        return False

# ───────────────────────── status / overview ─────────────────────────
@app.get("/api/status")
def status():
    return {
        "mounts": {
            "source": {"path": C.SOURCE_ROOT, "ok": _mounted(C.SOURCE_ROOT)},
            "archive": {"path": C.ARCHIVE_ROOT, "ok": _mounted(C.ARCHIVE_ROOT)},
        },
        "nas": store.free_space(C.ARCHIVE_ROOT),
        "photos": store.photo_stats(),
        "video": video.summary(),
        "convert": _convert_state(),
        "host": platform.node(), "time": int(time.time()),
    }

@app.get("/api/selftest")
def selftest():
    return {
        "mounts_ok": _mounted(C.SOURCE_ROOT) and _mounted(C.ARCHIVE_ROOT),
        "ffmpeg": _bin_ok(C.FFMPEG), "ffprobe": _bin_ok(C.FFPROBE),
        "manifest": os.path.isfile(C.MANIFEST),
        "pillow": HAVE_PIL, "platform": platform.system(),
        "encoder": video.cfg.get("encoder"),
        "nas": store.free_space(C.ARCHIVE_ROOT),
    }

# ─────────────────────────── photo review ────────────────────────────
@app.get("/api/photos")
def photos(status: str = Query("REVIEW"), limit: int = 300, offset: int = 0,
           sort: str = "corr", decided: str = Query("all")):
    recs = [r for r in store.read_manifest() if r.get("status") == status and r.get("out")]
    if sort == "corr":
        recs.sort(key=lambda r: (r.get("preview_corr") is None, r.get("preview_corr") or 0))
    elif sort == "date":
        recs.sort(key=lambda r: r.get("capture_date") or "")
    elif sort == "folder":
        recs.sort(key=lambda r: r.get("src_rel") or "")
    reviews = store.load_reviews()
    def _dec(r):   # the user's recorded decision for this RAW, or None if untouched
        return reviews.get(r.get("src_rel"), {}).get("decision")
    n_all = len(recs)
    n_pending = sum(1 for r in recs if not _dec(r))
    counts = {"all": n_all, "pending": n_pending, "decided": n_all - n_pending}
    # `decided` filter: pending = still needs input, decided = already approved/deleted/etc.
    if decided == "pending":
        view = [r for r in recs if not _dec(r)]
    elif decided == "decided":
        view = [r for r in recs if _dec(r)]
    else:
        view = recs
    total = len(view)
    items = [{
        "key": r.get("src_rel"), "src_rel": r.get("src_rel"), "out": r.get("out"),
        # archive-relative jpeg path — portable (the manifest's absolute `out` is from the
        # conversion box and won't resolve when the app runs on the Mac). Prefer the
        # converter's recorded out_rel; fall back to the RAW stem for older manifest lines.
        "img": r.get("out_rel") or ((os.path.splitext(r["src_rel"])[0] + ".jpg") if r.get("src_rel") else None),
        "status": r.get("status"), "corr": r.get("preview_corr"),
        "luma_before": r.get("mean_luma_before"), "luma_after": r.get("mean_luma_after"),
        "capture_date": r.get("capture_date"), "out_dims": r.get("out_dims"),
        "decision": _dec(r),
    } for r in view[offset:offset + limit]]
    return {"total": total, "offset": offset, "limit": limit,
            "filter": decided, "counts": counts, "items": items}

@app.get("/api/img")
def img(path: str, w: int = 0):
    # `path` is RELATIVE to ARCHIVE_ROOT so it resolves wherever the app runs (Mac or Linux).
    # (Absolute paths are still accepted if already under ARCHIVE_ROOT, for back-compat.)
    full = path if os.path.isabs(path) else os.path.join(C.ARCHIVE_ROOT, path)
    p = _safe(full, C.ARCHIVE_ROOT)
    if os.path.splitext(p)[1].lower() not in IMG_EXT:   # image endpoint != generic file reader
        raise HTTPException(403, "not an image")
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    headers = {"Cache-Control": "max-age=86400"}
    if w and HAVE_PIL:
        try:
            with Image.open(p) as im:
                im.thumbnail((w, w))
                buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=85); buf.seek(0)
        except Exception:
            raise HTTPException(415, "unreadable image")
        return StreamingResponse(buf, media_type="image/jpeg", headers=headers)
    mt = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mt, headers=headers)

@app.get("/api/raw")
def raw(path: str):
    """Download the original RAW (relative to RAW_ROOT) to edit manually in Photoshop /
    darktable / RawTherapee / DaVinci, etc. Path-contained to the RAW tree."""
    full = path if os.path.isabs(path) else os.path.join(C.RAW_ROOT, path)
    p = _safe(full, C.RAW_ROOT)
    if not os.path.isfile(p):
        raise HTTPException(404, "RAW not found (is the share mounted?)")
    return FileResponse(p, media_type="application/octet-stream", filename=os.path.basename(p))

DECISIONS = ("approve", "reject", "delete", "manual", "clear")
# approve=keep JPEG+delete RAW · delete=remove RAW (no JPEG) · manual=keep RAW for hand-editing · reject(legacy)

@app.post("/api/review", dependencies=[Depends(csrf_guard)])
def review(body: dict = Body(...)):
    key, decision = body.get("key"), body.get("decision")
    if not key or decision not in DECISIONS:
        raise HTTPException(400, f"key + decision{DECISIONS} required")
    total = store.apply_review(key, decision)
    return {"ok": True, "key": key, "decision": decision, "total_reviewed": total}

@app.post("/api/reconvert", dependencies=[Depends(csrf_guard)])
def reconvert(body: dict = Body(...)):
    """Reject -> queue this RAW to be re-developed one step darker by the conversion box."""
    key = body.get("key")
    if not key or deletion._bad_rel(key):
        raise HTTPException(400, "valid src_rel key required")
    os.makedirs(C.RECONVERT_DIR, exist_ok=True)
    fn = hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json"   # one request per photo (dedupes clicks)
    store._write_json(os.path.join(C.RECONVERT_DIR, fn), {"rel": key, "ts": int(time.time()), "attempts": 0})
    store.apply_review(key, "clear")   # clear ONLY after the request is safely queued
    pending = len([f for f in os.listdir(C.RECONVERT_DIR) if f.endswith(".json")])
    return {"ok": True, "queued": key, "pending": pending}

@app.get("/api/reconvert/pending")
def reconvert_pending():
    try:
        n = len([f for f in os.listdir(C.RECONVERT_DIR) if f.endswith(".json")])
    except Exception:
        n = 0
    return {"pending": n}

# ── convert-older-photos: the user sets a cutoff year; the box worker converts RAWs <= it ──
def _convert_state():
    """Live status the box worker writes, plus the policy the user last set. worker_alive
    is true only if the worker updated the status file recently (else the box is down)."""
    try:
        with open(C.CONVERT_STATUS) as fh:
            s = json.load(fh)
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    try:
        with open(C.CONVERT_POLICY) as fh:
            s["policy_cutoff"] = (json.load(fh) or {}).get("cutoff_year")
    except Exception:
        s["policy_cutoff"] = None
    # Alive if the worker heartbeat (every ~5s) stamped the status recently. Guarded so a
    # malformed 'updated' degrades to False; 90s tolerates modest box/Mac clock skew.
    try:
        s["worker_alive"] = (time.time() - float(s.get("updated"))) < 90
    except (TypeError, ValueError):
        s["worker_alive"] = False
    return s

@app.get("/api/convert/status")
def convert_status():
    return _convert_state()

@app.post("/api/convert/policy", dependencies=[Depends(csrf_guard)])
def convert_policy(body: dict = Body(...)):
    """Set 'convert RAWs from this year and older'. Already-converted photos are unaffected;
    raising the year later just brings the newly-in-scope RAWs into the conversion queue."""
    try:
        y = int(body.get("cutoff_year"))
    except (TypeError, ValueError):
        raise HTTPException(400, "cutoff_year must be a 4-digit year")
    if not (1990 <= y <= 2100):
        raise HTTPException(400, "cutoff_year out of range")
    store._write_json(C.CONVERT_POLICY, {"cutoff_year": y, "ts": int(time.time())})
    return {"ok": True, "cutoff_year": y}

@app.post("/api/review/bulk", dependencies=[Depends(csrf_guard)])
def review_bulk(body: dict = Body(...)):
    keys, decision = body.get("keys") or [], body.get("decision")
    if decision not in DECISIONS:
        raise HTTPException(400, "decision required")
    total = store.apply_review(keys, decision)
    return {"ok": True, "applied": len(keys), "total_reviewed": total}

# ───────────────────────────── deletion ──────────────────────────────
@app.get("/api/deletion/preview")
def del_preview(): return deletion.preview()

@app.get("/api/deletion/staged")
def del_staged(limit: int = 500, offset: int = 0):
    """limit=0 → every staged file (the UI's 'load all' path)."""
    return deletion.staged_list(max(0, limit), offset)

@app.post("/api/deletion/stage", dependencies=[Depends(csrf_guard)])
def del_stage(body: dict = Body(default={})): return deletion.stage(body.get("keys"))

@app.post("/api/deletion/stage/start", dependencies=[Depends(csrf_guard)])
def del_stage_start(body: dict = Body(default={})):
    """Non-blocking staging: returns at once, the UI polls /api/deletion/stage/progress
    so a multi-minute NAS move shows a live bar instead of a frozen button."""
    r = deletion.stage_start(body.get("keys"))
    return {**r, "progress": deletion.stage_progress()}

@app.get("/api/deletion/stage/progress")
def del_stage_progress(): return deletion.stage_progress()

@app.post("/api/deletion/recover", dependencies=[Depends(csrf_guard)])
def del_recover(body: dict = Body(default={})): return deletion.recover(body.get("keys"))

@app.post("/api/deletion/purge", dependencies=[Depends(csrf_guard)])
def del_purge(body: dict = Body(...)): return deletion.purge(body.get("confirm"))

# ────────────────────────────── video ────────────────────────────────
@app.get("/api/video")
def video_status(): return video.summary()

@app.get("/api/video/queue")
def video_queue(): return {"items": video.queue()}

@app.post("/api/video/config", dependencies=[Depends(csrf_guard)])
def video_config(body: dict = Body(...)): return video.set_cfg(body or {})

@app.post("/api/video/keep", dependencies=[Depends(csrf_guard)])
def video_keep(body: dict = Body(...)):
    return video.set_keep(body.get("rel"), bool(body.get("keep")))

@app.get("/api/video/failed")
def video_failed(): return video.failed()

@app.get("/api/video/fallbacks")
def video_fallbacks(): return video.fallbacks()

@app.get("/api/video/diagnose")
def video_diagnose(rel: str):
    """ffprobe a source clip (path-contained) so a FAIL can be explained, not guessed at."""
    return video.diagnose(rel)

@app.post("/api/video/retry", dependencies=[Depends(csrf_guard)])
def video_retry(body: dict = Body(...)):
    return video.retry(body.get("rel"))

# NOTE: keep this catch-all LAST — it would otherwise swallow /api/video/retry.
@app.post("/api/video/{action}", dependencies=[Depends(csrf_guard)])
def video_control(action: str):
    if action == "start": video.start()
    elif action == "pause": video.do_pause()
    elif action == "resume": video.resume()
    elif action == "stop": video.do_stop()
    else: raise HTTPException(400, "action must be start|pause|resume|stop")
    return video.summary()

# ─────────────────────────────── ui ──────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    print(f"CullStation → http://{C.HOST}:{C.PORT}   archive={C.ARCHIVE_ROOT}")
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning")
