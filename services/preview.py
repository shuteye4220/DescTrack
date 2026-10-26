"""Extracts short WAV clips (via ffmpeg) from the video's own audio and from
the AD audio, at whatever positions correspond to the currently chosen sync
offset. The frontend plays them back with the Web Audio API, scheduled to
start at the same instant, so a blind user can listen for drift before
committing to the final render.

Because the extraction already bakes in the offset, no playback-time delay
is needed on the client side - both clips just start together.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .ffmpeg_safety import input_args


class PreviewError(RuntimeError):
    pass


@dataclass
class PreviewClips:
    video_clip_path: str
    ad_clip_path: str


def prepare_preview_clips(
    ffmpeg_path: str,
    video_path: str,
    ad_audio_path: str,
    position_seconds: float,
    duration_seconds: float,
    offset_ms: int,
) -> PreviewClips:
    video_start = max(0.0, position_seconds)
    ad_start = max(0.0, position_seconds - offset_ms / 1000.0)

    video_clip = _clip_path("video")
    ad_clip = _clip_path("ad")

    # Cut both clips at the same time - each is a separate ffmpeg process.
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(_extract_clip, ffmpeg_path, video_path, video_start, duration_seconds, video_clip),
            pool.submit(_extract_clip, ffmpeg_path, ad_audio_path, ad_start, duration_seconds, ad_clip),
        ]
        try:
            for job in jobs:
                job.result()
        except PreviewError:
            cleanup_clip(video_clip)
            cleanup_clip(ad_clip)
            raise

    return PreviewClips(video_clip_path=video_clip, ad_clip_path=ad_clip)


_clip_dir: str | None = None
_clip_dir_lock = threading.Lock()


def _private_clip_dir() -> str:
    """A folder only this user can read (mkdtemp creates it with 0700
    permissions), removed again when DescTrack exits, so preview clips of
    the user's media are neither left behind nor readable by other users."""
    global _clip_dir
    with _clip_dir_lock:
        if _clip_dir is None or not os.path.isdir(_clip_dir):
            _clip_dir = tempfile.mkdtemp(prefix="desctrack_preview_")
            atexit.register(shutil.rmtree, _clip_dir, ignore_errors=True)
        return _clip_dir


def _clip_path(label: str) -> str:
    return os.path.join(_private_clip_dir(), f"{label}_{uuid.uuid4().hex}.wav")


def _extract_clip(ffmpeg_path: str, source_path: str, start_seconds: float, duration_seconds: float, output_path: str) -> None:
    args = [
        ffmpeg_path, "-nostdin", "-y",
        "-ss", str(start_seconds),
        *input_args(source_path),
        "-t", str(duration_seconds),
        "-vn", "-ac", "2", "-ar", "44100",
        "-f", "wav",
        output_path,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        raise PreviewError(f'Timed out extracting a preview clip from "{source_path}".')
    if result.returncode != 0 or not os.path.isfile(output_path):
        raise PreviewError(f'ffmpeg could not extract a preview clip from "{source_path}":\n{result.stderr}')


def cleanup_clip(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
