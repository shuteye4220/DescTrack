"""Media duration probing via ffprobe."""
from __future__ import annotations

import subprocess

from .ffmpeg_safety import probe_input_args


class ProbeError(RuntimeError):
    pass


def get_duration_seconds(ffprobe_path: str, media_path: str) -> float:
    args = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        *probe_input_args(media_path),
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise ProbeError(f'Timed out reading "{media_path}".')
    text = (result.stdout or "").strip()
    try:
        seconds = float(text)
        if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
            raise ValueError(text)
    except ValueError:
        raise ProbeError(
            f'Could not determine duration for "{media_path}". ffprobe said:\n{result.stderr}'
        )
    if result.returncode != 0:
        raise ProbeError(
            f'ffprobe exited with code {result.returncode} for "{media_path}":\n{result.stderr}'
        )
    return seconds
