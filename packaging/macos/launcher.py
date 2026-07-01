#!/usr/bin/env python3
"""CullStation.app entry point — macOS menu-bar launcher.

Runs the FastAPI dashboard IN-PROCESS (uvicorn on a background thread — NOT a child
process, which wouldn't work inside a PyInstaller bundle) and shows live status plus
quick actions in the menu bar. Bundled into CullStation.app by CullStation.spec.

Dev run:  pip install rumps uvicorn fastapi pillow ;  python3 packaging/macos/launcher.py
"""
import os, sys, json, time, threading, subprocess, urllib.request, webbrowser

# ── resolve bundled resources, and set env BEFORE importing the app ──────────────────
# config.py reads CULL_* env at import time, so everything here must run first.
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# Let the app import `app`/`config`/`video`. Frozen: BASE is the bundle root. Dev: also add the repo root.
_paths = [BASE]
if not getattr(sys, "_MEIPASS", None):
    _paths.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
for p in _paths:
    if p not in sys.path:
        sys.path.insert(0, p)

# Bundled static ffmpeg/ffprobe ship next to the launcher inside the .app (added by the spec).
for _name, _env in (("ffmpeg", "CULL_FFMPEG"), ("ffprobe", "CULL_FFPROBE")):
    _p = os.path.join(BASE, _name)
    if os.path.isfile(_p) and not os.environ.get(_env):
        os.environ[_env] = _p
        try: os.chmod(_p, 0o755)        # best-effort; the build already marks them executable
        except OSError: pass

PORT = os.environ.setdefault("CULL_PORT", "8765")
BASE_URL = f"http://127.0.0.1:{PORT}"
NAS_HOST = os.environ.get("CULL_NAS_HOST", "192.168.1.100")
NAS_SHARE = os.environ.get("CULL_NAS_SHARE", "Marketing Media")

from app import app as fastapi_app   # noqa: E402  (must follow the env setup above)
import uvicorn                       # noqa: E402

try:
    import rumps                     # noqa: E402
except Exception:
    rumps = None


class _Server:
    """uvicorn on a daemon thread. uvicorn skips signal-handler install off the main
    thread, so this is safe; the menu-bar UI owns the main (AppKit) thread."""
    def __init__(self):
        self._cfg = uvicorn.Config(fastapi_app, host="127.0.0.1", port=int(PORT), log_level="warning")
        self.server = uvicorn.Server(self._cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self): self.thread.start()
    def stop(self):  self.server.should_exit = True


def _req(path, method="GET", timeout=4):
    r = urllib.request.Request(BASE_URL + path, method=method, headers={"X-CullStation": "1"})
    return urllib.request.urlopen(r, timeout=timeout)


def _wait_up(timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            _req("/api/status", timeout=1); return True
        except Exception:
            time.sleep(0.3)
    return False


def _open_dashboard():
    webbrowser.open(BASE_URL)


def _mount_share():
    # Finder mount: prompts for credentials once, then remembered in the login Keychain.
    subprocess.Popen(["open", f"smb://{NAS_HOST}/{NAS_SHARE}"])


if rumps:
    class CullApp(rumps.App):
        def __init__(self):
            super().__init__("CullStation", title="🎞️", quit_button=None)
            self.srv = _Server(); self.srv.start()
            self._status = None            # latest /api/status — fetched OFF the main thread
            self.m_status = rumps.MenuItem("Starting…")
            self.m_mounts = rumps.MenuItem("Mounts: …")
            self.m_video = rumps.MenuItem("Video: …")
            self.menu = ["Open Dashboard", None,
                         self.m_status, self.m_mounts, self.m_video, None,
                         "Mount NAS Share…", None,
                         "Start Video", "Pause Video", None, "Quit"]
            # Network/disk polling on a worker thread; the Timer only RENDERS the cached value
            # on the main thread — so a stalled NAS can never freeze the menu bar.
            threading.Thread(target=self._poll, daemon=True).start()
            self._timer = rumps.Timer(self._render, 2); self._timer.start()   # retained, or it'd be GC'd
            threading.Thread(target=self._open_when_ready, daemon=True).start()

        def _open_when_ready(self):
            if _wait_up():
                _open_dashboard()

        def _poll(self):
            while True:
                try: self._status = json.load(_req("/api/status", timeout=3))
                except Exception: self._status = None
                time.sleep(5)

        def _render(self, _):
            s = self._status
            if not s:
                self.title = "🎞️⚠"; self.m_status.title = "Server starting / unreachable…"; return
            try:
                p, v = s["photos"], s["video"]
                self.title = "🎞️"
                self.m_status.title = f"Photos: {p.get('done',0):,} done · {p.get('review',0)} to review"
                ok = bool(s.get("mounts")) and all(m["ok"] for m in s["mounts"].values())
                self.m_mounts.title = "Mounts: ✓ connected" if ok else "Mounts: ✗ share not mounted"
                vs = "paused" if v.get("paused") else ("running" if v.get("running") else "idle")
                self.m_video.title = f"Video: {vs} · {v.get('ok',0)} done · {v.get('reclaimed_gb',0)} GB reclaimed"
            except Exception:
                pass

        @rumps.clicked("Open Dashboard")
        def _open(self, _): _open_dashboard()

        @rumps.clicked("Mount NAS Share…")
        def _mount(self, _): _mount_share()

        @rumps.clicked("Start Video")
        def _start(self, _):
            try: _req("/api/video/start", "POST")
            except Exception: pass

        @rumps.clicked("Pause Video")
        def _pause(self, _):
            try: _req("/api/video/pause", "POST")
            except Exception: pass

        @rumps.clicked("Quit")
        def _quit(self, _):
            try: self.srv.stop()
            except Exception: pass
            rumps.quit_application()


def main():
    if rumps is None:    # headless fallback (dev without rumps): serve + open the browser
        srv = _Server(); srv.start()
        if _wait_up(): _open_dashboard()
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            srv.stop()
        return
    CullApp().run()


if __name__ == "__main__":
    main()
