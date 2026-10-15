# Knowly

Knowly reads the newsletters you already get, regroups the stories around what you are building, turns them into a short spoken briefing, and checks what you learned after each segment. It keeps a per-concept learner model, so later briefings are pitched at the right level.

This repository implements *Knowly Architecture, Database, Features and UI, Draft v3 (15 Oct 2026)*, with one product decision on top: **all audio is made with Gemini text-to-speech** and **all text generation uses OpenAI `gpt-4o-mini`** (NotebookLM is not used). Section numbers below refer to the v3 document.

## Architecture

```
Browser (Vite + React PWA, Firebase Hosting)
  ├─ Firebase Auth: Google sign-in, or anonymous sign-in for sample mode
  ├─ Firestore listeners: read-only, for live progress (rules forbid all browser writes)
  └─ /api/** ──► Cloud Run: FastAPI service "knowly-api"  (backend/app)
                    ├─ live path: /ask (streamed), /quiz/*, /deeper, /apply, /rephrase, /library, /knowledge,
                    │             /qa/{id}/audio and /quiz/{id}/audio (spoken answers) …
                    ├─ B1 trigger ─► Pub/Sub run-briefing ─► /internal/pubsub/run-briefing ─► Cloud Run Job (python -m app.job)
                    │                 B2 fetch → B3 parse → B4 enrich → B5 de-dupe   (cached per issue, shared)
                    │                 B6 score → B7 plan → B8 grounded scripts → B10 publish text   (per user)
                    ├─ B9 per segment ─► Cloud Tasks ─► /internal/tasks/segment-audio (Gemini multi-speaker TTS)
                    ├─ Cloud Scheduler ─► /internal/scheduler/dispatch (every 5 min), /renew-watches (daily)
                    └─ Gmail users.watch ─► Pub/Sub gmail-events ─► /internal/pubsub/gmail (10-minute debounce)
Data: Firestore (vector fields) · Cloud Storage (WAV audio, raw HTML with a 7-day expiry) · Secret Manager (OpenAI key, Gemini keys, Gmail tokens)
AI:   OpenAI gpt-4o-mini (OPENAI_MODEL) for all text work (parse, plans, scripts, Ask, quiz, grading, faithfulness) · gemini-embedding-001 (768-d)
      Gemini TTS gemini-3.8-flash-tts: multi-speaker for the podcast, single-speaker for spoken answers
```

There is one backend. FastAPI owns every API route. The prototype's Express server (`server.ts`) has been removed.

| Path | What it is |
|---|---|
| `backend/app/pipeline/` | B2–B10: `ingest.py` (shared cache), `score.py` (§7.1 formula), `personal.py` (plans, scripts), `validate.py` (grounding gates), `runner.py`, `audio_stage.py` |
| `backend/app/audio/tts.py` | Gemini TTS client (multi- and single-speaker), WAV joining, line timings |
| `backend/app/learner/` | §7.2–7.4 mastery, 1/3/7-day review, topic weights with a 30-day half-life |
| `backend/app/checkpoint.py` | Ask with citation validation, recall quiz with server-only rubrics, Apply, Simpler, Go deeper, Save, spoken answers |
| `backend/app/gmail/` | Read-only OAuth, sender detection, fetch with the newsletter-header check, watch/renew/stop |
| `backend/app/routes/` | HTTP routes; `internal.py` is only reachable with a Google OIDC token |
| `frontend/` | Web app (Vite + React): Landing, Gmail explainer, 4-step onboarding, Generating, Today, Player + checkpoint, Recap, Library, Knowledge, Settings |
| `firestore.rules`, `firestore.indexes.json`, `storage.rules` | Security rules and indexes (§6.4–6.5) |
| `deploy/setup-gcp.sh` | One-time Google Cloud setup and deploy (not yet run against a real project) |

## Audio: Gemini text-to-speech

Audio follows the [Gemini speech-generation docs](https://ai.google.dev/gemini-api/docs/speech-generation), checked on 15 Oct 2026. Requests use `client.interactions.create` with `response_format={"type": "audio"}`.

**Podcast segments use multi-speaker TTS.**
- B8 writes a two-host script (Maya and Theo by default). Every factual line carries the ids of the newsletter items it comes from.
- Before any audio is made, the script must pass a validator: it uses only this segment's items, covers every item, keeps untagged lines to short transitions, and is about the target length.
- It must also pass a Gemini faithfulness check against the sources. A failing script is regenerated once, then replaced by a script built directly from the newsletter notes.
- TTS reads the script verbatim, so the audio says what the checked script says. Long scripts are sent in chunks and the WAV parts are joined.
- The **One narrator** style uses single-speaker TTS for the podcast instead.

**Answers to the listener use single-speaker TTS.**
- Ask answers, Apply suggestions, quiz questions and quiz feedback have a **Listen** control (`POST /api/qa/{id}/audio`, `POST /api/quiz/{id}/audio`).
- Only text already stored on the server is spoken. Quiz feedback is spoken only after the listener has answered.
- Audio is cached after the first request.

**Transcript.** The player shows the script with speaker names and highlights the current line. Gemini TTS returns no word timings, so line times are estimates: each request's measured audio length is shared across its lines by character count.

**Failures.** The text briefing is published before the audio. If a segment's audio fails, it is retried once with backoff, then shown as text with a **Retry audio** button.

Voices and models are configurable (`GEMINI_TTS_MODEL`, `TTS_HOST_A_VOICE`, …). TTS needs at least `GEMINI_API_KEY_1`.

## Run locally

Prerequisites: conda, Node 22+ and Java 21+ (for the emulators). In `backend/.env` set `OPENAI_API_KEY` (all text generation; `OPENAI_MODEL` defaults to `gpt-4o-mini`) and `GEMINI_API_KEY_1` (and optionally `_2` to `_10`; embeddings and audio). Those are the only required settings locally.

Run two scripts, each in its own terminal (Git Bash on Windows):

```bash
./backend/run.sh    # API on http://localhost:8000
./frontend/run.sh   # Firebase emulators + web app on http://localhost:5173 (emulator UI: http://127.0.0.1:4000)
```

- **`backend/run.sh`**
  - Activates the conda environment `ht`, creating it with Python 3.12 if missing (use another with `CONDA_ENV=name ./backend/run.sh`).
  - Installs the Python packages on first run and again whenever the requirements change.
  - Creates `backend/.env` from the example if it is missing, and stops with a message if no OpenAI key or no Gemini key is set.
  - Starts the API with auto-reload.
- **`frontend/run.sh`**
  - Installs npm packages when `package-lock.json` changes.
  - Starts the Auth, Firestore and Storage emulators together with Vite. Ctrl+C stops both.
  - Saves emulator data to `.emulator-data/` on exit and reloads it on the next run. `FRESH=1 ./frontend/run.sh` starts empty.
- Either script can be started first.
- **Other devices:** both scripts listen on `0.0.0.0`, and so do the emulators (see `firebase.json`). Other devices on your network can open `http://<this-PC-IP>:5173`.
  - The web app connects to the emulators on the host it was opened from.
  - Audio links are relative (`PUBLIC_API_BASE_URL` empty), so they work from any device.
  - For local-only access use `HOST=127.0.0.1 ./backend/run.sh` and `HOST=127.0.0.1 ./frontend/run.sh`.
  - Browsers allow the microphone only on `localhost` or HTTPS, so on another device type your questions; everything else works.
  - Allow ports 5173, 8000, 9099, 8080 and 9199 through the Windows firewall if other devices can't connect.

Manual equivalent: `cd frontend && npm run emulators`, then `cd backend && python -m uvicorn app.main:app --reload --port 8000`, then `cd frontend && npm run dev`.

**OpenAI text generation.** Every text call (item extraction, whole-email summary, segment plan, podcast script, faithfulness check, Ask, quiz, grading, Apply, rephrase, concepts, profile) goes through `backend/app/ai/openai_llm.py` with `OPENAI_API_KEY` and `OPENAI_MODEL` (default `gpt-4o-mini`). The key stays on the backend. One client is created on first use and reused.

- **Structured output:** JSON mode, with the Pydantic model's JSON Schema in the system prompt. The reply is validated with the same model; an invalid reply is retried once with the validation error, then raises. Prompts and schemas are unchanged.
- **Retries:** 429 (rate limit), 5xx and network/timeout errors are retried with backoff, at most `OPENAI_MAX_RETRIES` (default 2) times. Exhausted quota (`insufficient_quota`), authentication, unknown model and bad requests fail at once. Streaming retries only before the first chunk.
- **No fallback:** if OpenAI fails the request fails with the app's usual error; Gemini is never used for text.
- **Usage logging:** each call logs model, latency and input/output/total tokens from the API response (`openai_call`, `openai_stream_done`), plus failed attempts (`openai_attempt_failed`). Keys are never logged.

**Reviewing OpenAI responses before audio.** Tick "Review OpenAI responses and the script before audio" on the Today screen (or set `REVIEW_BEFORE_AUDIO=true` in `backend/.env` as the server default), then make a briefing.

- **Preview:** the Generating and Player screens show an *OpenAI response preview*: one row per `gpt-4o-mini` call (content extraction, whole-email summary, segment plan, podcast script, faithfulness check) with input preview, the **raw response exactly as returned**, the **parsed result** (shown separately), validation status, errors and the API's token counts. It is captured from the one existing call per step; nothing is re-requested. Stored in `llmtrace/{uid}/{briefingId}.json` (7-day lifecycle), only for briefings made with review on.
- **Script review:** each segment waits in the `review` state with the full script (speaker labels kept, scrollable), a raw-JSON view, Copy, and optional Edit. **Generate Audio** then runs the existing Gemini TTS on exactly that text. If you edited it, the segment is marked `scriptEdited` and the original is kept in `scriptOriginal`.
- **Not covered:** Ask, quiz, grading and "go deeper" segments are generated on demand and are not part of the preview (deeper segments still make audio automatically).

**Gemini API keys and quota (embeddings and audio only).** Gemini still provides `gemini-embedding-001` embeddings and all text-to-speech (`gemini-3.8-flash-tts`); that code and its credentials (`GEMINI_API_KEY_1` … `_10`, `GEMINI_TTS_MODEL`, voices) are unchanged. Free-tier keys allow only a few requests per model per day.

- **Up to 10 keys:** set `GEMINI_API_KEY_1` … `GEMINI_API_KEY_10`. Every request starts with key 1 and tries the keys in order, moving to the next key only when the current one is out of quota. The next request starts at key 1 again.
- **Rate limits and overload:** a short rate limit is retried on the same key after the server's suggested delay (at most twice). After that the request moves to the next key. A "model overloaded" error (503) is retried but never switches keys.
- **Errors that are never retried with another key:** bad requests, authentication or permission errors, unknown models, and safety blocks.
- **Shared quotas:** keys created in the same Google Cloud project share one quota, so extra keys only help if they come from different projects or billing accounts.
- **Secrets:** key values never appear in logs, errors or API responses; logs name the key (for example `GEMINI_API_KEY_3`). The single `GEMINI_API_KEY` still works when no numbered key is set.

Choose **Try with a sample newsletter**. Sample mode runs the real pipeline on three bundled sample issues (`backend/app/fixtures/sample_issues/`). They were written for the demo, and every link is a real documentation page. Gmail is not needed.

`GET /api/ready` reports what is configured (Firestore, OpenAI model, Gemini, TTS model, dispatch mode).

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest -q   # unit tests; integration tests need the emulators running
cd frontend && npm test                           # frontend unit tests (vitest)
cd frontend && npx tsc --noEmit                   # type check
# Browser journey (Playwright): emulators + test API + Vite
cd backend && .venv/Scripts/python -m tests.e2e_server     # TEST ONLY: real app, fake AI providers
cd frontend && VITE_USE_EMULATORS=true npx vite            # another terminal
cd frontend && npx playwright test                         # mobile + desktop
```

- Integration tests use real Auth and Firestore emulators, real ID tokens and the real security rules.
- OpenAI text (the OpenAI client is mocked), Gemini TTS, Cloud Storage and Secret Manager are replaced by deterministic fakes in `backend/tests/fakes.py`. The fake TTS returns real (silent) WAV audio. These fakes are never wired into the app.
- `backend/tests/unit/test_tts.py` checks that the TTS request body matches the documented shape exactly.

## Deploy

See `deploy/setup-gcp.sh`. It covers:

- APIs, two least-privilege service accounts, and secrets (OpenAI key, Gemini keys, Gmail client secret)
- Firestore rules and indexes, and the bucket with its lifecycle rule
- The Cloud Run service and job
- Pub/Sub push with OIDC and dead-lettering, Cloud Tasks and Cloud Scheduler
- Firebase Hosting

Configuration keys are documented in `backend/.env.example` and `frontend/.env.example`.

## Known limits

- **Email notifications:** not implemented. Sending email would need a non-Google service or a Gmail send scope, which breaks the read-only rule. Push (FCM) is implemented.
- **Gmail for real users:** needs Google OAuth verification of the restricted `gmail.readonly` scope (§13). Until then, use test users or sample mode.
- **Line highlighting is approximate:** Gemini TTS returns no word or line timings.
- **Never run here:** real OpenAI and Gemini calls (text, embeddings or TTS), Gmail and the deploy script. No credentials were available during development.
