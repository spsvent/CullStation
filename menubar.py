#!/usr/bin/env python3
"""Optional macOS menu-bar launcher for CullStation.

Starts the dashboard server and shows live cull status right in the menu bar,
with quick actions. macOS only.

    pip install rumps
    python3 menubar.py        # (run.sh starts the plain web server instead)
"""
import os, sys, json, subprocess, urllib.request, webbrowser

try:
    import rumps
except ImportError:
    sys.exit("menubar needs rumps:  pip install rumps   (macOS only)")

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get("CULL_PORT", "8765")
BASE = f"http://127.0.0.1:{PORT}"

def _req(path, method="GET"):
    r = urllib.request.Request(BASE + path, method=method, headers={"X-CullStation": "1"})
    return urllib.request.urlopen(r, timeout=4)

class CullApp(rumps.App):
    def __init__(self):
        super().__init__("🗄️ …", quit_button=None)
        # launch the web server as a child; keep the Mac awake handled by the worker
        env = dict(os.environ, CULL_PORT=PORT)
        self.server = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py")], env=env)
        self.m_photos = rumps.MenuItem("Photos: starting…")
        self.m_video = rumps.MenuItem("Video: …")
        self.menu = ["Open dashboard", None, self.m_photos, self.m_video, None,
                     "Start video", "Pause video", None, "Quit"]
        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()

    def refresh(self, _):
        try:
            s = json.load(_req("/api/status"))
            p, v = s["photos"], s["video"]
            self.title = f"🗄️ {p['done']:,}/{(p['target'] or 0)//1000}k"
            self.m_photos.title = f"Photos: {p['done']:,} done · {p['review']} to review"
            vs = "paused" if v["paused"] else ("running" if v["running"] else "idle")
            self.m_video.title = f"Video: {vs} · {v.get('ok', 0)} done · {v.get('reclaimed_gb', 0)} GB"
        except Exception:
            self.title = "🗄️ ⚠ offline"

    @rumps.clicked("Open dashboard")
    def open_dash(self, _):
        webbrowser.open(BASE)

    @rumps.clicked("Start video")
    def start_video(self, _):
        try: _req("/api/video/start", "POST")
        except Exception: pass

    @rumps.clicked("Pause video")
    def pause_video(self, _):
        try: _req("/api/video/pause", "POST")
        except Exception: pass

    @rumps.clicked("Quit")
    def quit_app(self, _):
        try: self.server.terminate()
        except Exception: pass
        rumps.quit_application()

if __name__ == "__main__":
    CullApp().run()
