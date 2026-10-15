"""Test-wide isolation: never read backend/.env and never see real Gemini keys, so no test can
reach the Gemini API. Every test injects fakes or mocked clients instead."""

import os

os.environ["KNOWLY_DISABLE_DOTENV"] = "1"
for name in list(os.environ):
    if name == "GEMINI_API_KEY" or name.startswith("GEMINI_API_KEY_"):
        del os.environ[name]


def _no_real_gemini(*_args, **_kwargs):
    raise RuntimeError("A real google.genai.Client was created during tests; inject a fake instead.")


from google import genai  # noqa: E402

genai.Client.__init__ = _no_real_gemini  # any accidental real client fails loudly
