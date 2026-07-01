# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds CullStation.app (menu-bar, Apple Silicon).

Run from packaging/macos/ via build_app.sh, or directly:
    pyinstaller CullStation.spec --noconfirm
Expects the static ffmpeg/ffprobe in ./vendor/ (see fetch_ffmpeg.sh).
"""
import os
from PyInstaller.utils.hooks import collect_submodules

HERE = os.path.abspath(os.getcwd())                       # packaging/macos (cwd when invoked)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))    # CullStation/  (app.py, config.py, static/)
VENDOR = os.path.join(HERE, "vendor")
VERSION = os.environ.get("CULL_VERSION", "1.0.0")
ICON = os.path.join(VENDOR, "CullStation.icns")           # optional; ignored if absent

# Bundled binaries land at the bundle's resource root (sys._MEIPASS at runtime), where
# launcher.py + app.py look for them ("ffmpeg"/"ffprobe", and the "static" dir).
binaries = []
for b in ("ffmpeg", "ffprobe"):
    p = os.path.join(VENDOR, b)
    if os.path.isfile(p):
        binaries.append((p, "."))
if len(binaries) < 2:   # fail loud, never ship an app that silently can't transcode
    raise SystemExit("vendor/ffmpeg + vendor/ffprobe missing — run ./fetch_ffmpeg.sh first")

datas = [(os.path.join(ROOT, "static"), "static")]

# uvicorn lazily imports its loop/protocol/lifespan backends by string — collect them all
# or the server fails to start inside the frozen app. anyio/pydantic also string-import
# backends; pydantic_core is a compiled .so. rumps pulls in PyObjC (has its own hooks).
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("anyio")
    + collect_submodules("pydantic")
    + collect_submodules("rumps")
    + ["pydantic_core", "fastapi", "starlette", "click", "h11",
       "config", "store", "video", "deletion", "app"]
)

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[ROOT, HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide6", "matplotlib", "numpy.testing"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CullStation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # GUI app: no terminal window
    target_arch=os.environ.get("CULL_ARCH") or None,   # None = build for THIS Mac's arch (arm64 or x86_64)
    codesign_identity=None,   # signing is done explicitly in build_app.sh (deep, ordered)
    entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="CullStation",
)
app = BUNDLE(
    coll,
    name="CullStation.app",
    icon=ICON if os.path.isfile(ICON) else None,
    bundle_identifier="com.skypark.cullstation",
    version=VERSION,
    info_plist={
        "CFBundleName": "CullStation",
        "CFBundleDisplayName": "CullStation",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,            # menu-bar only — no Dock icon
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
    },
)
