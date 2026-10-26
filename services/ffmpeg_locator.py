"""Finds ffmpeg / ffprobe executables.

DescTrack does not bundle ffmpeg itself (licensing + binary size); it looks
for a portable copy sitting next to the app first, then falls back to PATH
and the usual package-manager install locations.
See README.md for where to download ffmpeg on each platform.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Apps launched from the macOS Finder (or some Linux desktop launchers) get a
# minimal PATH that leaves out Homebrew / MacPorts / snap, so check the
# common install folders explicitly as a last resort.
_EXTRA_SEARCH_DIRS = [
    "/opt/homebrew/bin",   # Homebrew on Apple Silicon
    "/usr/local/bin",      # Homebrew on Intel Macs, manual installs
    "/opt/local/bin",      # MacPorts
    "/snap/bin",           # Ubuntu snap
    "/usr/bin",
]


def _app_dir() -> Path:
    """Directory the running app lives in (handles PyInstaller's onefile
    temp-extraction layout by preferring sys.executable's folder)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _app_dirs() -> list[Path]:
    """Folders to check for a portable ffmpeg, most specific first."""
    app_dir = _app_dir()
    dirs = [app_dir]
    # In a macOS .app bundle the executable lives in
    # DescTrack.app/Contents/MacOS; also look in the folder that holds
    # DescTrack.app, which is where a user would naturally drop ffmpeg.
    if sys.platform == "darwin" and app_dir.name == "MacOS" and app_dir.parent.name == "Contents":
        dirs.append(app_dir.parent.parent.parent)
    return dirs


def _exe_name(base: str) -> str:
    return f"{base}.exe" if os.name == "nt" else base


def _find(base_name: str) -> str | None:
    exe_name = _exe_name(base_name)

    candidates = []
    for app_dir in _app_dirs():
        candidates.append(app_dir / exe_name)
        candidates.append(app_dir / "ffmpeg" / exe_name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found_on_path = shutil.which(exe_name) or shutil.which(base_name)
    if found_on_path:
        return found_on_path

    if os.name != "nt":
        for directory in _EXTRA_SEARCH_DIRS:
            candidate = Path(directory) / exe_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def find_ffmpeg() -> str | None:
    return _find("ffmpeg")


def find_ffprobe() -> str | None:
    return _find("ffprobe")
