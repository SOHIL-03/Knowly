"""Tests every configured Gemini API key and says plainly whether it works, is out of quota/credits, is invalid or expired,
or is only rate limited.   Run from backend/:   python -m app.ai.check_keys

Each key gets one tiny embedding request and one tiny text-to-speech request. Both are checked because they have separate
quotas: a key can still embed while its speech quota is used up. Key values are never printed, only their variable names.
"""

from __future__ import annotations

import sys

from app.ai.gemini_keys import Category, GeminiError, GeminiKeyPool, load_keys
from app.audio.tts import GeminiSpeaker, Turn
from app.config import get_settings

VERDICT = {
    Category.QUOTA_EXHAUSTED: "OUT OF QUOTA / CREDITS (daily or monthly limit used up, or billing not enabled)",
    Category.RATE_LIMITED: "RATE LIMITED right now (per-minute limit; works again shortly)",
    Category.AUTH: "INVALID, EXPIRED OR DENIED KEY (revoked, deleted, wrong project, or API not enabled)",
    Category.NOT_FOUND: "MODEL NOT FOUND for this key (check GEMINI_TTS_MODEL / GEMINI_EMBED_MODEL)",
    Category.BAD_REQUEST: "REQUEST REJECTED (bad request)",
    Category.SERVER_UNAVAILABLE: "GEMINI TEMPORARILY UNAVAILABLE (overloaded; not the key's fault)",
    Category.NETWORK: "NETWORK ERROR reaching Gemini",
    Category.TIMEOUT: "HANGS: NO ANSWER within the time limit (a dead or blocked key/project; the app now skips it)",
}


def probe(settings, key) -> dict[str, str]:
    """{"embeddings": verdict, "speech": verdict} for one key, using a pool that contains only that key."""
    pool = GeminiKeyPool(settings, keys=[key])
    out: dict[str, str] = {}
    try:
        pool.call("check:embed", lambda c: c.models.embed_content(model=settings.gemini_embed_model, contents=["ping"]))
        out["embeddings"] = "ok"
    except GeminiError as e:
        out["embeddings"] = VERDICT.get(e.category, f"FAILED ({e.category.value})")
    try:
        GeminiSpeaker(settings, pool).synthesize([Turn(speaker="", text="Okay.")], {"Tutor": settings.tts_answer_voice})
        out["speech"] = "ok"
    except Exception as e:
        cat = getattr(e, "category", None)
        try:
            out["speech"] = VERDICT.get(Category(cat), f"FAILED ({cat})")
        except ValueError:
            out["speech"] = f"FAILED ({type(e).__name__})"
    return out


def main() -> int:
    settings = get_settings().model_copy(update={"gemini_timeout_seconds": 40.0})  # the checker should not wait minutes
    keys = load_keys(settings)
    if not keys:
        print("No Gemini API keys are configured (set GEMINI_API_KEY_1 ... GEMINI_API_KEY_10 in backend/.env).")
        return 1
    print(f"Checking {len(keys)} key(s): embeddings = {settings.gemini_embed_model}, speech = {settings.gemini_tts_model}\n")
    usable = 0
    for key in keys:
        res = probe(settings, key)
        ok = all(v == "ok" for v in res.values())
        usable += ok
        print(f"{key.name}: {'WORKING' if ok else 'PROBLEM'}")
        for what, verdict in res.items():
            print(f"    {what:<11} {verdict}")
    print(f"\n{usable} of {len(keys)} key(s) fully working.")
    return 0 if usable else 2


if __name__ == "__main__":
    sys.exit(main())
