"""Test-wide isolation: never read backend/.env and never see real Gemini or OpenAI keys, so no test
can reach either API. Every test injects fakes or mocked clients instead."""

import os

os.environ["KNOWLY_DISABLE_DOTENV"] = "1"
os.environ["EMBED_DIM"] = "64"  # the fake embedder (tests/fakes.py) makes 64-dimensional vectors
for name in list(os.environ):
    if name == "GEMINI_API_KEY" or name.startswith("GEMINI_API_KEY_") or name == "OPENAI_API_KEY":
        del os.environ[name]


def _no_real_gemini(*_args, **_kwargs):
    raise RuntimeError("A real google.genai.Client was created during tests; inject a fake instead.")


from google import genai  # noqa: E402

genai.Client.__init__ = _no_real_gemini  # any accidental real client fails loudly


def _no_real_openai(*_args, **_kwargs):
    raise RuntimeError("A real openai.OpenAI client was created during tests; inject a fake instead.")


import openai  # noqa: E402

openai.OpenAI.__init__ = _no_real_openai  # any accidental real client fails loudly
