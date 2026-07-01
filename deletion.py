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

def preview():
    present = [x for x in deletable() if x["exists"]]
    return {"count": len(present), "bytes": sum(x["src_bytes"] for x in present),
            "gb": round(sum(x["src_bytes"] for x in present) / 1e9, 2),
            "pass": sum(1 for x in present if x["status"] == "PASS"),
            "approved_review": sum(1 for x in present if x["status"] == "REVIEW"),
            "items": present[:500]}

# ---------------- two-phase delete ----------------
def stage(keys=None):
    with _lock:
        if not _same_volume(C.RAW_ROOT, C.HOLDING_ROOT):
            return {"staged": 0, "gb": 0, "errors": ["HOLDING is on a different volume than RAW "
                    "— a move would copy and double space on a full NAS; aborting"], "total_staged": 0}
        d = {x["key"]: x for x in deletable() if x["exists"]}
        sel = _keys(keys, d.keys())
        log = _load_log(); moved = 0; bytes_ = 0; errors = []
        for k in sel:
            if k in log["staged"]:
                continue
            src = d[k]["src"]; dest = os.path.join(C.HOLDING_ROOT, k)
            if not _contained(dest, C.HOLDING_ROOT):
                errors.append(f"{k}: dest escapes holding"); continue
            if os.path.exists(dest):
                errors.append(f"{k}: holding dest already exists — skipped"); continue
            if not _writable(src):
                errors.append(f"{k}: source not writable (read-only mount?)"); continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                log["staged"][k] = {"orig": src, "held": dest, "jpg": d[k]["jpg"],
                                    "no_archive": d[k].get("no_archive", False),
                                    "bytes": d[k]["src_bytes"], "ts": int(time.time())}
                moved += 1; bytes_ += d[k]["src_bytes"]
            except Exception as e:
                errors.append(f"{k}: {e}")
        _save_log(log)
        return {"staged": moved, "gb": round(bytes_ / 1e9, 2), "errors": errors[:20],
                "total_staged": len(log["staged"])}

def staged_list():
    with _lock:
        log = _load_log()
        items = [{"key": k, **v, "held_exists": os.path.isfile(v.get("held", ""))}
                 for k, v in log["staged"].items()]
        return {"count": len(items), "gb": round(sum(i.get("bytes", 0) for i in items) / 1e9, 2),
                "items": items[:500]}

def recover(keys=None):
    with _lock:
        log = _load_log(); back = 0; errors = []
        for k in _keys(keys, log["staged"].keys()):
            v = log["staged"].get(k)
            if not v:
                continue
            orig, held = v.get("orig", ""), v.get("held", "")
            if not (_contained(orig, C.RAW_ROOT) and _contained(held, C.HOLDING_ROOT)):
                errors.append(f"{k}: stored path escapes its root — skipped"); continue
            if os.path.exists(orig):
                errors.append(f"{k}: original already present — not overwriting"); continue
            if not os.path.isfile(held):
                # Only drop the entry if HOLDING is reachable and the file is genuinely gone.
                # A transient NAS unmount makes isfile() False too — keep the entry then.
                if not os.path.isdir(os.path.dirname(held)):
                    errors.append(f"{k}: holding unreachable — keeping recovery entry"); continue
                log["staged"].pop(k, None); continue   # reachable, file truly absent
            try:
                os.makedirs(os.path.dirname(orig), exist_ok=True)
                shutil.move(held, orig); log["staged"].pop(k, None); back += 1
            except Exception as e:
                errors.append(f"{k}: {e}")
        _save_log(log)
        return {"recovered": back, "errors": errors[:20], "total_staged": len(log["staged"])}

def purge(confirm):
    if confirm != "PURGE":
        return {"error": "confirm token must be exactly 'PURGE'"}
    with _lock:
        log = _load_log(); removed = 0; bytes_ = 0; errors = []
        reviews = store.load_reviews(); kept = _kept_jpg_set()
        for k, v in list(log["staged"].items()):
            if reviews.get(k, {}).get("decision") == "manual":
                errors.append(f"{k}: now marked manual — left staged, recover it"); continue
            held = v.get("held", "")
            if not _contained(held, C.HOLDING_ROOT):
                errors.append(f"{k}: held escapes holding — skipped"); continue
            try:
                if os.path.isfile(held):
                    bytes_ += os.path.getsize(held); os.remove(held)
                # "Just Delete" items also remove the JPEG, but ONLY if the decision is still
                # 'delete' AND no other kept photo shares that JPEG (stem-collision guard).
                jpg = v.get("jpg", "")
                still_delete = reviews.get(k, {}).get("decision") == "delete"
                if (v.get("no_archive") and still_delete and jpg and _contained(jpg, C.ARCHIVE_ROOT)
                        and os.path.realpath(jpg) not in kept and os.path.isfile(jpg)):
                    os.remove(jpg)
                log["staged"].pop(k, None); removed += 1
            except Exception as e:
                errors.append(f"{k}: {e}")
        _save_log(log)
        for dp, _, _ in os.walk(C.HOLDING_ROOT, topdown=False):
            if os.path.realpath(dp) != os.path.realpath(C.HOLDING_ROOT) and not os.listdir(dp):
                try: os.rmdir(dp)
                except Exception: pass
        return {"purged": removed, "freed_gb": round(bytes_ / 1e9, 2), "errors": errors[:20]}
