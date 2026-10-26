"""Detects how much leading silence a media file has, so the sync-mixer
preview can default to a position with actual audible content in both
files instead of starting at 0:00 (often just dead air / title cards).

Uses ffmpeg's built-in `silencedetect` filter rather than guessing a fixed
offset - it actually measures each file.
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")

# If the whole scanned window is silent (no silence_end found), fall back to
# this many seconds in, capped at the scan window.
_FALLBACK_SECONDS = 60.0


def detect_content_start_seconds(
    ffmpeg_path: str,
    media_path: str,
    max_scan_seconds: float = 180.0,
    noise_floor_db: float = -35.0,
    min_silence_duration: float = 0.5,
) -> float:
    """Returns the estimated timestamp (seconds) where real audio content
    begins. 0.0 if the file doesn't open with silence at all."""
    args = [
        ffmpeg_path, "-y",
        "-i", media_path,
        "-t", str(max_scan_seconds),
        "-af", f"silencedetect=noise={noise_floor_db}dB:d={min_silence_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(args, capture_output=True, text=True, errors="replace")
    stderr = result.stderr or ""

    first_start = _first_match(_SILENCE_START_RE, stderr)
    first_end = _first_match(_SILENCE_END_RE, stderr)

    if first_start is None:
        return 0.0

    if abs(first_start) > 0.25:
        # The first silence begins after some audible content, so the file
        # does not open with silence.
        return 0.0

    if first_end is not None:
        # The file opens with silence that ends at first_end - that's where
        # content starts. (Bounded by the scan window, which is where
        # ffmpeg reports the end of a silence that runs off the clip.)
        return min(first_end, max_scan_seconds)

    # Silence from the very start through the whole scanned window - fall
    # back to a fixed offset rather than 0:00.
    return min(_FALLBACK_SECONDS, max_scan_seconds)


def _first_match(pattern: re.Pattern, text: str) -> Optional[float]:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
