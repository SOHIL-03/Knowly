"""Reading Gemini's transcription response, and the transcriber's call, without the network."""

from __future__ import annotations

from types import SimpleNamespace as NS

from app.audio.stt import GeminiTranscriber, transcript_text


def response(*parts):
    return NS(candidates=[NS(content=NS(parts=list(parts)))])


def test_reads_the_audio_transcription_part_of_a_dedicated_transcribe_model():
    res = response(NS(audio_transcription=NS(text="How do vector databases help?"), text=None))
    assert transcript_text(res) == "How do vector databases help?"


def test_reads_plain_text_parts_and_joins_several():
    assert transcript_text(response(NS(text=" Hello ", audio_transcription=None), NS(text="there", audio_transcription=None))) == "Hello there"


def test_no_speech_is_an_empty_string_not_an_error():
    assert transcript_text(response(NS(text=None, audio_transcription=NS(text="")))) == ""
    assert transcript_text(NS(candidates=None)) == ""


def test_transcriber_sends_audio_and_returns_the_recognised_text(monkeypatch):
    from app.config import Settings

    calls = []

    class Pool:
        vertex = False

        def call(self, what, fn):
            class Models:
                def generate_content(self, **kw):
                    calls.append(kw)
                    return response(NS(audio_transcription=NS(text="what is chunking"), text=None))

            return fn(NS(models=Models()))

    t = GeminiTranscriber(Settings(gemini_stt_model="gemini-3.5-transcribe"), Pool())
    assert t.transcribe(bytes(2000), "audio/webm") == "what is chunking"
    assert calls[0]["model"] == "gemini-3.5-transcribe" and len(calls[0]["contents"]) == 2
