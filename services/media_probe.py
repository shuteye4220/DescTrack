"""Media duration probing via ffprobe."""
from __future__ import annotations

import subprocess


class ProbeError(RuntimeError):
    pass


def get_duration_seconds(ffprobe_path: str, media_path: str) -> float:
    args = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        media_path,
    ]
    result = subprocess.run(args, capture_output=True, text=True, errors="replace")
    text = (result.stdout or "").strip()
    try:
        seconds = float(text)
    except ValueError:
        raise ProbeError(
            f'Could not determine duration for "{media_path}". ffprobe said:\n{result.stderr}'
        )
    if result.returncode != 0:
        raise ProbeError(
            f'ffprobe exited with code {result.returncode} for "{media_path}":\n{result.stderr}'
        )
    return seconds
