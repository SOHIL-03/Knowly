"""Speaks a validated script through the existing TTS client without losing text or audio.

The script is split at turn boundaries into chunks of at most `chunk_chars` (the same limit the
Gemini TTS client uses per request; the model's input window is far larger). Each chunk is
synthesized separately, retried on transient failure, and rejected when its audio is implausibly short
for its text (a sign the provider stopped early). Chunks are kept in order in a list (never written
to a shared buffer or file), joined once, and the result is verified before it is returned.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.ai.client import AiQuotaExceeded, AiUnavailable
from app.audio.tts import SAMPLE_RATE, Speaker, Turn, chunk_turns, pcm_from_audio, pcm_seconds, wav_from_pcm
from app.logs import Timer, get_logger, log

logger = get_logger(__name__)

CHARS_PER_SEC = 14.0  # about 150 spoken English words a minute
MIN_RATIO = 0.35  # a chunk shorter than this share of its expected duration is treated as cut off
MAX_ATTEMPTS = 3


@dataclass
class Assembled:
    wav: bytes
    duration_sec: float
    parts: list[tuple[int, float]]  # (turns, seconds) per chunk, for line timings
    chunk_chars: list[int]
    tts_calls: int


def synthesize_all(tts: Speaker, turns: list[Turn], voices: dict[str, str], *, chunk_chars: int, rid: str,
                   sleep: Callable[[float], None] | None = None, concurrency: int = 1) -> Assembled:
    """Speaks every chunk. With concurrency > 1 several chunks are synthesized at once (each Gemini call takes a minute or
    more, so a long episode is far faster); the results are always joined in script order."""
    chunks = chunk_turns(turns, chunk_chars)
    if sum(len(c) for c in chunks) != len(turns):
        raise AiUnavailable("Internal error: script turns were lost while splitting for audio")

    def speak(n: int, chunk: list[Turn]) -> tuple[bytes, int]:
        """(pcm, tts calls made) for one chunk, retried on transient failure or a cut-off clip."""
        chars = sum(len(t.text) for t in chunk)
        expected = chars / CHARS_PER_SEC
        calls = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            timer = Timer()
            calls += 1
            try:
                speech = tts.synthesize(chunk, voices)
            except AiQuotaExceeded:
                raise  # retrying a used-up quota cannot succeed
            except AiUnavailable as exc:
                log(logger, logging.WARNING, "tldr_tts_chunk_failed", podcastId=rid, chunk=f"{n}/{len(chunks)}",
                    attempt=attempt, chars=chars, category=exc.category, durationMs=timer.ms())
                if attempt < MAX_ATTEMPTS:
                    (sleep or time.sleep)(2.0 * attempt)
                continue
            got = pcm_from_audio(speech.wav)
            secs = pcm_seconds(got)
            if secs < MIN_RATIO * expected:
                log(logger, logging.WARNING, "tldr_tts_chunk_too_short", podcastId=rid, chunk=f"{n}/{len(chunks)}", attempt=attempt,
                    chars=chars, expectedSec=round(expected, 1), gotSec=round(secs, 1), durationMs=timer.ms())
                continue
            log(logger, logging.INFO, "tldr_tts_chunk_ok", podcastId=rid, chunk=f"{n}/{len(chunks)}", attempt=attempt,
                chars=chars, seconds=round(secs, 1), durationMs=timer.ms())
            return got, calls
        raise AiUnavailable(f"Audio for part {n} of {len(chunks)} could not be generated")

    workers = max(1, min(concurrency, len(chunks)))
    if workers == 1:
        done = [speak(n, c) for n, c in enumerate(chunks, start=1)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(lambda a: speak(*a), enumerate(chunks, start=1)))  # map keeps the script order
    pcms = [pcm for pcm, _ in done]
    calls = sum(c for _, c in done)
    parts = [(len(chunk), pcm_seconds(pcm)) for chunk, pcm in zip(chunks, pcms, strict=True)]
    sizes = [sum(len(t.text) for t in chunk) for chunk in chunks]

    pcm_all = b"".join(pcms)
    wav = wav_from_pcm(pcm_all)
    expected_sec = sum(secs for _, secs in parts)
    with wave.open(io.BytesIO(wav), "rb") as w:  # readable, non-empty, duration matches the chunks
        frames, rate = w.getnframes(), w.getframerate()
    duration = frames / rate if rate else 0.0
    if len(pcms) != len(chunks) or not frames or rate != SAMPLE_RATE or abs(duration - expected_sec) > 0.05:
        raise AiUnavailable("The assembled audio did not match the generated parts")
    log(logger, logging.INFO, "tldr_audio_assembled", podcastId=rid, chunks=len(chunks), ttsCalls=calls, seconds=round(duration, 1),
        bytes=len(wav), chars=sum(sizes))
    return Assembled(wav, duration, parts, sizes, calls)
