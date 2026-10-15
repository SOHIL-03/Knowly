"""Gemini TTS client: request shape matches ai.google.dev/gemini-api/docs/speech-generation, WAV
handling, chunking and line timings. The Gemini client is replaced by a recorder; no network."""

import base64
import io
import wave
from types import SimpleNamespace

import pytest

from app.ai.client import AiUnavailable
from app.audio.tts import GeminiSpeaker, Turn, chunk_turns, line_timings, pcm_from_audio, pcm_seconds, wav_from_pcm
from app.config import Settings


class Recorder:
    def __init__(self, seconds_per_call=(1.0,), fail=False):
        self.calls = []
        self.seconds = list(seconds_per_call)
        self.fail = fail

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        secs = self.seconds[min(len(self.calls) - 1, len(self.seconds) - 1)]
        wav = wav_from_pcm(bytes(int(24_000 * secs) * 2))
        return SimpleNamespace(output_audio=SimpleNamespace(data=base64.b64encode(wav).decode()))


def speaker(rec, **settings):
    from app.ai.gemini_keys import ApiKey, GeminiKeyPool

    s = Settings(**settings)
    pool = GeminiKeyPool(s, keys=[ApiKey("GEMINI_API_KEY_1", "test-key-value")],
                         client_factory=lambda key: SimpleNamespace(interactions=rec), sleep=lambda _s: None)
    return GeminiSpeaker(s, pool)


def test_multi_speaker_request_matches_documented_shape():
    rec = Recorder()
    sp = speaker(rec)
    out = sp.synthesize([Turn("Maya", "How's it going?", "cheerful"), Turn("Theo", "Not bad.")], {"Maya": "Kore", "Theo": "Puck"})
    call = rec.calls[0]
    assert call["model"] == "gemini-3.8-flash-tts"
    assert call["response_format"] == {"type": "audio"}
    assert call["generation_config"] == {"speech_config": {"speakers": [
        {"speaker": "Maya", "voice": "Kore"}, {"speaker": "Theo", "voice": "Puck"}]}}
    content = call["input"][0]["content"]
    assert call["input"][0]["type"] == "user_input"
    assert content[0] == {"type": "text", "text": "How's it going?",
                          "annotations": [{"type": "speech_metadata", "speaker": "Maya", "style": "cheerful"}]}
    assert content[1]["annotations"] == [{"type": "speech_metadata", "speaker": "Theo"}]
    assert out.duration_sec == pytest.approx(1.0)
    with wave.open(io.BytesIO(out.wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24_000, 1, 2)


def test_single_speaker_request_matches_documented_shape():
    rec = Recorder()
    speaker(rec).synthesize([Turn("", "Have a wonderful day!", "warm")], {"Tutor": "Sulafat"})
    call = rec.calls[0]
    assert call["generation_config"] == {"speech_config": [{"voice": "Sulafat"}]}
    assert call["input"][0]["content"] == [{"type": "text", "text": "Have a wonderful day!",
                                            "annotations": [{"type": "speech_metadata", "style": "warm"}]}]


def test_more_than_two_voices_is_rejected():
    with pytest.raises(ValueError):
        speaker(Recorder()).synthesize([Turn("a", "x")], {"a": "Kore", "b": "Puck", "c": "Zephyr"})


def test_long_scripts_are_chunked_and_joined():
    rec = Recorder(seconds_per_call=(1.0, 2.0))
    sp = speaker(rec, tts_chunk_chars=20)
    turns = [Turn("Maya", "a" * 15), Turn("Theo", "b" * 15), Turn("Maya", "c" * 5)]
    out = sp.synthesize(turns, {"Maya": "Kore", "Theo": "Puck"})
    assert len(rec.calls) == 2
    assert out.parts == [(1, pytest.approx(1.0)), (2, pytest.approx(2.0))]
    assert out.duration_sec == pytest.approx(3.0)


def test_errors_and_missing_key_become_ai_unavailable():
    with pytest.raises(AiUnavailable):
        speaker(Recorder(fail=True)).synthesize([Turn("", "x")], {"Tutor": "Kore"})
    with pytest.raises(AiUnavailable):
        GeminiSpeaker(Settings(gemini_api_key="")).synthesize([Turn("", "x")], {"Tutor": "Kore"})


def test_raw_pcm_from_older_models_is_accepted():
    pcm = bytes(4800)
    assert pcm_from_audio(pcm) == pcm
    assert pcm_from_audio(wav_from_pcm(pcm)) == pcm
    assert pcm_seconds(pcm) == pytest.approx(0.1)


def test_chunk_turns_never_splits_a_turn():
    turns = [Turn("a", "x" * 30), Turn("b", "y" * 30)]
    assert [len(c) for c in chunk_turns(turns, 10)] == [1, 1]


def test_line_timings_share_each_part_by_text_length():
    turns = [Turn("a", "x" * 10), Turn("b", "y" * 30), Turn("a", "z" * 10)]
    timings = line_timings(turns, [(2, 4.0), (1, 2.0)])
    assert timings == [{"s": 0.0, "e": 1.0}, {"s": 1.0, "e": 4.0}, {"s": 4.0, "e": 6.0}]
