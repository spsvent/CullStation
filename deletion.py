"""Verified-delete of RAW originals — safety-first, two-phase, path-contained, locked.

A RAW is *deletable* only when its conversion is trustworthy:
  - manifest status PASS (auto-verified), OR status REVIEW that the user APPROVED,
  - the output JPEG exists on disk,
  - and src_rel is a SAFE relative path (no absolute paths, no '..') that stays inside RAW_ROOT.

Phase 1 stage(): move the RAW into a holding tree on the same volume (recoverable).
Phase 2 purge(): permanently remove the holding tree (frees space) — needs confirm='PURGE'.
recover() moves staged files back, never overwriting a file already in place.

Every log read-modify-write is lock-guarded and persisted atomically (fsync). A corrupt
log is preserved (backed up), never silently wiped — it is the only recovery map.
"""
import os, json, time, shutil, threading
import config as C
import store

_lock = threading.RLock()

# ---------------- live job progress ----------------
# stage/purge/recover each walk thousands of files on a NAS and each holds _lock for the
# whole run, so a synchronous request would sit there for minutes with the UI frozen and
# every other deletion route blocked behind it. They all publish per-file progress into one
# job slot (only one can run at a time anyway — they serialise on _lock) guarded by its OWN
# lock, so /api/deletion/job answers instantly while a job holds _lock.
_prog_lock = threading.Lock()
_JOB0 = {"kind": "", "state": "idle", "phase": "", "total": 0, "done": 0, "ok": 0,
         "skipped": 0, "bytes": 0, "current": "", "errors": [], "error_count": 0,
         "recent": [], "started": 0, "finished": 0, "result": None}
_job = dict(_JOB0)

# what each job calls the thing it did, for the dashboard
JOB_VERB = {"stage": "moved to holding", "purge": "permanently deleted",
            "recover": "put back in the RAW tree", "adopt": "adopted into the log"}

def _pub(**kw):
    with _prog_lock:
        _job.update(kw)

def _job_claim(kind):
    """Take the job slot, or return False if one is already running."""
    with _prog_lock:
        if _job["state"] == "running":
            return False
        _job.update(_JOB0, kind=kind, state="running", phase="scanning",
                    errors=[], recent=[], started=int(time.time()))
    return True

def job_progress():
    """Snapshot of the running (or last) job — safe to poll at any time."""
    with _prog_lock:
        j = dict(_job)
    j["errors"] = list(j["errors"]); j["recent"] = list(j["recent"])
    j["gb"] = round(j["bytes"] / 1e9, 2)
    j["pct"] = round(100.0 * j["done"] / j["total"], 1) if j["total"] else 0.0
    j["verb"] = JOB_VERB.get(j["kind"], "processed")
    return j

# back-compat for anything still calling the stage-specific names
stage_progress = job_progress

def job_start(kind, **kw):
    """Run stage/purge/recover in the background and return at once; the caller polls
    job_progress(). Refuses to start while another job is running."""
    fn = {"stage": stage, "purge": purge, "recover": recover, "adopt": adopt}.get(kind)
    if fn is None:
        return {"started": False, "reason": f"unknown job {kind!r}"}
    if kind == "purge" and kw.get("confirm") != "PURGE":
        return {"started": False, "reason": "confirm token must be exactly 'PURGE'"}
    if not _job_claim(kind):
        with _prog_lock:
            running = _job["kind"]
        return {"started": False, "reason": f"a {running} job is already running"}

    def _bg():
        try:
            fn(_own_job=False, **kw)
        except Exception as e:   # the job already published state=error; last-resort net
            _pub(state="error", current="", errors=[str(e)], error_count=1,
                 finished=int(time.time()))
    threading.Thread(target=_bg, daemon=True).start()
    return {"started": True}

def stage_start(keys=None):
    return job_start("stage", keys=keys)

def _job_begin(kind, own):
    """Called at the top of a job body: claim the slot when invoked synchronously. A direct
    call must NOT stomp the progress of a background job that already owns the slot (it will
    just queue on _lock behind it)."""
    if not own:
        return
    with _prog_lock:
        if _job["state"] == "running":
            return
        _job.update(_JOB0, kind=kind, state="running", phase="scanning",
                    errors=[], recent=[], started=int(time.time()))

def _job_fail(msg):
    _pub(state="error", phase="", current="", errors=[msg], error_count=1,
         finished=int(time.time()))

def _job_end(done, ok, bytes_, skipped, recent, errors):
    invalidate_preview()
    _pub(state="done", phase="", current="", done=done, ok=ok, bytes=bytes_,
         skipped=skipped, recent=recent[-200:], errors=errors[-20:],
         error_count=len(errors), finished=int(time.time()))

# ---------------- path safety ----------------
def _contained(path, root):
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)

def _bad_rel(rel):
    """Reject manifest-supplied paths that aren't safe to join under a root."""
    if not rel or os.path.isabs(rel):
        return True
    return any(p in ("", "..", ".") for p in rel.replace("\\", "/").split("/"))

def _writable(path):
    return os.access(os.path.dirname(path), os.W_OK)

def _same_volume(a, b):
    # Fail CLOSED: if we can't positively confirm same-volume, treat as different so
    # stage() refuses (a cross-volume move would copy and double space on a full NAS).
    try:
        ar = a if os.path.exists(a) else os.path.dirname(a)
        br = b if os.path.exists(b) else os.path.dirname(b)
        return os.stat(ar).st_dev == os.stat(br).st_dev
    except Exception:
        return False

def _jpg_rel(rec):
    """Archive-relative JPEG path: prefer the converter's recorded out_rel; else derive
    from the RAW stem. (out_rel is authoritative and avoids guessing the layout.)"""
    o = rec.get("out_rel")
    if o and not _bad_rel(o):
        return o
    rel = rec.get("src_rel")
    return (os.path.splitext(rel)[0] + ".jpg") if rel else None

def _kept_jpg_set():
    """Realpaths of archive JPEGs that some NON-delete record wants to keep — these must
    never be removed by a 'delete' item that happens to share the same JPEG path (stem collision)."""
    reviews = store.load_reviews(); keep = set()
    for r in store.read_manifest():
        rel, st = r.get("src_rel"), r.get("status")
        if _bad_rel(rel):
            continue
        dec = reviews.get(rel, {}).get("decision")
        if dec == "delete":
            continue
        if st == "PASS" or (st == "REVIEW" and dec == "approve"):
            jr = _jpg_rel(r)
            if jr:
                keep.add(os.path.realpath(os.path.join(C.ARCHIVE_ROOT, jr)))
    return keep

def _keys(keys, available):
    if keys in (None, "all"):
        return list(available)
    if isinstance(keys, str):           # a single key, NOT an iterable of chars
        keys = [keys]
    return [k for k in keys if k in available]

# ---------------- durable, non-destructive log ----------------
def _load_log():
    if not os.path.exists(C.DELETION_LOG):
        return {"staged": {}}
    try:
        with open(C.DELETION_LOG) as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or "staged" not in d:
            raise ValueError("bad shape")
        return d
    except Exception:
        # preserve the corrupt log under a UNIQUE name; never proceed (and risk a later
        # _save_log overwriting it with an empty map) unless the original is safely backed up.
        base = C.DELETION_LOG + f".corrupt-{int(time.time())}"
        bak, i = base, 0
        while os.path.exists(bak):
            i += 1; bak = f"{base}.{i}"
        try:
            os.replace(C.DELETION_LOG, bak)
            print(f"[deletion] corrupt log preserved at {bak}; starting fresh")
        except Exception as e:
            raise RuntimeError(f"deletion log corrupt and could not be backed up ({e}); refusing to continue")
        return {"staged": {}}

def _save_log(d):
    store._write_json(C.DELETION_LOG, d)   # atomic + fsync

# ---------------- eligibility ----------------
def deletable():
    reviews = store.load_reviews()
    out = []
    for r in store.read_manifest():
        rel, st = r.get("src_rel"), r.get("status")
        if _bad_rel(rel):
            continue
        decision = reviews.get(rel, {}).get("decision")
        if decision == "manual":
            continue   # user is hand-editing this RAW elsewhere — never cull it, even if PASS
        approved = decision == "approve"
        delete_no_archive = decision == "delete"   # user wants the RAW gone, no JPEG kept
        if not (st == "PASS" or (st == "REVIEW" and approved) or delete_no_archive):
            continue
        # archive-relative jpeg path — portable across machines (prefers recorded out_rel)
        jr = _jpg_rel(r)
        jpg = os.path.join(C.ARCHIVE_ROOT, jr) if jr else ""
        # "delete" mode doesn't require a JPEG; the others do (the JPEG is the kept copy)
        if not delete_no_archive and not (os.path.isfile(jpg) and os.path.getsize(jpg) > 1024):
            continue
        src = os.path.join(C.RAW_ROOT, rel)
        if not _contained(src, C.RAW_ROOT):
            continue
        out.append({"key": rel, "src": src, "jpg": jpg, "status": st, "approved": approved,
                    "no_archive": delete_no_archive,
                    "src_bytes": r.get("src_size", 0), "exists": os.path.isfile(src)})
    return out

_preview_cache = {"t": 0, "val": None}
PREVIEW_TTL = 60   # seconds

def invalidate_preview():
    _preview_cache["t"] = 0

def preview(force=False):
    """deletable() stats every manifest row against the RAW tree — ~29k round trips over SMB,
    which is far too slow to redo on every dashboard poll. Cache it briefly; every job that
    changes the picture invalidates it."""
    now = time.time()
    if not force and _preview_cache["val"] and (now - _preview_cache["t"]) < PREVIEW_TTL:
        return _preview_cache["val"]
    present = [x for x in deletable() if x["exists"]]
    val = {"count": len(present), "bytes": sum(x["src_bytes"] for x in present),
           "gb": round(sum(x["src_bytes"] for x in present) / 1e9, 2),
           "pass": sum(1 for x in present if x["status"] == "PASS"),
           "approved_review": sum(1 for x in present if x["status"] == "REVIEW"),
           "items": present[:500], "cached_age_s": 0}
    _preview_cache.update(t=now, val=val)
    return val

# ---------------- two-phase delete ----------------
def stage(keys=None, _own_job=True):
    """Move deletable RAWs into the holding tree. Publishes progress after every file so
    the dashboard shows a bar + a live list instead of freezing on one long request."""
    _job_begin("stage", _own_job)
    try:
        with _lock:
            if not _same_volume(C.RAW_ROOT, C.HOLDING_ROOT):
                err = ("HOLDING is on a different volume than RAW — a move would copy and "
                       "double space on a full NAS; aborting")
                _job_fail(err)
                return {"staged": 0, "gb": 0, "errors": [err], "error_count": 1, "total_staged": 0}
            d = {x["key"]: x for x in deletable() if x["exists"]}
            sel = _keys(keys, d.keys())
            log = _load_log(); moved = 0; bytes_ = 0; errors = []; done = 0; skipped = 0
            recent = []
            _pub(phase="staging", total=len(sel))
            for k in sel:
                if k in log["staged"]:
                    done += 1; skipped += 1
                    _pub(done=done, skipped=skipped); continue
                _pub(current=k)
                src = d[k]["src"]; dest = os.path.join(C.HOLDING_ROOT, k)
                if not _contained(dest, C.HOLDING_ROOT):
                    errors.append(f"{k}: dest escapes holding"); done += 1
                    _pub(done=done, errors=errors[-20:], error_count=len(errors)); continue
                if os.path.exists(dest):
                    errors.append(f"{k}: holding dest already exists — skipped"); done += 1; skipped += 1
                    _pub(done=done, skipped=skipped, errors=errors[-20:], error_count=len(errors)); continue
                if not _writable(src):
                    errors.append(f"{k}: source not writable (read-only mount?)"); done += 1
                    _pub(done=done, errors=errors[-20:], error_count=len(errors)); continue
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(src, dest)
                    log["staged"][k] = {"orig": src, "held": dest, "jpg": d[k]["jpg"],
                                        "no_archive": d[k].get("no_archive", False),
                                        "bytes": d[k]["src_bytes"], "ts": int(time.time())}
                    moved += 1; bytes_ += d[k]["src_bytes"]
                    recent.append({"key": k, "bytes": d[k]["src_bytes"]})
                    # checkpoint the log periodically: if the process dies mid-run the moved
                    # files are still recoverable (the log is the only recovery map).
                    if moved % 25 == 0:
                        _save_log(log)
                except Exception as e:
                    errors.append(f"{k}: {e}")
                done += 1
                _pub(done=done, ok=moved, bytes=bytes_, skipped=skipped,
                     recent=recent[-200:], errors=errors[-20:], error_count=len(errors))
            _save_log(log)
            _job_end(done, moved, bytes_, skipped, recent, errors)
            return {"staged": moved, "gb": round(bytes_ / 1e9, 2), "errors": errors[:20],
                    "error_count": len(errors), "total_staged": len(log["staged"])}
    except Exception as e:
        _job_fail(str(e))
        raise

STAT_PAGE_MAX = 2000   # above this, listing is a bulk dump — don't stat every row

def staged_list(limit=500, offset=0):
    """Staged files, oldest-staged first. `limit=0` returns them all — the dashboard asks for
    everything when the user wants to eyeball the whole holding set before purging.

    Only the rows actually returned are stat()ed for held_exists: checking all 25k entries to
    render 500 of them meant 25k SMB round trips per page load (and held _lock throughout).
    On a bulk dump the check is skipped entirely and held_exists comes back null."""
    with _lock:
        log = _load_log()
        rows = sorted(log["staged"].items(), key=lambda kv: (kv[1].get("ts", 0), kv[0]))
        offset = max(0, int(offset))
        page = rows[offset:] if not limit else rows[offset:offset + int(limit)]
        check = len(page) <= STAT_PAGE_MAX
        items = [{"key": k, **v,
                  "held_exists": os.path.isfile(v.get("held", "")) if check else None}
                 for k, v in page]
        return {"count": len(rows),
                "gb": round(sum(v.get("bytes", 0) for _, v in rows) / 1e9, 2),
                "offset": offset, "listed": len(items), "checked_on_disk": check,
                "items": items}

# ---------------- adopt orphaned holding files ----------------
def _eligible(rec, reviews, rel):
    """Same verdict deletable() reaches, minus 'the original is still in the RAW tree' —
    an adopted file is by definition no longer there. Returns (ok, reason, jpg, no_archive)."""
    st = rec.get("status")
    decision = reviews.get(rel, {}).get("decision")
    if decision == "manual":
        return False, "marked manual (you are hand-editing this RAW)", "", False
    approved = decision == "approve"
    no_archive = decision == "delete"
    if not (st == "PASS" or (st == "REVIEW" and approved) or no_archive):
        return False, f"status {st or '?'} and not approved — never verified", "", False
    jr = _jpg_rel(rec)
    jpg = os.path.join(C.ARCHIVE_ROOT, jr) if jr else ""
    if not no_archive and not (jpg and os.path.isfile(jpg) and os.path.getsize(jpg) > 1024):
        return False, "converted JPEG missing or truncated in the archive", jpg, no_archive
    return True, "", jpg, no_archive

# macOS/SMB/Synology leave these next to real files; they are not RAWs and reporting each
# one as "no manifest record" would bury the findings that matter.
def _junk(name):
    return name in (".DS_Store", "Thumbs.db", ".apdisk") or name.startswith("._")

def adopt(commit=False, _own_job=True):
    """Rebuild the recovery map for files already sitting in HOLDING that the log doesn't
    track — the state you get when a previous log was lost/rotated, or when files were moved
    into holding outside the app. Every file is re-verified against the manifest, the review
    decisions and the JPEG actually on disk before it is adopted; anything that fails stays
    put and is reported. Nothing is deleted here — adoption only makes Purge (and Recover)
    able to see these files, with the guards they normally apply.

    commit=False (default) is a dry run: identical checks, nothing written.

    The walk runs WITHOUT _lock (it only reads the filesystem); the lock is taken just for
    the merge at the end, so a scan of 25k files over SMB doesn't block the staged list."""
    _job_begin("adopt", _own_job)
    try:
        if not os.path.isdir(C.HOLDING_ROOT):
            msg = f"holding folder not found: {C.HOLDING_ROOT}"
            _job_fail(msg)
            return {"error": msg}
        by_rel = {r.get("src_rel"): r for r in store.read_manifest() if r.get("src_rel")}
        reviews = store.load_reviews()
        with _lock:                      # cheap snapshot of what is already tracked
            tracked = set(_load_log()["staged"])
        adopted, skipped, seen_bytes, ignored = [], [], 0, 0
        already = 0; scanned = 0
        _pub(phase="scanning holding folder")
        for dp, _, fns in os.walk(C.HOLDING_ROOT):
            for fn in fns:
                scanned += 1
                if scanned % 200 == 0:
                    _pub(done=scanned, ok=len(adopted), bytes=seen_bytes, skipped=len(skipped))
                if _junk(fn):
                    ignored += 1; continue
                held = os.path.join(dp, fn)
                rel = os.path.relpath(held, C.HOLDING_ROOT)
                if rel in tracked:
                    already += 1; continue
                if _bad_rel(rel) or not _contained(held, C.HOLDING_ROOT):
                    skipped.append({"key": rel, "why": "unsafe path"}); continue
                rec = by_rel.get(rel)
                if not rec:
                    skipped.append({"key": rel, "why": "no manifest record — cannot verify it was converted"})
                    continue
                ok, why, jpg, no_archive = _eligible(rec, reviews, rel)
                if not ok:
                    skipped.append({"key": rel, "why": why}); continue
                orig = os.path.join(C.RAW_ROOT, rel)
                if not _contained(orig, C.RAW_ROOT):
                    skipped.append({"key": rel, "why": "original path escapes the RAW root"}); continue
                if os.path.exists(orig):
                    # the RAW is back in place, so this holding copy is a duplicate, not a
                    # staged original — deleting it via Purge would be a different act.
                    skipped.append({"key": rel, "why": "original is back in the RAW tree — recover/remove by hand"})
                    continue
                try:
                    sz = os.path.getsize(held)
                    ts = int(os.path.getmtime(held))
                except OSError as e:
                    skipped.append({"key": rel, "why": f"unreadable in holding ({e})"}); continue
                seen_bytes += sz
                adopted.append({"key": rel, "orig": orig, "held": held, "jpg": jpg,
                                "no_archive": no_archive, "bytes": sz, "ts": ts})
        if commit:
            _pub(phase="writing the recovery log", current="")
            with _lock:
                log = _load_log()
                for a in adopted:
                    if a["key"] in log["staged"]:      # re-check: it may have been staged
                        already += 1; continue         # while we were walking
                    log["staged"][a["key"]] = {k: a[k] for k in
                                               ("orig", "held", "jpg", "no_archive", "bytes", "ts")}
                    log["staged"][a["key"]]["adopted"] = True
                _save_log(log)
                total_staged = len(log["staged"])
        else:
            with _lock:
                total_staged = len(_load_log()["staged"]) + len(adopted)
        reasons = {}
        for sk in skipped:
            reasons[sk["why"]] = reasons.get(sk["why"], 0) + 1
        res = {"committed": bool(commit), "adopted": len(adopted),
               "gb": round(seen_bytes / 1e9, 2), "already_tracked": already,
               "scanned": scanned, "ignored_system_files": ignored,
               "skipped": len(skipped), "skipped_reasons": reasons,
               "skipped_examples": skipped[:50], "total_staged": total_staged}
        _pub(state="done", phase="", current="", done=scanned, total=scanned,
             ok=len(adopted), bytes=seen_bytes, skipped=len(skipped),
             finished=int(time.time()), result=res)
        return res
    except Exception as e:
        _job_fail(str(e))
        raise

def recover(keys=None, _own_job=True):
    """Move staged originals back where they came from. Never overwrites a file already in
    place. Reports progress per file — recovering 20k RAWs off a NAS is not instant."""
    _job_begin("recover", _own_job)
    try:
        with _lock:
            log = _load_log(); back = 0; errors = []; done = 0; skipped = 0; bytes_ = 0
            recent = []
            sel = _keys(keys, log["staged"].keys())
            _pub(phase="recovering", total=len(sel))
            for k in sel:
                v = log["staged"].get(k)
                if not v:
                    done += 1; _pub(done=done); continue
                _pub(current=k)
                orig, held = v.get("orig", ""), v.get("held", "")
                if not (_contained(orig, C.RAW_ROOT) and _contained(held, C.HOLDING_ROOT)):
                    errors.append(f"{k}: stored path escapes its root — skipped"); done += 1
                    _pub(done=done, errors=errors[-20:], error_count=len(errors)); continue
                if os.path.exists(orig):
                    errors.append(f"{k}: original already present — not overwriting")
                    done += 1; skipped += 1
                    _pub(done=done, skipped=skipped, errors=errors[-20:], error_count=len(errors)); continue
                if not os.path.isfile(held):
                    # Only drop the entry if HOLDING is reachable and the file is genuinely gone.
                    # A transient NAS unmount makes isfile() False too — keep the entry then.
                    if not os.path.isdir(os.path.dirname(held)):
                        errors.append(f"{k}: holding unreachable — keeping recovery entry")
                        done += 1
                        _pub(done=done, errors=errors[-20:], error_count=len(errors)); continue
                    log["staged"].pop(k, None); done += 1; skipped += 1
                    _pub(done=done, skipped=skipped); continue   # reachable, file truly absent
                try:
                    os.makedirs(os.path.dirname(orig), exist_ok=True)
                    shutil.move(held, orig)
                    log["staged"].pop(k, None); back += 1
                    bytes_ += v.get("bytes", 0)
                    recent.append({"key": k, "bytes": v.get("bytes", 0)})
                    if back % 25 == 0:
                        _save_log(log)
                except Exception as e:
                    errors.append(f"{k}: {e}")
                done += 1
                _pub(done=done, ok=back, bytes=bytes_, skipped=skipped,
                     recent=recent[-200:], errors=errors[-20:], error_count=len(errors))
            _save_log(log)
            _job_end(done, back, bytes_, skipped, recent, errors)
            return {"recovered": back, "errors": errors[:20], "error_count": len(errors),
                    "total_staged": len(log["staged"])}
    except Exception as e:
        _job_fail(str(e))
        raise

def purge(confirm, _own_job=True):
    """Permanently delete everything staged. Irreversible, so it re-checks each entry against
    the current review decisions and path containment as it goes, and reports progress —
    deleting 20k files on a NAS takes minutes."""
    if confirm != "PURGE":
        return {"error": "confirm token must be exactly 'PURGE'"}
    _job_begin("purge", _own_job)
    try:
        with _lock:
            log = _load_log(); removed = 0; bytes_ = 0; errors = []; done = 0; skipped = 0
            recent = []
            reviews = store.load_reviews(); kept = _kept_jpg_set()
            items = list(log["staged"].items())
            _pub(phase="purging", total=len(items))
            for k, v in items:
                _pub(current=k)
                if reviews.get(k, {}).get("decision") == "manual":
                    errors.append(f"{k}: now marked manual — left staged, recover it")
                    done += 1; skipped += 1
                    _pub(done=done, skipped=skipped, errors=errors[-20:], error_count=len(errors))
                    continue
                held = v.get("held", "")
                if not _contained(held, C.HOLDING_ROOT):
                    errors.append(f"{k}: held escapes holding — skipped"); done += 1; skipped += 1
                    _pub(done=done, skipped=skipped, errors=errors[-20:], error_count=len(errors))
                    continue
                try:
                    n = 0
                    if os.path.isfile(held):
                        n = os.path.getsize(held); os.remove(held); bytes_ += n
                    # "Just Delete" items also remove the JPEG, but ONLY if the decision is still
                    # 'delete' AND no other kept photo shares that JPEG (stem-collision guard).
                    jpg = v.get("jpg", "")
                    still_delete = reviews.get(k, {}).get("decision") == "delete"
                    if (v.get("no_archive") and still_delete and jpg and _contained(jpg, C.ARCHIVE_ROOT)
                            and os.path.realpath(jpg) not in kept and os.path.isfile(jpg)):
                        os.remove(jpg)
                    log["staged"].pop(k, None); removed += 1
                    recent.append({"key": k, "bytes": n})
                    if removed % 25 == 0:
                        _save_log(log)
                except Exception as e:
                    errors.append(f"{k}: {e}")
                done += 1
                _pub(done=done, ok=removed, bytes=bytes_, skipped=skipped,
                     recent=recent[-200:], errors=errors[-20:], error_count=len(errors))
            _save_log(log)
            _pub(phase="tidying empty folders", current="")
            for dp, _, _ in os.walk(C.HOLDING_ROOT, topdown=False):
                if os.path.realpath(dp) != os.path.realpath(C.HOLDING_ROOT) and not os.listdir(dp):
                    try: os.rmdir(dp)
                    except Exception: pass
            _job_end(done, removed, bytes_, skipped, recent, errors)
            return {"purged": removed, "freed_gb": round(bytes_ / 1e9, 2), "errors": errors[:20],
                    "error_count": len(errors), "total_staged": len(log["staged"])}
    except Exception as e:
        _job_fail(str(e))
        raise
