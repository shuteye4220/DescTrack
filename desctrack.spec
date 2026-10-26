# PyInstaller build spec for DescTrack - works on Windows, macOS and Linux.
#
#   pyinstaller desctrack.spec
#
# Output:
#   Windows -> dist/DescTrack.exe        (single file)
#   Linux   -> dist/DescTrack            (single file)
#   macOS   -> dist/DescTrack.app        (app bundle)
#
# ffmpeg / ffprobe are NOT bundled; see README.md.

import sys

block_cipher = None

hiddenimports = [
    "services.ffmpeg_locator",
    "services.media_probe",
    "services.audio_sync",
    "services.preview",
    "services.mux",
    "services.silence",
]
hooksconfig = {}

if sys.platform.startswith("linux"):
    # pywebview picks its GUI backend at runtime, so PyInstaller can't see
    # the GTK / WebKitGTK imports on its own. GTK is preferred over Qt on
    # Linux because WebKitGTK exposes the page to Orca via AT-SPI.
    hiddenimports += [
        "webview.platforms.gtk",
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.GLib",
        "gi.repository.Gio",
        "gi.repository.WebKit2",
    ]
    hooksconfig["gi"] = {"module-versions": {"Gtk": "3.0", "Gdk": "3.0", "WebKit2": "4.1"}}
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
elif sys.platform == "win32":
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium"]

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=[],
    datas=[("static", "static")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig=hooksconfig,
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "darwin":
    # macOS: a proper .app bundle (one-folder inside the bundle, which is
    # what PyInstaller recommends for .app builds).
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="DescTrack",
        console=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="DescTrack", upx=False)
    app = BUNDLE(
        coll,
        name="DescTrack.app",
        bundle_identifier="io.github.shuteye4220.desctrack",
        info_plist={
            "CFBundleName": "DescTrack",
            "CFBundleDisplayName": "DescTrack",
            "CFBundleShortVersionString": "1.2",
            "CFBundleVersion": "1.2",
            "NSHighResolutionCapable": True,
        },
    )
else:
    # Windows / Linux: a single self-contained executable.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="DescTrack",
        console=False,
        upx=False,
    )
