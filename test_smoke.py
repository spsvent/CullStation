#!/usr/bin/env python3
"""CullStation smoke test — validates a running server.
Usage:  python3 test_smoke.py [http://127.0.0.1:8765]"""
import sys, json, urllib.request, urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
ok = bad = 0

def call(path, method="GET", body=None, headers=None, expect=200):
    h = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    if data: h["content-type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=8)
        return r.getcode(), json.load(r) if "json" in (r.headers.get("content-type") or "") else None
    except urllib.error.HTTPError as e:
        return e.code, None

def check(name, cond):
    global ok, bad
    print(("  ok  " if cond else " FAIL ") + name)
    ok += cond; bad += (not cond)

print(f"CullStation smoke test → {BASE}")
c, s = call("/api/status")
check("status 200", c == 200)
check("status has mounts/photos/nas/video", bool(s) and all(k in s for k in ("mounts", "photos", "nas", "video")))
check("photos has counts", bool(s) and all(k in s["photos"] for k in ("done", "pass", "review", "fail")))

c, st = call("/api/selftest")
check("selftest 200", c == 200)
check("ffmpeg/ffprobe present", bool(st) and st.get("ffmpeg") and st.get("ffprobe"))

c, _ = call("/api/review", "POST", {"key": "x", "decision": "approve"})
check("CSRF: POST without header → 403", c == 403)
c, _ = call("/api/photos?status=PASS&limit=1")
check("photos list 200", c == 200)

c, _ = call("/api/img?path=/etc/passwd")
check("img path traversal blocked (403)", c == 403)
c, _ = call("/api/deletion/preview")
check("deletion preview 200", c == 200)
c, sp = call("/api/deletion/job")
check("job progress 200 + shape", c == 200 and bool(sp) and all(k in sp for k in ("kind", "state", "done", "total", "pct")))
c, _ = call("/api/deletion/job/start", "POST", {"kind": "purge", "confirm": "PURGE"})
check("CSRF: job start without header → 403", c == 403)
c, st = call("/api/deletion/staged?limit=1")
check("staged list paged", c == 200 and bool(st) and "checked_on_disk" in st)
c, _ = call("/api/deletion/stage/start", "POST", {})
check("CSRF: stage start without header → 403", c == 403)
c, _ = call("/api/deletion/adopt", "POST", {})   # no header → must not even walk the tree
check("CSRF: adopt without header → 403", c == 403)
c, _ = call("/api/video")
check("video summary 200", c == 200)
c, vf = call("/api/video/failed")
check("video failed list 200", c == 200 and vf is not None and "items" in vf)
c, dg = call("/api/video/diagnose?rel=../../etc/passwd")
check("diagnose path traversal refused", c == 200 and bool(dg) and "error" in dg)
c, _ = call("/api/video/retry", "POST", {"rel": "x.mov"})
check("CSRF: video retry without header → 403", c == 403)

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
