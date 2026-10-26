"""Hardening for every ffmpeg / ffprobe input DescTrack opens.

ffmpeg normally guesses a file's format from its contents, so a file that
merely *claims* to be a video can steer ffmpeg into any of its hundreds of
demuxers - including rarely used ones with known memory-safety bugs (for
example CVE-2026-64830, a heap overflow in the VobSub demuxer). Playlist-style
formats (HLS, concat, ...) can also make ffmpeg open network URLs or other
local files.

Every input is therefore opened with:

* ``-protocol_whitelist file`` - local files only, no http/tcp/pipe/etc.
* ``-format_whitelist ...`` - only the container formats DescTrack actually
  supports for video and AD audio.
* an absolute path, so a file name can never be mistaken for an option
  (``-something``) or a protocol (``name:...``).
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from typing import Optional

# ffmpeg demuxer names (not file extensions). Each covers several
# extensions, e.g. "mov" = .mp4 .m4v .mov .m4a .3gp, "matroska" = .mkv .webm
# .mka, "asf" = .wmv .wma, "mpeg" = DVD .vob/.mpg, "mpegts" = .ts/.m2ts.
ALLOWED_FORMATS = ",".join([
    "mov", "matroska", "avi", "asf", "mpeg", "mpegts", "flv", "ogg",
    "mp3", "wav", "w64", "aiff", "aac", "flac",
])

# ffmpeg releases older than this have publicly known high-severity bugs
# that a crafted media file can trigger (e.g. CVE-2026-64830 and
# CVE-2026-75141, both fixed in 9.0.1).
RECOMMENDED_MIN_VERSION = (9, 0, 1)


def input_args(path: str) -> list[str]:
    """Options + ``-i path`` for one hardened ffmpeg input."""
    return [
        "-protocol_whitelist", "file",
        "-format_whitelist", ALLOWED_FORMATS,
        "-i", os.path.abspath(path),
    ]


def probe_input_args(path: str) -> list[str]:
    """Same hardening for ffprobe, which takes the path without ``-i``."""
    return [
        "-protocol_whitelist", "file",
        "-format_whitelist", ALLOWED_FORMATS,
        os.path.abspath(path),
    ]


@lru_cache(maxsize=8)
def get_version(ffmpeg_path: str) -> Optional[tuple[int, ...]]:
    """Returns ffmpeg's release version, e.g. (9, 0, 1), or None when it
    can't be told (such as a git snapshot build, which is newer than any
    release)."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-version"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = (result.stdout or "").splitlines()[0:1]
    if not first_line:
        return None
    # "ffmpeg version 9.0.1-essentials_build-www.gyan.dev ..."
    # "ffmpeg version n9.0.2 ..." / "ffmpeg version 6.1.1-3ubuntu5 ..."
    match = re.match(r"\S+ version n?(\d+)\.(\d+)(?:\.(\d+))?", first_line[0])
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def is_outdated(ffmpeg_path: str) -> bool:
    version = get_version(ffmpeg_path)
    return version is not None and version < RECOMMENDED_MIN_VERSION
