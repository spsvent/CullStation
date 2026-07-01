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
c, _ = call("/api/video")
check("video summary 200", c == 200)

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
