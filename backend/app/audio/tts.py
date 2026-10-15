"""Gemini text-to-speech (https://ai.google.dev/gemini-api/docs/speech-generation, read 2026-10-15).

- Podcast segments: multi-speaker TTS, two prebuilt voices, one text turn per script line with a
  `speech_metadata` annotation naming the speaker.
- Answers to the listener: single-speaker TTS.

Requests go through `client.interactions.create(... response_format={"type": "audio"})`. Unary
output from the 3.8 TTS models is a WAV file (24 kHz, mono, 16-bit PCM); older models returned raw
PCM, which is also accepted below.
"""

from __future__ import annotations

import base64
import io
import logging
import wave
from dataclasses import dataclass
from typing import Protocol

from app.ai.client import AiUnavailable, to_ai_error
from app.ai.gemini_keys import GeminiError, GeminiKeyPool, load_keys
from app.config import Settings
from app.logs import Timer, get_logger, log

logger = get_logger(__name__)

SAMPLE_RATE = 24_000
SAMPLE_WIDTH = 2
CHANNELS = 1


@dataclass
class Turn:
    speaker: str  # configured speaker name (multi-speaker) or "" for single-speaker
    text: str
    style: str | None = None


@dataclass
class Speech:
    wav: bytes
    duration_sec: float
    # (number of turns, seconds) for each request, in order; used to time individual lines.
    parts: list[tuple[int, float]]


class Speaker(Protocol):
    def synthesize(self, turns: list[Turn], voices: dict[str, str]) -> Speech:
        """voices maps speaker name -> prebuilt voice. Two entries = multi-speaker TTS,
        one entry = single-speaker TTS (turn speakers are then ignored)."""
        ...


# --- WAV helpers ---------------------------------------------------------------------------

def pcm_from_audio(data: bytes) -> bytes:
    """Raw 16-bit PCM frames from a WAV file, or the bytes unchanged if they are already raw PCM."""
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != SAMPLE_WIDTH or w.getnchannels() != CHANNELS or w.getframerate() != SAMPLE_RATE:
                raise AiUnavailable(
                    f"Unexpected TTS audio format {w.getframerate()} Hz, {w.getnchannels()} ch, {w.getsampwidth() * 8} bit"
                )
            return w.readframes(w.getnframes())
    return data


def wav_from_pcm(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


def chunk_turns(turns: list[Turn], max_chars: int) -> list[list[Turn]]:
    """Groups consecutive turns into requests of at most about max_chars characters."""
    chunks: list[list[Turn]] = []
    cur: list[Turn] = []
    size = 0
    for t in turns:
        if cur and size + len(t.text) > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t.text)
    if cur:
        chunks.append(cur)
    return chunks


# --- Gemini implementation -------------------------------------------------------------------

class GeminiSpeaker:
    def __init__(self, settings: Settings, pool: GeminiKeyPool | None = None) -> None:
        self.settings = settings
        # TTS runs on the Gemini API with API keys (not Vertex), through the shared key pool.
        self.pool = pool if pool is not None and not pool.vertex else GeminiKeyPool(settings, keys=load_keys(settings))

    def _create(self, content: list[dict], speech_config: dict | list) -> bytes:
        timer = Timer()
        model = self.settings.gemini_tts_model
        try:
            interaction = self.pool.call(
                f"{model}:tts",
                lambda client: client.interactions.create(
                    model=model,
                    input=[{"type": "user_input", "content": content}],
                    response_format={"type": "audio"},
                    generation_config={"speech_config": speech_config},
                ),
            )
        except GeminiError as err:
            log(logger, logging.ERROR, "tts_call_failed", model=model, category=err.category.value, attempts=err.attempts,
                latencyMs=timer.ms())
            raise to_ai_error(err) from None
        audio = getattr(interaction, "output_audio", None)
        if audio is None or not getattr(audio, "data", None):
            log(logger, logging.ERROR, "tts_no_audio", model=model, latencyMs=timer.ms(),
                outputType=type(interaction).__name__)
            raise AiUnavailable("Gemini TTS returned no audio")
        data = base64.b64decode(audio.data)
        log(logger, logging.INFO, "tts_call", model=self.settings.gemini_tts_model, latencyMs=timer.ms(), bytes=len(data))
        return data

    def synthesize(self, turns: list[Turn], voices: dict[str, str]) -> Speech:
        if not 1 <= len(voices) <= 2:
            raise ValueError("Gemini TTS takes one voice, or two for multi-speaker (prebuilt voices)")
        multi = len(voices) == 2
        pcm = b""
        parts: list[tuple[int, float]] = []
        chunks = chunk_turns(turns, self.settings.tts_chunk_chars)
        log(logger, logging.INFO, "tts_synthesize_start", turns=len(turns), chunks=len(chunks), multiSpeaker=multi,
            chars=sum(len(t.text) for t in turns), model=self.settings.gemini_tts_model)
        for n, chunk in enumerate(chunks, start=1):
            log(logger, logging.DEBUG, "tts_chunk", chunk=f"{n}/{len(chunks)}", turns=len(chunk))
            content = []
            for t in chunk:
                ann: dict = {"type": "speech_metadata"}
                if multi:
                    ann["speaker"] = t.speaker
                if t.style:
                    ann["style"] = t.style
                item: dict = {"type": "text", "text": t.text}
                if len(ann) > 1:
                    item["annotations"] = [ann]
                content.append(item)
            if multi:
                speech_config: dict | list = {"speakers": [{"speaker": n, "voice": v} for n, v in voices.items()]}
            else:
                speech_config = [{"voice": next(iter(voices.values()))}]
            part = pcm_from_audio(self._create(content, speech_config))
            parts.append((len(chunk), pcm_seconds(part)))
            pcm += part
        return Speech(wav_from_pcm(pcm), pcm_seconds(pcm), parts)


def line_timings(turns: list[Turn], parts: list[tuple[int, float]]) -> list[dict[str, float]]:
    """Start/end seconds per turn. Each request's measured duration is shared out by character
    count, so timings are estimates within a request and exact at request boundaries."""
    out: list[dict[str, float]] = []
    pos = 0
    t0 = 0.0
    for n, secs in parts:
        group = turns[pos : pos + n]
        total = sum(max(1, len(t.text)) for t in group) or 1
        t = t0
        for turn in group:
            d = secs * max(1, len(turn.text)) / total
            out.append({"s": round(t, 2), "e": round(t + d, 2)})
            t += d
        pos += n
        t0 += secs
    return out


def build_speaker(settings: Settings, pool: GeminiKeyPool | None = None) -> Speaker:
    return GeminiSpeaker(settings, pool)
