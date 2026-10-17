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
| `backend/app/recommend/` | Semantic recommendations: interest facets, story embeddings, ranking, feedback learning (see Recommendations) |
| `backend/recsys_eval/` | Offline evaluation: 50 labelled stories, keyword versus embedding ranking |
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

## Recommendations: semantic ranking

Stories are ranked for each user from embeddings and plain Python. **No language model scores, classifies, reranks or explains a story**, and ranking makes no AI call at all (a test fails the build if `app/recommend` mentions the LLM). Code: `backend/app/recommend/`. `REC_ENABLED=false` brings back the original B6 scorer.

**Where it plugs into the pipeline**

| Stage | What happens |
|---|---|
| B5 Embed and de-duplicate (`pipeline/ingest.py`) | `ensure_story_embeddings`: one embedding per story from headline + blurb + the opening of the linked article (`REC_EXCERPT_CHARS`), task type `RETRIEVAL_DOCUMENT`, cached on `items/{id}` and shared by every user. A content signature (model, dimension, headline, blurb, article present, excerpt length) decides whether it is embedded again. Vectors that are missing, the wrong size, non-finite or all zeros are rejected. Batched, with bounded retries and exponential backoff for server/network errors and one overall time budget (`REC_EMBED_*`). |
| B6 Rank (`pipeline/personal.py`) | `prepare_profile` builds the user's facets and embeds only those that have no valid embedding (`RETRIEVAL_QUERY`). `semantic_rank` then runs `recommend/ranking.py`. If the user has no embedded interest facet, no story has a usable vector, or anything fails, the original scorer runs unchanged (users without a profile still get the popularity ranking). |
| B7 Plan and publish (`pipeline/runner.py`) | The ranked list, the matched facet and the ranking mode are stored on the briefing (`ranked`, `rankingMode`). The planner and the script writer receive the same `Scored` objects and reason lines as before, so the API contract is unchanged. |

**Interest facets** (`users/{uid}/profile/main.facets[]`): `project`, `goal`, `stack`, `interest`, `expertise` and `mute`, each with `id`, `text`, `type`, `weight`, `embedding`, `createdAt`, `updatedAt`. They come only from what the user declared: the project text and goals and mute topics from onboarding, plus optional `stack`, `interests` and `expertise` lists accepted by `PUT /api/profile`. Nothing is inferred and no model is used. Facets are never averaged into one profile vector. An embedding is reused while the facet text, model and dimension are unchanged and is regenerated when the text changes. The existing project and goal embeddings are reused too, so most users need no extra embedding call.

**Ranking** (`ranking.py`)
1. `facet_score = max(0, cosine(story, facet)) × effective facet weight` for every positive facet; `semantic_score` is the best one. Up to two close runners-up are kept as secondary matches for display and never add to the score.
2. Relevance is min-max normalised over the candidates, because raw cosines have no universal scale. Novelty (only where there is data: known concepts with mastery ≥ 0.7, or "Knew this already" feedback) is at most 30% of the score (default 15%); stories from clusters heard in the last 7 briefings are penalised.
3. Duplicates (same id, canonical URL, normalised headline, or cosine ≥ `REC_DUPLICATE_THRESHOLD`) collapse to one story, preferring the copy with article text. Related but different stories are kept. With recommendations on, B5 clustering uses the same threshold (0.93) instead of the original 0.88: in `recsys_eval`, true duplicates scored 0.99, 0.96 and 0.89 while distinct but related stories reached 0.91, so 0.88 would have merged different stories. 0.93 merges none of those but misses the 0.89 duplicate, which only MMR demotes; the cosine alone cannot separate them.
4. Maximal marginal relevance (`REC_MMR_LAMBDA`) picks `REC_FINAL_COUNT` stories. A wildcard is a story that looks different from the picks but clears a relevance floor and has no mute penalty; it is never forced.

**Mutes** are separate negative signals, never mixed into relevance. A story is dropped when a muted phrase is in its headline, category or extracted entities, or when its cosine to a muted topic is at least `REC_MUTE_EXCLUDE_GAP` above the candidates' median cosine to that topic (a fixed baseline stands in for the median when there are fewer than `REC_MUTE_MIN_CANDIDATES` stories). A smaller gap, or a phrase only in the blurb (an incidental mention), is penalised instead. High relevance never overrides a mute. *Limitation:* embeddings cannot reliably separate "about X" from "mentions X at length". In the evaluation set a pgvector story that only thanks "startup funding programs" scores close to funding stories, so it is penalised but kept, and one clearly crypto story (+0.097) falls just under the exclude gap and is only penalised. The gaps were chosen from that same dataset's live similarities, so they are a starting point to recalibrate (`python -m recsys_eval.run_eval --live --mute-diagnostics`), not a validated threshold.

**Explanations** are built from the matched facet's own text, with no model: "Recommended because it matches your project: …", "Matches your learning goal: …", "Related to your technology stack: …". A weakly matched story gets a neutral line, the wildcard keeps "Wildcard: outside your usual topics". The Today screen shows these lines under each segment.

**Feedback** (`feedback.py`, called from `record_events`)

| Action | Effect |
|---|---|
| Save, Ask, Go deeper | learned weight of the matched facet +0.10 / +0.05 / +0.08 |
| Less like this | learned weight −0.15 (the existing 3-times rule that adds the topic to the mute list still applies) |
| Knew this already (new button in the checkpoint sheet) | raises familiarity of the story's topics, which lowers its novelty; relevance is untouched |

The declared weight is never rewritten: `effective weight = clamp(declared + decayed learned delta, REC_WEIGHT_MIN, REC_WEIGHT_MAX)`, with the learned part bounded at ±`REC_FEEDBACK_MAX_DELTA` and halving every `REC_FEEDBACK_HALF_LIFE_DAYS` (30). Each submission is one record in `users/{uid}/feedback/{key}` (`userId`, `storyId`, `briefingId`, `action`, `matchedFacetId`, `topic`, `createdAt`, `idempotencyKey`), written in the same transaction as the profile update; the key is one per briefing, story and action, so retries and double taps count once. `GET /api/profile/facets` shows declared, learned and effective weights; `POST /api/profile/facets/reset-learning` clears what feedback taught.

**Data model changes** (no new database, index or security rule; the user subtree is already read-only for browsers, exported and deleted with the account)
- `users/{uid}/profile/main`: `facets[]`, `facetPrefs{facetId: {delta, updatedAt}}`, `familiarity{topic: {w, updatedAt}}`, optional `stack[]`, `interests[]`, `expertise[]`.
- `users/{uid}/feedback/{idempotencyKey}`: new collection (above).
- `items/{id}`: `embeddingSig`, `embeddingModel`, `embeddingDim`, `embeddedAt` next to the existing `embedding`. Items embedded before this change are embedded once more so every cached vector uses the same text.
- `users/{uid}/briefings/{id}`: `ranked[]` (`itemId`, `score`, `semanticScore`, `rawSimilarity`, `relevance`, `facetId`, `facetType`, `facetText`, `matchedFacets[]`, `noveltyScore`, `rankingVersion`, `reason`, `wildcard`, `explained`), `rankingMode` (`semantic` or `legacy`). There is no rerank or model-generated field.

**Configuration:** every weight, threshold and limit above is a `REC_*` variable listed in `backend/.env.example` (model: `GEMINI_EMBED_MODEL`, dimension: `EMBED_DIM`). Facet and ranking state is per user under `users/{uid}`; the only shared data is the story embedding on the shared item.

### Evaluation (`backend/recsys_eval/`)

50 hand-labelled stories, two profiles (A: RAG developer on Python, Gemini, PostgreSQL and pgvector; B: AI agent learner), labels relevant / adjacent / irrelevant per profile, three duplicate pairs, muted-topic stories, an incidental mention and ambiguous terms (a crypto exchange called Gemini, RAG status reports, real-estate agents, Python snakes). Same data and labels for every method:

```bash
cd backend
python -m recsys_eval.run_eval --write                      # offline, deterministic, no network
python -m recsys_eval.run_eval --live --write               # real Gemini embeddings; vectors cached in embedding_cache.json
python -m recsys_eval.run_eval --live --mute-diagnostics    # similarity gaps per muted topic
```

Live run, `gemini-embedding-001` at 768 dimensions (`RESULTS.live.md`), mean of the two profiles:

| | Keyword | Embedding, facets only | Embedding, full ranking |
|---|---|---|---|
| precision@5 / nDCG@5 / recall@5 | 1.00 / 1.00 / 0.39 | 1.00 / 1.00 / 0.39 | 1.00 / 1.00 / 0.39 |
| precision@10 / nDCG@10 / recall@10 | 0.75 / 0.88 / 0.58 | 0.90 / 0.96 / 0.70 | 0.85 / 0.93 / 0.67 |
| mute violations @5 / @10 | 0 / 0 | 0 / 0 | 0 / 0 |
| duplicates @5 / @10 | 0.20 / 0.15 | 0.00 / 0.10 | 0.10 / 0.05 |
| mean ranking time, 50 stories | 3 ms | 41 ms | 46 ms |

Embedding cost: 1 batched API call for all 50 stories (0.02 calls per story, 1 text per story) plus 1 call per profile for its facets; about 2,200 input tokens in total (characters ÷ 4, an estimate because `embed_content` returns no token count). Cost is not reported because the pricing page does not list `gemini-embedding-001`; set `REC_EMBED_PRICE_PER_MILLION_TOKENS` or `--price-per-million`.

How to read these numbers:
- Top 5 is at the ceiling for every method, so it cannot tell them apart; the differences appear at 10. Two profiles and 50 stories give very small samples (one slot is 5 points of precision@10 per profile), the labels were written by the author of the evaluation, and the mute gaps were set from this same data. Treat it as a regression harness, not as proof of a gain.
- "Full ranking" (mutes, duplicate removal, MMR) gives up a little relevance for diversity, as intended. None of the mute-tagged stories reached the top 10 for any method, so mute handling is checked by unit and integration tests, not by this dataset.
- The offline mode is a deterministic hashed bag-of-words stand-in with no semantic knowledge. In it the keyword baseline wins (precision@10 0.75 against 0.60 and 0.70), so those numbers describe the ranking code, not Gemini.
- The dataset was revised once, after a first offline run showed keyword matching at 1.00 / 1.00 (precision@5 / nDCG@5) against 0.80 / 0.88 for the embedding stand-in: most positives reused the profile's own words, so several were rewritten as paraphrases. That revision was informed by a result, and the live numbers above come from the revised data only.

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest -q   # unit tests; integration tests need the emulators running
cd backend && .venv/Scripts/python -m recsys_eval.run_eval   # offline recommendation evaluation (add --live for Gemini)
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
- **Mostly never run against real services:** OpenAI text, Gemini TTS, Gmail and the deploy script were never run. Gemini embeddings were run once, on the synthetic `recsys_eval` dataset only (60 distinct short texts in 3 API calls); no briefing has run end to end with real providers.
- **Recommendation thresholds come from one embedding model and a small dataset:** the mute gaps were chosen from the live similarities of the 50-story `recsys_eval` set (so mute handling is not independently validated), and the duplicate threshold was checked against its three duplicate pairs. Re-run `recsys_eval --live --mute-diagnostics` after changing `GEMINI_EMBED_MODEL` or `EMBED_DIM`.
- **Stack, interests and expertise have no onboarding fields yet:** the API accepts them (`PUT /api/profile`) and they become facets, but the 4-step onboarding collects only the project, goals and mute topics.
