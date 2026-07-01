"""Data layer: cached manifest reads, review decisions, NAS free space, conversion ETA."""
import os, json, time, threading
from collections import deque, Counter
import config as C

_lock = threading.Lock()
_reviews_lock = threading.RLock()   # guards the read-modify-write of REVIEW_FILE
_cache = {"mtime": None, "size": None, "recs": []}
_rate = deque(maxlen=180)   # (timestamp, done_count) samples for ETA
_rate_last_t = 0.0          # throttle: one sample per window regardless of #pollers

def _write_json(path, obj):
    """Atomic, durable JSON write (temp + fsync file + rename + fsync dir)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:   # make the rename itself durable
        dfd = os.open(d, os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    except Exception:
        pass

def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out

def read_manifest():
    """Merge the main manifest + the convert shard + the re-convert shard (each has a single
    writer, so no NFS torn-line race). Re-parse only when a file changes; dedupe by src_rel,
    read order = main, convert, re-convert, so the newest wins (a re-developed-darker version
    beats a fresh conversion, which beats an original/SKIP line)."""
    files = [C.MANIFEST, C.CONVERT_MANIFEST, C.RECONVERT_MANIFEST]
    key = []
    for p in files:
        try:
            st = os.stat(p); key.append((st.st_mtime, st.st_size))
        except FileNotFoundError:
            key.append(None)
    with _lock:
        if _cache.get("key") == key:
            return _cache["recs"]
    recs = []
    for p in files:                     # main first, then the re-convert shard (so it wins)
        recs += _read_jsonl(p)
    seen = {}
    for i, r in enumerate(recs):
        seen[r.get("src_rel") or f"__norel_{i}"] = r
    recs = list(seen.values())
    with _lock:
        _cache.update(key=key, recs=recs)
    return recs

def load_reviews():
    if os.path.exists(C.REVIEW_FILE):
        try:
            with open(C.REVIEW_FILE) as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}

def save_reviews(d):
    _write_json(C.REVIEW_FILE, d)

def _load_reviews_safe():
    """Load decisions; if the file exists but is unparseable, PRESERVE it (back up) and
    start fresh rather than silently wiping irreplaceable review decisions."""
    if not os.path.exists(C.REVIEW_FILE):
        return {}
    try:
        with open(C.REVIEW_FILE) as fh:
            return json.load(fh)
    except Exception:
        bak = C.REVIEW_FILE + f".corrupt-{int(time.time())}"
        try:
            os.replace(C.REVIEW_FILE, bak)
            print(f"[store] corrupt reviews preserved at {bak}; starting fresh")
        except Exception:
            pass
        return {}

def apply_review(keys, decision):
    """Atomic, lock-guarded read-modify-write so concurrent reviewer clicks can't
    drop each other's decisions. keys: str or iterable; decision: approve|reject|clear."""
    if isinstance(keys, str):
        keys = [keys]
    with _reviews_lock:
        d = _load_reviews_safe()
        for k in keys:
            if decision == "clear":
                d.pop(k, None)
            else:
                d[k] = {"decision": decision, "ts": int(time.time())}
        save_reviews(d)
        return len(d)

def free_space(path):
    try:
        s = os.statvfs(path)
        free = s.f_bavail * s.f_frsize
        total = s.f_blocks * s.f_frsize
        return {"free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1),
                "used_pct": round(100 * (1 - free / total), 1) if total else None}
    except Exception:
        return {"free_gb": None, "total_gb": None, "used_pct": None}

def eta(done, target):
    """Rolling-rate ETA from recent samples (sampled at most once / 15s so multiple
    pollers or browser tabs don't collapse the window)."""
    global _rate_last_t
    now = time.time()
    if now - _rate_last_t >= 15:
        _rate.append((now, done)); _rate_last_t = now
    if len(_rate) < 2 or not target or done >= target:
        return {"rate_per_min": None, "eta_min": None}
    t0, d0 = _rate[0]
    dt = now - t0
    dd = done - d0
    if dt < 30 or dd <= 0:
        return {"rate_per_min": None, "eta_min": None}
    rpm = dd / (dt / 60)
    remaining = max(0, target - done)
    return {"rate_per_min": round(rpm, 1), "eta_min": round(remaining / rpm) if rpm else None}

def photo_stats():
    recs = read_manifest()
    cnt = Counter(r.get("status") for r in recs)
    reviews = load_reviews()
    keys = {r.get("src_rel") for r in recs}
    reviewed = sum(1 for k in reviews if k in keys)   # ignore orphaned decisions
    insz = sum(r.get("src_size", 0) for r in recs if r.get("status") != "SKIP")
    outsz = sum(r.get("out_size", 0) for r in recs if r.get("out_size"))
    done = len(recs)
    return {
        "done": done, "target": C.PHOTO_TARGET,
        "pass": cnt.get("PASS", 0), "review": cnt.get("REVIEW", 0),
        "fail": cnt.get("FAIL", 0), "skip": cnt.get("SKIP", 0),
        "reviewed": reviewed,
        "raw_gb": round(insz / 1e9, 1), "jpeg_gb": round(outsz / 1e9, 2),
        "reclaimable_gb": round((insz - outsz) / 1e9, 1),
        **eta(done, C.PHOTO_TARGET),
    }
