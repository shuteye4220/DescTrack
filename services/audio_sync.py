"""Best-effort automatic sync: downsamples the leading chunk of both the
video's own audio and the AD audio to mono 8 kHz PCM, then finds the lag
that maximizes their cross-correlation using an FFT (numpy). This is a
starting point, not a guarantee - the UI always lets the user nudge from
here.

Offset convention (matches the rest of the app): ad_time = video_time - offset.
A positive offset means the AD track should be delayed relative to the video;
a negative offset means its lead-in should be trimmed so it starts earlier.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .ffmpeg_safety import input_args

SAMPLE_RATE_HZ = 8000


class AudioSyncError(RuntimeError):
    pass


@dataclass
class SyncResult:
    offset_ms: int
    confidence: float


def estimate_offset(
    ffmpeg_path: str,
    video_path: str,
    ad_audio_path: str,
    analysis_seconds: float = 90.0,
    max_lag_seconds: float = 20.0,
    log: Optional[Callable[[str], None]] = None,
) -> SyncResult:
    def report(msg: str) -> None:
        if log:
            log(msg)

    report("Extracting reference audio for analysis...")
    # Decode both files at the same time - each is a separate ffmpeg process.
    with ThreadPoolExecutor(max_workers=2) as pool:
        reference_job = pool.submit(_extract_mono_pcm, ffmpeg_path, video_path, analysis_seconds)
        target_job = pool.submit(_extract_mono_pcm, ffmpeg_path, ad_audio_path, analysis_seconds)
        reference = reference_job.result()
        target = target_job.result()

    if len(reference) < SAMPLE_RATE_HZ // 2 or len(target) < SAMPLE_RATE_HZ // 2:
        raise AudioSyncError(
            "Not enough audio could be decoded from one of the files to estimate sync."
        )

    report("Cross-correlating audio (this can take a few seconds)...")
    max_lag_samples = int(max_lag_seconds * SAMPLE_RATE_HZ)
    lag_samples, confidence = _cross_correlate_best_lag(reference, target, max_lag_samples)

    offset_ms = int(round(lag_samples * 1000.0 / SAMPLE_RATE_HZ))
    return SyncResult(offset_ms=offset_ms, confidence=confidence)


def _extract_mono_pcm(ffmpeg_path: str, media_path: str, seconds: float) -> np.ndarray:
    # Raw 16-bit samples are streamed straight from ffmpeg's stdout (at most
    # ~1.4 MB for 90 s at 8 kHz), so no temporary file is needed.
    args = [
        ffmpeg_path, "-nostdin", "-y",
        "-t", str(seconds),
        *input_args(media_path),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE_HZ),
        "-f", "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise AudioSyncError(f'Timed out decoding audio from "{media_path}".')
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise AudioSyncError(f'ffmpeg could not extract audio from "{media_path}":\n{stderr}')
    raw = result.stdout
    raw = raw[: len(raw) - (len(raw) % 2)]  # whole 16-bit samples only
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def _next_pow2(value: int) -> int:
    power = 1
    while power < value:
        power <<= 1
    return power


def _cross_correlate_best_lag(reference: np.ndarray, target: np.ndarray, max_lag_samples: int):
    n, m = len(reference), len(target)
    size = _next_pow2(n + m - 1)

    ref_energy = float(np.dot(reference, reference))
    tgt_energy = float(np.dot(target, target))
    denom = (ref_energy * tgt_energy) ** 0.5 if ref_energy > 0 and tgt_energy > 0 else 1.0

    ref_fft = np.fft.rfft(reference, size)
    tgt_fft = np.fft.rfft(target, size)
    corr = np.fft.irfft(ref_fft * np.conj(tgt_fft), size)

    # corr[k] is the correlation at lag k for k <= size/2, and at lag
    # k - size (a negative lag) for the upper half of the circular result.
    lags = np.arange(size)
    lags = np.where(lags <= size // 2, lags, lags - size)

    mask = np.abs(lags) <= max_lag_samples
    if not np.any(mask):
        raise AudioSyncError("Search window too small to evaluate any lag.")

    scores = corr[mask] / denom
    candidate_lags = lags[mask]
    best_idx = int(np.argmax(scores))
    best_lag = int(candidate_lags[best_idx])
    confidence = float(np.clip(scores[best_idx], 0.0, 1.0))

    # A positive lag means the reference (video audio) is "ahead" in the
    # buffer, i.e. ad_time = video_time - lag - which is exactly the
    # app-wide offset convention.
    return best_lag, confidence
