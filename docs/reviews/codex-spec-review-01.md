Reading additional input from stdin...
2026-07-05T02:48:04.095736Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\skills\blog-writing\SKILL.md: missing YAML frontmatter delimited by ---
2026-07-05T02:48:04.095819Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\skills\exam-design\SKILL.md: missing YAML frontmatter delimited by ---
2026-07-05T02:48:04.095835Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\skills\proposal-writing\SKILL.md: invalid YAML: invalid type: sequence, expected struct SkillFrontmatter at line 5 column 1
2026-07-05T02:48:04.096090Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\opcos\kpim-skills\0.1.0\skills\blog-writing\SKILL.md: missing YAML frontmatter delimited by ---
2026-07-05T02:48:04.096126Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\opcos\kpim-skills\0.1.0\skills\exam-design\SKILL.md: missing YAML frontmatter delimited by ---
2026-07-05T02:48:04.096139Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\opcos\kpim-skills\0.1.0\skills\proposal-writing\SKILL.md: invalid YAML: invalid type: sequence, expected struct SkillFrontmatter at line 5 column 1
2026-07-05T02:48:04.096151Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\opcos\kpim-skills\0.1.0\skills\ui-ux-pro-max\SKILL.md: invalid description: exceeds maximum length of 1024 characters
2026-07-05T02:48:04.096160Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\openai-curated\heygen\3fdeeb49\skills\heygen-avatar\SKILL.md: invalid description: exceeds maximum length of 1024 characters
2026-07-05T02:48:04.096169Z ERROR codex_core::session::session: failed to load skill C:\Users\DucNguyen\.codex\plugins\cache\openai-curated\heygen\3fdeeb49\skills\heygen-video\SKILL.md: invalid description: exceeds maximum length of 1024 characters
OpenAI Codex v0.137.0
--------
workdir: C:\Users\DucNguyen\Code\ai-record
model: gpt-5.5
provider: openai
approval: never
sandbox: read-only
reasoning effort: xhigh
reasoning summaries: none
session id: 019f302c-f366-7ab3-8c2e-5ee8bf759742
--------
user
You are an expert software architect doing an ADVERSARIAL design review of a specification. Read the file `docs/SPEC.md` in the current directory (a Windows-11 local real-time meeting transcription + translation + speaker-diarization desktop app: dual WASAPI loopback+mic capture, faster-whisper on GPU, NLLB-200 translation, 2-tier speaker diarization with online embedding clustering + offline pyannote, pluggable CLI summarizer, FastAPI + pywebview UI, all local).

Your job is to try to BREAK this design before code is written. Do not rubber-stamp it. Be concrete and specific — cite section numbers. For every issue give an actionable fix, not just a complaint.

Focus especially on:
1. FEASIBILITY of real-time performance: can faster-whisper large-v3 + tier-1 speaker-embedding extraction + NLLB translation realistically run concurrently on a single consumer GPU (8-12GB) fast enough to keep up with live speech? Where will it fall behind? What's the right default model and fallback ladder?
2. AUDIO CAPTURE correctness on Windows: WASAPI loopback pitfalls with `soundcard`/`pyaudiowpatch` — device sample-rate mismatch, format (float32 vs int16), buffer under/overruns, default-device changes mid-meeting, exclusive-mode conflicts, silent-loopback when nothing is playing. Is the ring-buffer + resample design correct?
3. SEGMENTATION & timebase: is the VAD chunking + cumulative-sample timebase actually consistent across two independently-clocked capture streams (loopback vs mic may drift)? Does tier-2 re-diarize alignment hold if the two WAVs have different lengths/drift?
4. DIARIZATION tier-1 online clustering: threshold selection, cold-start, speaker drift, the "new speaker vs same speaker" decision — is the specified approach sound or naive? Overlapping speech.
5. CONCURRENCY model: threads + asyncio + a single GPU worker + WebSocket broadcast — any deadlocks, races, backpressure gaps, or places the GPU worker starves/blocks the event loop?
6. DATA LOSS & crash safety: is per-utterance autosave enough? WAV file finalization on crash? Partial writes to JSONL.
7. SECURITY/PRIVACY: plaintext secrets (HF token, Gemini key), summarizer shelling out to `claude -p`/`codex exec` (command injection via transcript content? argument vs stdin), local server binding (is it 127.0.0.1 only?), consent gate.
8. ERROR HANDLING gaps and any missing edge cases.
9. SCOPE: anything in v1 that should be cut, or any critical missing piece that MUST be in v1.

Output your review as markdown with sections: `## Critical` (must fix before coding), `## Important` (should fix), `## Minor`, `## Suggestions/Improvements`. Under each, numbered actionable items citing spec section numbers. End with a one-paragraph `## Verdict` (is the spec implementation-ready, or what must change first). Be blunt and technical.
2026-07-05T02:48:04.373363Z ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed, when AuthRequired(AuthRequiredError { www_authenticate_header: "Bearer realm=\"OAuth\", resource_metadata=\"https://mcp.cloudflare.com/.well-known/oauth-protected-resource/mcp\", error=\"invalid_token\", error_description=\"Missing or invalid access token\"" })
hook: SessionStart
hook: SessionStart Completed
codex
I’ll read `docs/SPEC.md` and extract the numbered sections so the review can cite the spec precisely. The necessary command is a read-only `Get-Content` in the current repo.
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md" in C:\Users\DucNguyen\Code\ai-record
 succeeded in 872ms:
# ai-record Г?" Software Specification

**Version:** 1.0 (implementation-ready)
**Target platform:** Windows 11, Python 3.12, NVIDIA GPU with CUDA-enabled PyTorch
**Status:** Locked design. This document is the single source of truth for a first implementation. An engineer should be able to build v1 directly from it without further design decisions.

---

## 1. Overview

**ai-record** is a *local* meeting-scribe desktop application. While the user is in an online meeting (Microsoft Teams, Zoom, Google Meet, Webex, a browser call, or any app that plays audio through the speakers), ai-record:

1. **Captures** two audio streams simultaneously on Windows:
   - The **system audio mix** via WASAPI *loopback* of the default output device Г?" i.e. everything the speakers play, which includes remote participants. Labelled **"Them"**.
   - The **default microphone** Г?" the local user's own voice. Labelled **"You"**.
2. **Segments** each stream independently into utterances using voice-activity detection (VAD), dropping silence.
3. **Transcribes** each finalized utterance to text in near-real-time with faster-whisper on the GPU, detecting the spoken language.
4. **Translates** foreign-language utterances to Vietnamese live (optional, toggleable), using a local NLLB-200 model by default.
5. **Diarizes speakers** in two tiers: a rough real-time online-clustering pass (for the "Them" stream) and an accurate offline `pyannote.audio` re-diarization pass on demand after the meeting.
6. **Summarizes** the transcript on demand, post-meeting, by shelling out to a local AI CLI (Claude Code by default) or other pluggable providers, producing structured Markdown notes.
7. **Persists** every session (transcript, structured records, raw audio per source, summary) to disk, autosaving each utterance so a crash never loses data.

Everything runs **on the user's machine**. No audio or transcript leaves the computer except when the user explicitly invokes a cloud-based translation or summarization provider (both off/local by default).

The UI is a small **frameless, always-on-top** window (a compact bar by default, expandable to a full transcript view), implemented as a local web app served by FastAPI and wrapped in **pywebview**.

### 1.1 Why loopback capture

ai-record does **not** use any meeting platform's official recording API. It records the operating system's audio output (loopback) plus the microphone. This is the same technique used by mainstream AI note-takers (Otter, tl;dv, Fireflies, Fathom). A direct and honest technical consequence is that **the meeting platform displays no "recording" indicator**, because from the platform's perspective nothing is being recorded Г?" the OS is simply playing audio and ai-record is listening to the speaker output like any other audio app. See the **Legal & Consent** section (A3); this behavior must be framed to the user as a technical consequence, never marketed as an anti-detection or stealth feature.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1, in scope)

- Dual-stream WASAPI loopback + microphone capture, resampled to 16 kHz mono.
- Per-source VAD segmentation into utterances with low latency.
- Real-time GPU transcription with language detection and hallucination guards.
- Live optional translation of foreign speech Г+' Vietnamese (local NLLB default; Gemini stub pluggable).
- Two-tier speaker diarization: real-time rough (online clustering) + offline accurate (`pyannote.audio`).
- On-demand post-meeting AI summarization via pluggable CLI/local providers (Claude CLI default).
- Durable session storage with crash-safe autosave and a defined JSONL schema.
- FastAPI backend with WebSocket live push + REST control endpoints.
- Compact + expanded frameless always-on-top UI with settings, search, renameable speaker labels.
- JSON-persisted settings with documented keys and defaults.
- Graceful error handling for missing devices, GPU OOM, missing CLIs, missing HF token.
- Unit + integration test suites; documented manual smoke test.

### 2.2 Non-Goals (v1, explicitly out of scope)

- Perfect **overlapping-speech** separation (when two people talk at once). Best-effort only.
- `.exe` / installer packaging (PyInstaller etc.) Г?" noted as future work.
- Cloud STT (Whisper stays local). Cloud is only optionally used for translation/summarization.
- Non-Windows platforms (macOS/Linux). WASAPI loopback is Windows-specific.
- Mobile / web-hosted / multi-user deployments.
- Speaker *identification* against a named voiceprint database (we only cluster into anonymous, renameable "Speaker N").
- Real-time translation of the user's own outgoing speech beyond the same pipeline treatment (no TTS back-translation).

---

## 3. Legal & Consent (READ FIRST)

> **This section MUST be surfaced in the app** (a first-run modal that the user must acknowledge, plus a permanent link in Settings). The text below is the normative content.

ai-record captures audio by recording your computer's audio output (WASAPI loopback) together with your microphone. Because it does **not** use the meeting platform's recording feature, **the meeting platform will not show a recording indicator to other participants.** This is a technical consequence of loopback capture Г?" the app is listening to your speakers the same way any audio app does Г?" and **not** a feature designed to hide recording from anyone.

**Recording other people without their knowledge or consent may be illegal.** Many jurisdictions have wiretap / eavesdropping / "two-party (all-party) consent" laws (for example, several U.S. states such as California, Florida, and Illinois; and various national laws in the EU and elsewhere) that make it unlawful to record a conversation unless **everyone** being recorded has consented. Rules differ widely by country, state, and context (workplace, private call, public meeting).

**ai-record is intended for personal note-taking of meetings you are a participant in.** You, the user, are solely responsible for complying with the law that applies to you and to the other participants. Where the law requires it, **you must obtain consent from, and/or disclose the recording to, all participants** before recording. When in doubt, ask and disclose; many organizations require an explicit verbal or written notice at the start of a call.

The developers of ai-record provide the software "as is" and are not liable for unlawful use. Using this software to record others without required consent may expose you to civil and/or criminal liability.

**Implementation requirements tied to this section:**
- On first run, show a modal containing the above text with an "I understand and agree" button. Persist acknowledgement in settings (`consent_acknowledged: true`, with `consent_acknowledged_at` timestamp). Do not enable the Start button until acknowledged.
- Provide a "Legal & Consent" link in the expanded window's Settings that reopens this text at any time.
- Do **not** add any UI copy, tooltips, or marketing that describes the no-indicator behavior as "undetectable", "stealth", "invisible recording", or similar. Describe it factually if at all.

---

## 4. Architecture

### 4.1 High-level model

ai-record is a single Python process that runs:
- A **FastAPI + Uvicorn** server (HTTP + WebSocket) on `127.0.0.1` (localhost only, never `0.0.0.0`).
- A **capture/processing pipeline** running in background threads and asyncio tasks.
- A **pywebview** window that loads the local web UI (`http://127.0.0.1:<port>`).

The pipeline is a chain of producer/consumer stages connected by bounded queues. Each source ("You", "Them") has its own capture Г+' ring buffer Г+' segmenter. Finalized utterances from both sources are merged into a single work queue consumed by the transcription worker, then translation, then real-time diarization, then persistence + WebSocket broadcast.

### 4.2 ASCII data-flow diagram

```
                          WINDOWS AUDIO SUBSYSTEM (WASAPI)
        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?   Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
        Г", Default OUTPUT device          Г",   Г", Default INPUT (mic)      Г",
        Г", (speakers) Г?" LOOPBACK          Г",   Г",                          Г",
        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~   Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г", raw frames (native SR)          Г", raw frames (native SR)
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", capture.py        Г",              Г", capture.py        Г",
              Г",  loopback stream  Г",              Г",  mic stream       Г",
              Г",  Г+' resample 16k   Г",              Г",  Г+' resample 16k   Г",
              Г",  Г+' mono           Г",              Г",  Г+' mono           Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г", 16k mono float32 frames          Г",
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", RingBuffer "Them" Г",              Г", RingBuffer "You"  Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", segmenter.py      Г",              Г", segmenter.py      Г",
              Г", VAD (Silero)      Г",              Г", VAD (Silero)      Г",
              Г", Г+' utterance chunk Г",              Г", Г+' utterance chunk Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г",  Utterance{source, pcm, t0, t1}  Г",
                        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г",  utterance_queue  Г",  (bounded, backpressure)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", transcriber.py    Г",  faster-whisper (GPU, fp16)
                             Г", text + lang       Г",  VAD filter (secondary)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", translator.py     Г",  NLLB (if enabled & langГ% vi)
                             Г", + translation     Г",
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", diarizer.py (T1)  Г",  ECAPA embed + online cluster
                             Г", speaker label     Г",  ("You" is fixed for mic)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"'Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                        Г-м                              Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", store.py          Г",        Г", server.py WebSocket   Г",
              Г", append JSONL +    Г",        Г", push live utterance   Г",
              Г", transcript.md +   Г",        Г", Г+' UI (compact/expand) Г",
              Г", audio_*.wav       Г",        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~

   POST-MEETING (on demand):
     summarizer.py  Г"?Г"? reads transcript TEXT Г"?Г"?Г-з summary.md
     diarizer.py T2 Г"?Г"? reads audio_*.wav (pyannote) Г"?Г"?Г-з relabel transcript
```

### 4.3 Concurrency & backpressure model

- **Capture threads (2):** one per source. Each is a dedicated OS thread (audio callbacks / blocking record loops). They write fixed-size frames into a lock-free-ish `RingBuffer` (a `numpy` circular buffer guarded by a `threading.Lock`, or `collections.deque` of frames). Capture must never block on downstream work; if the ring buffer is full it overwrites oldest data and increments a `dropped_frames` counter (logged, surfaced as a warning if sustained).
- **Segmenter threads (2):** one per source. Each pulls frames from its ring buffer, runs VAD, and emits `Utterance` objects onto the shared bounded `utterance_queue` (`queue.Queue(maxsize=32)`).
- **Transcription worker (1 thread):** the GPU is a single serialized resource. One worker pulls from `utterance_queue` and runs faster-whisper. **Backpressure:** if the queue is full, segmenters block on `put()` Г?" but since segments are only produced at speech boundaries and transcription is faster-than-realtime on GPU for `large-v3` with short utterances, this is rarely hit. If it is hit repeatedly (queue full > `backpressure_warn_seconds`), the app: (a) logs a warning, (b) surfaces a "falling behind" status dot, and (c) optionally auto-downgrades the model per settings (`auto_downgrade_on_backpressure`).
- **Translation + T1 diarization:** run inline on the transcription worker thread *or* on a small follow-on worker (`post_queue`, maxsize=32). Recommended: keep transcription worker lean (STT only) and run translate+diarize on a separate `post_worker` thread so GPU STT is never stalled by translation model inference. Both translation (NLLB) and diarization embeddings can share the GPU; serialize GPU access with a single `torch` device lock if VRAM is tight (see A11.5).
- **Persistence + broadcast:** the `post_worker` (or a dedicated `sink` thread) writes to `store.py` (append-only, fast) and publishes to an asyncio broadcast. Because FastAPI/WebSocket lives on the asyncio event loop and workers are threads, cross into the loop via `asyncio.run_coroutine_threadsafe(broadcast(msg), loop)` or an `asyncio.Queue` fed through `loop.call_soon_threadsafe`.
- **Bridge (threads Г+" asyncio):** the server holds a reference to the running event loop. Worker threads enqueue outgoing WS messages using `loop.call_soon_threadsafe(async_queue.put_nowait, msg)`. A single async task drains `async_queue` and fans out to all connected WS clients.
- **Ordering:** utterances may finish transcription slightly out of wall-clock order across sources (a long "Them" utterance vs a short "You" one). Each utterance carries `start`/`end` timestamps (seconds from session start). The UI orders by `start`. `store.py` appends in completion order to JSONL but each record has authoritative timestamps; the rendered `transcript.md` is sorted by `start` on finalize/close.

### 4.4 Timebase

A single monotonic session clock starts at capture start (`session_t0 = time.perf_counter()`). All utterance `start`/`end` are seconds relative to `session_t0`, derived from sample counts in each source's stream (sample_index / 16000) to keep audio and transcript aligned for tier-2 re-diarization. Capture start for both streams is recorded; a small per-stream offset (measured start delta) is stored so audio files and timestamps line up.

---

## 5. Components

Each subsection: **Responsibility A· Public interface A· Dependencies A· Key algorithms / details.**

### 5.1 `capture.py` Г?" audio capture

**Responsibility.** Open and run two simultaneous WASAPI streams (loopback of default output = "Them"; default microphone = "You"), convert both to 16 kHz mono float32, push frames into per-source ring buffers, and also tee raw audio to the per-source WAV writers in `store.py`. Handle device changes and missing devices.

**Public interface.**
```python
class AudioFrame:  # lightweight
    source: str          # "you" | "them"
    pcm: np.ndarray      # float32, mono, 16000 Hz, shape (N,)
    n_samples: int
    t_start: float       # seconds since session_t0

class CaptureSource:
    source: str                     # "you" | "them"
    available: bool
    device_name: str | None

class CaptureManager:
    def __init__(self, ring_you: RingBuffer, ring_them: RingBuffer,
                 wav_you: WavWriter | None, wav_them: WavWriter | None,
                 settings: Settings, on_status): ...
    def start(self) -> list[CaptureSource]:   # returns which sources came up
    def stop(self) -> None
    def sources_status(self) -> list[CaptureSource]
    # callback on_status(source, event) for device add/remove/error
```

**Dependencies.** `soundcard` (preferred) or `pyaudiowpatch` (fallback); `numpy`; `scipy.signal` or `soxr`/`resampy` for resampling; `store.WavWriter`.

**Key details.**
- **Library selection.** Try `soundcard` first: it exposes loopback recorders on Windows via `soundcard.default_speaker()`'s loopback and `soundcard.default_microphone()`. If import/init fails, fall back to `pyaudiowpatch` (a PyAudio fork exposing WASAPI loopback). Selection is automatic but overridable by setting `audio_backend: "auto"|"soundcard"|"pyaudiowpatch"`.
  - *soundcard path:* obtain loopback microphone for the default speaker via `soundcard.get_microphone(id=default_speaker.name, include_loopback=True)`; record with `.recorder(samplerate=native, channels=Г?▌, blocksize=Г?▌)`.
  - *pyaudiowpatch path:* use `get_default_wasapi_loopback()` to find the loopback device index; open an input stream on it. Open the mic as a normal WASAPI input stream.
- **Two independent streams.** Each runs in its own thread with its own recorder context manager. The loopback stream's native format is whatever the output mixer runs at (commonly 48 kHz stereo). The mic may be 44.1/48 kHz mono/stereo.
- **Downmix to mono.** Average channels (`pcm.mean(axis=1)`) if multi-channel.
- **Resample to 16 kHz.** Use a high-quality polyphase resampler. Preferred: `soxr.resample(x, in_rate, 16000)` (fast, good quality). Fallback: `scipy.signal.resample_poly` with computed up/down from `gcd(in_rate, 16000)`. Resampling is stateful across blocks: maintain per-stream filter state or use overlap; with `soxr` use a streaming `soxr.ResampleStream` to avoid block-edge artifacts.
- **Frame size.** Emit frames of a fixed hop (e.g. 20 ms = 320 samples @16 kHz) to feed VAD cleanly; internally read larger blocks from the device (e.g. 100Г?"200 ms) and re-chunk.
- **Raw persistence.** As frames are produced (post-resample), also append them to `audio_you.wav` / `audio_them.wav` via `store.WavWriter` (16 kHz, mono, 16-bit PCM). This is required for tier-2 re-diarization (A5.5) and is always on while capturing (unless `persist_audio: false`, which disables tier-2).
- **Device-change handling.** WASAPI default-device changes (user switches headset, plugs in HDMI) invalidate a stream. Detect via: (a) recorder raising/returning an error, or (b) a periodic (every 2 s) check of the current default device id vs the one the stream was opened on. On change: stop the affected stream, attempt to reopen on the new default device (up to `device_reopen_retries`, default 5, with 500 ms backoff), emit `on_status(source, "reopened"|"lost")`. Do not touch the other stream. During a gap, VAD simply sees silence; the session continues.
- **Missing-device handling.** If loopback cannot be opened (no output device, or exclusive-mode conflict), start with only the mic and mark `them.available=false`. If the mic is missing or blocked (Windows privacy setting), start with only loopback and mark `you.available=false`. If **both** fail, `start()` returns an empty list; the server reports an error and does not enter the recording state. At least one source is required.
- **No exclusive mode.** Always open shared-mode WASAPI so we don't seize the device from the meeting app.

### 5.2 `segmenter.py` Г?" VAD segmentation

**Responsibility.** Convert a continuous 16 kHz mono stream (via its ring buffer) into discrete utterance chunks bounded by natural pauses, dropping silence, keeping latency low. One instance per source.

**Public interface.**
```python
class Utterance:
    source: str          # "you" | "them"
    pcm: np.ndarray      # float32 16k mono, the utterance audio
    start: float         # seconds since session_t0
    end: float

class Segmenter:
    def __init__(self, source: str, settings: Settings): ...
    def run(self, ring: RingBuffer, out_queue: queue.Queue, stop_event): ...
    # pulls frames, emits Utterance to out_queue
```

**Dependencies.** `silero-vad` (preferred, torch model) or `webrtcvad` (fallback, pure C, no GPU); `numpy`.

**Key algorithm (streaming VAD state machine).**
- VAD is evaluated on fixed frames (Silero works on 30 ms / 512-sample windows @16 kHz; webrtcvad on 10/20/30 ms). Produce a per-frame speech probability (Silero) or boolean (webrtcvad, aggressiveness `vad_aggressiveness` 0Г?"3, default 2).
- State machine per source:
  - **IDLE** Г+' accumulate a short rolling pre-roll buffer (`pre_roll_ms`, default 300 ms) so we don't clip word onsets.
  - Transition to **SPEECH** when `speech` sustained for `speech_start_ms` (default 150 ms). Prepend pre-roll to the utterance.
  - In **SPEECH**, append frames. Track trailing silence.
  - End the utterance (**Г+' IDLE, emit**) when trailing silence exceeds `silence_end_ms` (default 600 ms) **or** the utterance reaches `max_utterance_seconds` (default 15 s Г?" force a cut mid-speech to bound latency; the next chunk continues).
  - Discard utterances shorter than `min_speech_ms` (default 250 ms) as noise.
- **Max-length cut:** when forced, cut at the most recent low-energy frame within the last 500 ms if possible (avoid slicing mid-word); otherwise cut hard. Mark `forced_cut=true` internally (not persisted) so the transcriber can optionally overlap-pad.
- **Silero specifics.** Use the packaged VAD (`silero_vad` pip, or torch.hub). Keep the model on CPU (tiny) to leave GPU headroom for Whisper, unless `vad_device: "cuda"`. Reset internal RNN state between utterances.
- **webrtcvad fallback.** No probabilities Г?" use a hangover counter (N consecutive speech/nonspeech frames) to emulate start/stop hysteresis.
- **Latency budget.** End-of-utterance latency Г%^ `silence_end_ms` (0.6 s) + transcription time. This is the primary tunable for perceived responsiveness.
- **Two independent instances** ("you", "them") run concurrently and never share state.

### 5.3 `transcriber.py` Г?" speech-to-text

**Responsibility.** Transcribe each finalized `Utterance` to text using faster-whisper on the GPU; detect language; guard against hallucinations on near-silent input.

**Public interface.**
```python
class Transcript:
    source: str
    start: float
    end: float
    text: str
    lang: str            # ISO-639-1, e.g. "en", "ja", "vi", "zh", "ko"
    lang_prob: float
    avg_logprob: float
    no_speech_prob: float

class Transcriber:
    def __init__(self, settings: Settings): ...
    def load(self) -> None            # loads model, may fall back on OOM
    def transcribe(self, utt: Utterance) -> Transcript | None   # None if dropped
    def current_model(self) -> str
```

**Dependencies.** `faster-whisper` (CTranslate2 backend), CUDA torch runtime present; `numpy`.

**Key details.**
- **Model.** `WhisperModel(model_size, device="cuda", compute_type="float16")`. `model_size` from settings, default `large-v3`; also allow `medium`, `small`, `large-v2`. Model loads once at capture start (or lazily on first utterance) and is reused.
- **Per-utterance transcription.** Call `model.transcribe(pcm, language=None or fixed, vad_filter=True, beam_size=Г?▌, temperature=[0.0, 0.2, 0.4], condition_on_previous_text=False)`.
  - `condition_on_previous_text=False` Г?" critical: prevents cross-utterance hallucination drift and keeps utterances independent.
  - `vad_filter=True` Г?" built-in Silero VAD as a **secondary** guard (segmenter already gated, but this trims residual silence inside the chunk). Configurable `whisper_vad_filter` (default true).
  - `language`: if `force_language` is set (e.g. user knows the meeting is Japanese), pass it; otherwise `None` for auto-detect. Auto-detect returns `info.language`, `info.language_probability`.
  - `beam_size`: default 5 (quality) but `beam_size=1` when in low-latency mode (`latency_mode: "fast"`).
- **Concatenate segments.** faster-whisper returns segment iterator; join `.text`, take earliest start / latest end, average `avg_logprob`, take max `no_speech_prob`.
- **Hallucination guards** (Whisper invents text like "Thank you." / "Thanks for watching" on silence/noise). Drop the transcript (return `None`) when ANY:
  - `no_speech_prob > no_speech_threshold` (default 0.6) AND `avg_logprob < logprob_drop_threshold` (default Г^'1.0).
  - Text (after strip) matches a configurable **hallucination denylist** (case-insensitive, whole-utterance): `["thank you", "thanks for watching", "please subscribe", "a?", "a?"К▌-К?'a?,a,Sa?Oa?"a?+a?"a?-a?,a?_a?-a?Y", "Ж--Ж1", ...]` Г?" provide a default list in config, user-extendable.
  - Text is empty or only punctuation/whitespace.
  - Utterance audio RMS below `min_rms` (belt-and-suspenders vs VAD false-positives).
- **GPU OOM fallback.** Wrap load + transcribe in try/except for `RuntimeError`/CUDA OOM. On OOM: (1) `torch.cuda.empty_cache()`, (2) reload at the next smaller model (`large-v3`Г+'`medium`Г+'`small`), (3) if still failing, switch `compute_type` to `int8_float16` then `int8`. Emit a status event describing the downgrade. Persist the effective model in session metadata. Never crash the pipeline on OOM.
- **Threading.** Single transcription worker (the GPU is serialized). See A4.3.

### 5.4 `translator.py` Г?" live translation

**Responsibility.** Translate foreign-language utterance text into Vietnamese, live, when enabled. Pluggable providers behind one interface.

**Public interface.**
```python
class Translator(Protocol):
    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str: ...
    def available(self) -> bool: ...

class NllbTranslator(Translator):   # default
    def __init__(self, settings: Settings): ...

class GeminiTranslator(Translator): # stub, off by default
    def __init__(self, settings: Settings): ...
```

**Dependencies.** Default: `transformers` (+ `sentencepiece`) or `ctranslate2` for NLLB-200 distilled-600M; `torch`. Gemini stub: `google-generativeai` (optional, only if user enables + provides key).

**When to translate.** The pipeline calls `translate()` for an utterance **only if ALL**:
1. `translate_enabled` is true (Settings / "Dап<ch" toggle), AND
2. detected `lang != target_lang` (`target_lang` default `"vi"`), AND
3. `lang` Г^^ `source_languages` (user-selected set of source languages to translate; empty set = "translate any non-target language").

Otherwise the utterance is passed through with `translation=None`.

**NLLB details.**
- Model: `facebook/nllb-200-distilled-600M`. Load once; keep on GPU (`device="cuda"`) if VRAM allows, else CPU (still usable for short text). `compute_type` int8 on CTranslate2 for speed.
- **Language code mapping (Whisper ISO-639-1 Г+' NLLB FLORES code).** Maintain a dict; ship at least:
  | Whisper | NLLB |
  |--------|------|
  | `ja` | `jpn_Jpan` |
  | `vi` | `vie_Latn` |
  | `en` | `eng_Latn` |
  | `zh` | `zho_Hans` |
  | `ko` | `kor_Hang` |
  | `fr` | `fra_Latn` |
  | `de` | `deu_Latn` |
  | `es` | `spa_Latn` |
  | `ru` | `rus_Cyrl` |
  | `th` | `tha_Thai` |
  Target `vi` Г+' `vie_Latn`. If a detected `lang` has no mapping, skip translation (pass through) and log once.
- Inference: set `tokenizer.src_lang = <nllb_src>`, generate with `forced_bos_token_id = tokenizer.convert_tokens_to_ids(<nllb_tgt>)`, `max_length` scaled to input length, `num_beams` default 3.
- **Quality note (must appear in code comment + docs):** NLLB JapaneseГ+'Vietnamese quality is *moderate*; for higher-quality JAГ+'VI (or other hard pairs), the user can switch `translation_provider` to `gemini`, which improves results at the cost of sending text to Google. This is a deliberate quality/privacy tradeoff exposed in Settings.

**GeminiTranslator (stub).** Implements the same interface. `available()` returns true only if `gemini_api_key` is set. `translate()` calls the Gemini API with a terse system prompt ("Translate the following {src} text to Vietnamese. Output only the translation."). Off by default. If selected but no key, `available()` is false Г+' UI shows a clear "set your Gemini key" message and falls back to NLLB (or no translation).

**Error handling.** Any translation exception Г+' return original text unchanged is **wrong** (would look like a translation). Instead return `None`/empty and mark `translation_error` on the record; the UI shows the original with a small "translation failed" hint. Never block the pipeline.

### 5.5 `diarizer.py` Г?" speaker diarization (two tiers)

**Responsibility.** Assign a speaker label to each utterance. Tier 1 is real-time and rough; Tier 2 is offline and accurate.

**Public interface.**
```python
class RealtimeDiarizer:   # Tier 1
    def __init__(self, settings: Settings): ...
    def label(self, utt: Utterance) -> str      # "You" for mic; "Speaker N" for loopback
    def reset(self) -> None
    def rename(self, old_label: str, new_label: str) -> None

class OfflineDiarizer:    # Tier 2
    def __init__(self, settings: Settings): ...
    def rediarize(self, session_dir: str) -> list[SpeakerSpan]   # relabels transcript
```

**Tier 1 Г?" real-time online clustering.**
- **"You" (mic source):** always labelled `"You"`. No embedding needed.
- **"Them" (loopback):** for each utterance, extract a fixed-dim speaker embedding and assign it online.
  - **Embedding model:** ECAPA-TDNN via SpeechBrain (`speechbrain/spkrec-ecapa-voxceleb`) Г?" preferred. Fallback: Resemblyzer (`VoiceEncoder`) Г?" lighter, CPU-friendly. Selectable via `diarization_embedder: "ecapa"|"resemblyzer"`.
  - **Online clustering algorithm:** maintain a list of centroids `{label: (mean_embedding, count)}`.
    1. Compute embedding `e` (L2-normalized) for the utterance.
    2. If utterance shorter than `min_embed_ms` (default 800 ms), skip clustering and label `"Speaker ?"` (uncertain) Г?" too short for a reliable embedding.
    3. Find nearest centroid by cosine similarity. If `max_sim >= sim_threshold` (default 0.70) assign that label and update the centroid as a running mean (weighted by count, capped so it stays adaptive).
    4. Else create a new label `"Speaker {n+1}"` with `e` as its centroid.
  - **Cap** the number of speakers at `max_speakers` (default 8); beyond that, force-assign to nearest to avoid runaway label creation on noisy embeddings.
  - **Renaming:** `rename()` updates the label map; new incoming utterances that match the centroid keep the new name. Renames also propagate to already-emitted records via a store update (A5.7) and a WS `rename` event.
- **Toggle / cost:** controlled by `diarization_enabled` (Settings, default **ON**) and `diarization_realtime` (default ON). When realtime diarization is off, all "Them" utterances are labelled `"Them"` (single bucket). The embedding model adds VRAM (~1 GB ECAPA on GPU) and per-utterance latency (~50Г?"150 ms). If VRAM is tight, run the embedder on CPU (`diarization_device: "cpu"`) Г?" slower but non-blocking to Whisper. Document this VRAM/latency tradeoff in Settings help text.

**Tier 2 Г?" offline accurate re-diarization.**
- Triggered by the UI "Re-diarize" button Г+' REST Г+' runs `OfflineDiarizer.rediarize(session_dir)`.
- **Requires** the persisted audio. Re-diarize primarily on `audio_them.wav` (remote speakers) Г?" "You" stays "You". Optionally also process a **mixed** track if desired, but v1: run pyannote on `audio_them.wav` only, keep mic as "You".
- **Model:** `pyannote/speaker-diarization-3.1` pipeline via `pyannote.audio`. `Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)`. Run on GPU (`pipeline.to(torch.device("cuda"))`).
- **HuggingFace token** (free) required; user accepts the model's gated terms once on HF and pastes the token into Settings (`hf_token`). If missing, tier-2 is **disabled** and the button shows a clear message + link to instructions. See A6 & A8.
- **Relabelling algorithm:** pyannote returns speaker-homogeneous time spans with cluster ids (SPEAKER_00, Г?▌). For each existing "Them" utterance record, compute the overlap-weighted majority pyannote speaker over `[start, end]` and assign a stable label (`"Speaker A/B/Г?▌"`). Preserve any user renames by offering a mapping step (v1: apply pyannote clusters as fresh labels, but keep a `diarization_source` field so the UI shows "re-diarized" state). Rewrite `transcript.jsonl` speaker fields (new file version) and re-render `transcript.md`; keep a backup `transcript.jsonl.pre-rediarize`.
- Long-running: runs in a background thread; report progress/completion via WS + REST status.

### 5.6 `summarizer.py` Г?" AI summarization

**Responsibility.** Produce structured Markdown notes from the transcript **text** (not audio), once, on demand, post-meeting. Pluggable providers behind one interface.

**Public interface.**
```python
class Summarizer(Protocol):
    def summarize(self, transcript_text: str, meta: dict) -> str: ...  # returns markdown
    def available(self) -> tuple[bool, str]:  ...  # (ok, reason_if_not)

class ClaudeCliSummarizer(Summarizer):   # default
class CodexCliSummarizer(Summarizer):
class GeminiSummarizer(Summarizer):
class OllamaSummarizer(Summarizer):
```

**Selection.** `summarizer_provider` setting: `"claude_cli" | "codex_cli" | "gemini" | "ollama"`, default `"claude_cli"`.

**Input assembly.** Build a plain-text transcript from `transcript.jsonl`, one line per utterance: `"[mm:ss] <SpeakerLabel>: <text>"`, using the translated Vietnamese where translation exists (configurable `summary_use_translation`, default true) else original. Include a short header with meeting title/date/duration/languages. If the transcript is very long, chunk to fit context (`summary_max_chars`, default 48000) Г?" map-reduce: summarize chunks then summarize the summaries. Document the chunking.

**Prompt (default).** The prompt must let the model **self-organize** into whatever sections fit the content Г?" do not hard-force a fixed template. Ship this default (Vietnamese-first output):
```
You are an assistant that writes clear, well-structured meeting notes in Vietnamese.
Read the transcript below (speaker-labelled). Produce concise notes in Markdown.
Organize into whatever sections actually fit this meeting Г?" for example (only if relevant):
key points, decisions, action items (with owner if stated), open questions / Q&A,
risks or warnings, and misc notes. Omit sections that don't apply. Do not invent content
not supported by the transcript. Keep it skimmable.

TRANSCRIPT:
<transcript_text>
```
Prompt text lives in config (`summary_prompt`) and is user-editable.

**Provider implementations.**
- **ClaudeCliSummarizer (default):** shell out headless: `claude -p "<prompt+transcript>"` (or pass the prompt via stdin to avoid arg-length limits Г?" **preferred**: `claude -p` reading prompt from stdin / a temp prompt file). Capture stdout as the markdown. Use `subprocess.run([...], capture_output=True, text=True, timeout=summary_timeout_s, encoding="utf-8")`.
- **CodexCliSummarizer:** `codex exec` with the prompt (via stdin/temp file), capture stdout.
- **GeminiSummarizer:** call Gemini API with the prompt (needs `gemini_api_key`).
- **OllamaSummarizer:** POST to local Ollama (`http://localhost:11434/api/generate`, model `ollama_model` default `llama3.1`), stream/collect `response`.

**"CLI not installed" / unavailable handling.** `available()` probes: for CLI providers, check the binary exists (`shutil.which("claude")` / `"codex"`) and optionally a `--version`. If missing, `summarize()` is not called; UI shows: *"Claude CLI not found. Install it, or choose another summarizer (Codex / Gemini / Ollama) in Settings."* Same for `codex`, for a missing Ollama server (connection refused), and for a missing Gemini key. Never crash; return a clear error string that the UI renders as a notice (not saved as `summary.md`).

**Output.** On success, save markdown to `<session_dir>/summary.md` (overwrite allowed; keep a `.bak` of the previous). Return it over REST/WS so the UI shows it. Record `summary_provider` and `summarized_at` in session metadata.

**Windows subprocess notes.** Use `text=True, encoding="utf-8"`; set `creationflags=subprocess.CREATE_NO_WINDOW` so no console flashes. Prefer passing the prompt via a temp file path or stdin (arg length + quoting on Windows is fragile for long transcripts).

### 5.7 `store.py` Г?" session storage

**Responsibility.** Durable, crash-safe persistence of each session; autosave every finalized utterance; expose read APIs for listing/opening sessions; support tier-2 rewrite and speaker renames.

**Public interface.**
```python
class WavWriter:
    def __init__(self, path: str, samplerate=16000, channels=1): ...
    def write(self, pcm: np.ndarray) -> None
    def close(self) -> None

class SessionStore:
    def __init__(self, sessions_root: str): ...
    def create(self, title: str) -> Session          # makes folder + opens files
    def append_utterance(self, rec: UtteranceRecord) -> None   # autosave (jsonl + md)
    def rename_speaker(self, session_id, old, new) -> int      # updates records + md
    def write_summary(self, session_id, markdown: str) -> None
    def rewrite_after_rediarize(self, session_id, new_labels) -> None
    def list_sessions(self) -> list[SessionMeta]
    def load_session(self, session_id) -> SessionData
    def finalize(self, session_id) -> None            # sort md, write meta, close wavs
```

**Session folder layout.**
```
sessions/
  20260705-142530-standup/
    meta.json            # session metadata (see below)
    transcript.jsonl     # one JSON object per utterance (append-only, authoritative)
    transcript.md        # human-readable, rendered/sorted by start time
    summary.md           # created on demand (may be absent)
    audio_you.wav        # 16 kHz mono PCM16 mic capture (may be absent if mic missing)
    audio_them.wav       # 16 kHz mono PCM16 loopback capture (may be absent)
    transcript.jsonl.pre-rediarize   # backup, only if tier-2 ran
    summary.md.bak       # backup, only if re-summarized
```
- `session_id` = folder name = `<YYYYMMDD-HHMMSS>-<slug(title)>`. Title default = "meeting"; slug = lowercased, non-alnumГ+'`-`, trimmed, max 40 chars.
- `sessions_root` default `./sessions/` under the app data dir (see A10).

**Autosave.** `append_utterance` is called for every finalized utterance and must be cheap + durable:
- Append one line to `transcript.jsonl` (open in `"a"`, `encoding="utf-8"`), then `f.flush()` and `os.fsync(fileno)` on a throttle (fsync at most every `fsync_interval_ms`, default 1000 ms, to bound overhead while bounding data loss to ~1 s). WAV writers similarly flush periodically.
- Append a rendered line to `transcript.md` incrementally (unsorted, in completion order) so a crash still leaves readable text; the sorted, clean `transcript.md` is regenerated on `finalize()`.
- Crash safety: because JSONL is append-only + flushed, a crash loses at most the last unflushed line. On next open, a partial trailing line (no newline) is ignored/truncated.

**JSONL utterance record schema.** One object per line:
```json
{
  "id": "u_000123",                // stable per-utterance id, zero-padded seq
  "session_id": "20260705-142530-standup",
  "seq": 123,                      // completion order
  "source": "them",                // "you" | "them"
  "speaker": "Speaker 2",          // display label (renameable / re-diarized)
  "start": 842.31,                 // seconds since session_t0
  "end": 846.07,
  "duration": 3.76,
  "text": "ДмsК-°a,'Ж<a,?a?_a?-a,╪a?+a?,",     // original transcription
  "lang": "ja",                    // detected language (ISO-639-1)
  "lang_prob": 0.98,
  "translation": "HAЬy bаз_t О`азu cuапTc hап?p.",  // Vietnamese, or null if not translated
  "translation_provider": "nllb",  // "nllb" | "gemini" | null
  "translation_error": false,
  "no_speech_prob": 0.04,
  "avg_logprob": -0.31,
  "diarization_source": "realtime",// "realtime" | "offline" | "manual"
  "created_at": "2026-07-05T14:39:12.031+07:00",
  "schema": 1
}
```
- Missing/optional fields (`translation`, etc.) are `null`. `schema` version enables future migrations.

**`meta.json` schema.**
```json
{
  "session_id": "20260705-142530-standup",
  "title": "standup",
  "created_at": "2026-07-05T14:25:30+07:00",
  "ended_at": "2026-07-05T15:02:11+07:00",
  "duration_sec": 2201,
  "sources": {"you": true, "them": true},
  "whisper_model": "large-v3",
  "compute_type": "float16",
  "translate_enabled": true,
  "target_lang": "vi",
  "source_languages": ["ja", "en"],
  "translation_provider": "nllb",
  "diarization_enabled": true,
  "speakers": {"Speaker 1": "Tanaka-san", "Speaker 2": "Suzuki"},  // rename map
  "summary_provider": "claude_cli",
  "summarized_at": null,
  "rediarized_at": null,
  "app_version": "1.0",
  "schema": 1
}
```

**`transcript.md` rendering.** Sorted by `start`. Format per utterance:
```
**[14:39:12] Speaker 2 (ja):** ДмsК-°a,'Ж<a,?a?_a?-a,╪a?+a?,
> HAЬy bаз_t О`азu cuапTc hап?p.
```
(Translation line only when present. Timestamp shown as wall-clock derived from `created_at`/`start`.)

**Renames.** `rename_speaker` updates all matching records' `speaker` in `transcript.jsonl` (rewrite the file atomically: write to temp, `os.replace`), updates `meta.json.speakers`, and re-renders `transcript.md`. Returns count updated.

### 5.8 `server.py` Г?" FastAPI backend

**Responsibility.** Host the HTTP API + WebSocket, own the pipeline lifecycle, serve the web UI, bridge worker threads to the event loop.

**Dependencies.** `fastapi`, `uvicorn`, `pydantic`, the components above, `config.Settings`.

**Server config.** Bind `127.0.0.1` on a fixed default port `8848` (configurable `server_port`); if occupied, try next 10 ports and report the chosen one to pywebview. CORS locked to localhost. No auth (localhost-only, single user).

**REST endpoints.**
| Method | Path | Body / Params | Returns |
|-------|------|---------------|---------|
| `POST` | `/api/capture/start` | `{title?: str}` | `{session_id, sources: {you, them}}` Г?" starts pipeline; errors if both sources fail |
| `POST` | `/api/capture/stop` | Г?" | `{session_id, finalized: true}` Г?" stops capture, finalizes session |
| `GET` | `/api/capture/status` | Г?" | `{recording, session_id, sources, dropped_frames, backpressure, model}` |
| `GET` | `/api/sessions` | Г?" | `[SessionMeta]` (list, newest first) |
| `GET` | `/api/sessions/{id}` | Г?" | full session: meta + utterances (+ summary if present) |
| `POST` | `/api/sessions/{id}/summarize` | `{provider?}` | `{markdown}` or `{error}` (runs summarizer) |
| `POST` | `/api/sessions/{id}/rediarize` | Г?" | `{status:"started"}`; completion via WS |
| `POST` | `/api/sessions/{id}/speakers/rename` | `{old, new}` | `{updated: n}` |
| `GET` | `/api/settings` | Г?" | full settings object |
| `PUT` | `/api/settings` | partial settings | updated settings (validated) |
| `GET` | `/api/health` | Г?" | `{ok, gpu, cuda, models_loaded}` |
| `GET` | `/` and `/static/*` | Г?" | serves `web/` UI |

**WebSocket** `GET /ws`:
- Server Г+' client message types:
  - `{"type":"utterance", "record": <UtteranceRecord>}` Г?" a new finalized, translated, diarized utterance.
  - `{"type":"status", "recording":bool, "sources":{Г?▌}, "backpressure":bool, "model":str, "note":str}`.
  - `{"type":"rename", "old":str, "new":str}`.
  - `{"type":"rediarize", "state":"started|progress|done|error", "detail":Г?▌}`.
  - `{"type":"summary", "state":"started|done|error", "markdown"?:str, "error"?:str}`.
  - `{"type":"error", "code":str, "message":str}` (device lost, OOM downgrade, etc.).
- Multiple clients (compact + expanded can both be open, or reconnect) supported; broadcast to all. On connect, server sends current status + optionally the last N utterances of the active session.

**Lifecycle.** `start` builds ring buffers, WAV writers, segmenters, transcriber (loads model), translator/diarizer (lazily), starts capture + worker threads, creates the session in `store`. `stop` signals `stop_event`, joins threads (with timeout), flushes/closes WAVs, `finalize()`s the session. The pipeline objects are held on an app-state singleton.

**Bridge.** Server stores `self.loop = asyncio.get_running_loop()` at startup; worker threads push WS messages via `loop.call_soon_threadsafe`. See A4.3.

### 5.9 `web/` Г?" front-end UI (served by FastAPI, wrapped in pywebview)

**Responsibility.** Present the compact bar and expanded window; connect to `/ws`; call REST; render live transcript, translation, speaker labels; expose settings and actions.

**Tech.** Plain, dependency-light: a single-page app in vanilla JS + modern CSS (or a tiny framework like Preact/Alpine if bundled locally Г?" **no CDN**, everything served from `web/`). No build step required for v1 (ship static files). Two logical views toggle within one page (compact Г+" expanded) so a single pywebview window resizes rather than opening a second window.

**pywebview host.** Frameless (`frameless=True`), always-on-top (`on_top=True`), small default size (compact Г%^ 460A-150). `resizable=True`. Custom drag region (a top strip with `-webkit-app-region: drag` semantics Г?" for pywebview implement drag via a JS `pywebview.api` call or a draggable header using `window.moveTo`, or set `easy_drag`). Provide window controls (close, minimize) since the frame is hidden. Expand button resizes the window (Г%^ 900A-640) and switches to the expanded layout; collapse returns to compact.

**Compact bar (default).**
- Left: **Start/Stop** button (turns red "Г-? Stop" while recording).
- A **status dot**: grey (idle) / green (recording, healthy) / amber (falling behind / one source only) / red (error). Tooltip shows detail.
- The **2Г?"3 most recent** transcript lines, each: `Speaker: original` and, if translated, an inline muted Vietnamese line beneath.
- A **"Dап<ch"** (translate) on/off toggle switch.
- An **expand** button (ГЫ).
- Minimal chrome; content updates live via WS.

**Expanded window.**
- **Header:** title (editable), recording controls, status dot, search box, expand/collapse, settings gear.
- **Transcript pane:** full scrolling list. Each utterance row: timestamp A· speaker label (click to rename inline) A· original text A· translation (muted, below). Auto-scroll to bottom unless the user has scrolled up (then show a "jump to latest" pill). Source visually distinguished (You vs Them Г?" e.g. subtle left border color).
- **Search box:** filters/highlights utterances by substring across original + translation + speaker.
- **Settings panel** (drawer/modal), fields (bound to `/api/settings`):
  - Whisper model size (select: small / medium / large-v2 / large-v3).
  - Translate on/off (mirrors "Dап<ch").
  - Source-language selection (multi-select chips: ja, en, zh, ko, fr, Г?▌; empty = any).
  - Translation provider (select: NLLB / Gemini).
  - Summarizer provider (select: Claude CLI / Codex CLI / Gemini / Ollama).
  - Diarization on/off; realtime diarization on/off; diarization device (GPU/CPU).
  - HF token field (for tier-2), Gemini key field (masked). Never echo secrets back in full; show `Г?ЫГ?ЫГ?ЫГ?Ы` with a "change" affordance.
  - Legal & Consent link (reopens A3 text).
- **Summarize button:** calls `/api/sessions/{id}/summarize`; shows spinner then renders the returned structured Markdown (in a panel with copy button). If provider unavailable, shows the graceful notice.
- **Re-diarize button:** calls `/api/sessions/{id}/rediarize`; shows progress; on done, transcript relabels live.
- **Sessions list:** open past sessions (read-only view + summarize/re-diarize).
- **Speaker rename:** inline edit on any label Г+' `POST Г?▌/speakers/rename` Г+' live update everywhere.

**Aesthetic.** Clean, modern, minimal Г?" **not "AI slop."** Concretely: one restrained accent color; system font stack (Segoe UI / Inter if bundled locally); generous line-height and spacing; muted secondary text for translations/timestamps; subtle borders not heavy shadows; no gradients-for-gradient's-sake, no emoji-spam, no purple-glow. Light and dark mode via `prefers-color-scheme`. Fast, no layout jank on new utterances (virtualize or cap DOM nodes if the transcript is very long).

### 5.10 `config.py` Г?" settings

**Responsibility.** Define, load, validate, persist all settings as JSON. See full reference in A7.

**Public interface.**
```python
class Settings(BaseModel):     # pydantic
    ...  # all fields in A7 with defaults + validators
    @classmethod
    def load(cls, path: str) -> "Settings"
    def save(self, path: str) -> None
    def update(self, partial: dict) -> "Settings"   # validate + persist
```
- File location: app data dir `settings.json` (see A10). If absent, created with defaults. Unknown keys ignored with a warning; invalid values rejected (422 on the API). Secrets (`hf_token`, `gemini_api_key`) stored in the same JSON on the local machine (single-user desktop); documented as plaintext-at-rest (v1 acceptable; note as a hardening item). Never log secret values.

---

## 6. Data Model

Covered inline in A5.7 (JSONL utterance schema, `meta.json`, session folder layout, `transcript.md` format). Summary of persisted artifacts per session: `meta.json`, `transcript.jsonl` (authoritative append-only), `transcript.md` (rendered), `summary.md` (on demand), `audio_you.wav` / `audio_them.wav` (16 kHz mono PCM16, required for tier-2), plus backups on rewrite. Schema versioning via the `schema` integer on records and meta.

---

## 7. Settings / Config Reference

All keys, types, defaults. Persisted in `settings.json`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `consent_acknowledged` | bool | `false` | User accepted the Legal & Consent modal. |
| `consent_acknowledged_at` | str/null | `null` | ISO timestamp of acknowledgement. |
| `server_port` | int | `8848` | Localhost port (auto-bumps if busy). |
| `sessions_root` | str | `"./sessions"` | Root dir for session folders (under app data). |
| `audio_backend` | enum | `"auto"` | `auto`\|`soundcard`\|`pyaudiowpatch`. |
| `persist_audio` | bool | `true` | Write per-source WAVs (needed for tier-2). |
| `target_sample_rate` | int | `16000` | Pipeline sample rate (do not change in v1). |
| `frame_ms` | int | `20` | VAD frame hop in ms. |
| `vad_engine` | enum | `"silero"` | `silero`\|`webrtcvad`. |
| `vad_device` | enum | `"cpu"` | `cpu`\|`cuda` for Silero VAD. |
| `vad_aggressiveness` | int | `2` | webrtcvad only, 0Г?"3. |
| `pre_roll_ms` | int | `300` | Audio kept before speech onset. |
| `speech_start_ms` | int | `150` | Sustained speech to start an utterance. |
| `silence_end_ms` | int | `600` | Trailing silence to end an utterance (latency knob). |
| `min_speech_ms` | int | `250` | Drop utterances shorter than this. |
| `max_utterance_seconds` | int | `15` | Force-cut long utterances. |
| `whisper_model` | enum | `"large-v3"` | `small`\|`medium`\|`large-v2`\|`large-v3`. |
| `whisper_compute_type` | enum | `"float16"` | `float16`\|`int8_float16`\|`int8`. |
| `latency_mode` | enum | `"quality"` | `quality`(beam 5)\|`fast`(beam 1). |
| `whisper_vad_filter` | bool | `true` | Secondary VAD filter in faster-whisper. |
| `force_language` | str/null | `null` | Force Whisper language (e.g. `"ja"`), else auto. |
| `no_speech_threshold` | float | `0.6` | Hallucination guard. |
| `logprob_drop_threshold` | float | `-1.0` | Hallucination guard. |
| `min_rms` | float | `0.005` | Drop near-silent utterances. |
| `hallucination_denylist` | list[str] | (see A5.3) | Whole-utterance texts to drop. |
| `auto_downgrade_on_backpressure` | bool | `false` | Auto-shrink model if falling behind. |
| `backpressure_warn_seconds` | int | `8` | Sustained backlog Г+' warn. |
| `translate_enabled` | bool | `false` | Live translation on/off ("Dап<ch"). |
| `target_lang` | str | `"vi"` | Translation target. |
| `source_languages` | list[str] | `[]` | Langs to translate; empty = any non-target. |
| `translation_provider` | enum | `"nllb"` | `nllb`\|`gemini`. |
| `nllb_model` | str | `"facebook/nllb-200-distilled-600M"` | HF model id. |
| `translation_device` | enum | `"cuda"` | `cuda`\|`cpu` for NLLB. |
| `gemini_api_key` | str/null | `null` | For Gemini translate/summarize (masked). |
| `diarization_enabled` | bool | `true` | Master diarization toggle. |
| `diarization_realtime` | bool | `true` | Tier-1 online clustering on/off. |
| `diarization_embedder` | enum | `"ecapa"` | `ecapa`\|`resemblyzer`. |
| `diarization_device` | enum | `"cuda"` | `cuda`\|`cpu` for embeddings/pyannote. |
| `sim_threshold` | float | `0.70` | Cosine sim to merge into existing speaker. |
| `min_embed_ms` | int | `800` | Min utterance length to embed/cluster. |
| `max_speakers` | int | `8` | Cap on tier-1 speakers. |
| `hf_token` | str/null | `null` | HuggingFace token for pyannote (tier-2). |
| `pyannote_model` | str | `"pyannote/speaker-diarization-3.1"` | Tier-2 pipeline. |
| `summarizer_provider` | enum | `"claude_cli"` | `claude_cli`\|`codex_cli`\|`gemini`\|`ollama`. |
| `summary_prompt` | str | (see A5.6) | Editable summarization prompt. |
| `summary_use_translation` | bool | `true` | Feed Vietnamese text to summarizer when available. |
| `summary_max_chars` | int | `48000` | Chunking threshold (map-reduce beyond). |
| `summary_timeout_s` | int | `300` | Subprocess/API timeout. |
| `ollama_model` | str | `"llama3.1"` | Ollama model name. |
| `ollama_url` | str | `"http://localhost:11434"` | Ollama endpoint. |
| `fsync_interval_ms` | int | `1000` | Autosave durability throttle. |
| `theme` | enum | `"auto"` | `auto`\|`light`\|`dark`. |
| `app_version` | str | `"1.0"` | Read-only. |

Validators: enums constrained; thresholds range-checked; `source_languages` items must be known ISO codes; changing `whisper_model` mid-session takes effect at next `start`.

---

## 8. Error Handling

Dedicated behaviors (all surface a clear UI notice via WS `error`/`status`; none crash the pipeline):

1. **No loopback device / output missing:** start with mic only, `them.available=false`, status dot amber, note "Recording microphone only Г?" no system audio device." Transcript proceeds for "You".
2. **Mic blocked / missing (Windows privacy or no input device):** start with loopback only, `you.available=false`, note "Recording system audio only Г?" microphone unavailable (check Windows mic privacy)." 
3. **Both sources fail:** `start` returns error; do not enter recording state; UI shows actionable message (check audio devices).
4. **Device change mid-session:** auto-reopen with retries (A5.1); brief gap tolerated; note on lost/reopened.
5. **GPU OOM (load or inference):** empty cache Г+' downgrade model Г+' downgrade compute_type (float16Г+'int8_float16Г+'int8) (A5.3). Emit note describing the effective config. Persist to meta.
6. **Whisper hallucination on silence:** prevented by VAD gating + `min_speech_ms` + `no_speech_prob`/`avg_logprob` thresholds + denylist + `min_rms` (A5.3). Dropped utterances are silently not emitted (optionally logged at debug).
7. **Translation failure / unmapped language:** pass through original, mark `translation_error`/`translation=null`; UI shows a subtle "translation failed" hint. Unmapped language Г+' skip translation, log once.
8. **Summarizer CLI not installed / provider unavailable:** `available()` false Г+' graceful message naming the missing tool and suggesting an alternative provider; no `summary.md` written.
9. **HF token missing for tier-2:** Re-diarize button disabled/greyed with tooltip "Add a free HuggingFace token in Settings to enable accurate re-diarization" + link to model-terms acceptance.
10. **pyannote/model download failure or network offline:** clear error; tier-2 stays available once model cached.
11. **Backpressure (GPU can't keep up):** amber status, "falling behind"; optional auto-downgrade. Data still captured to WAV, so nothing is lost even if live transcript lags.
12. **Crash / power loss:** append-only fsynced JSONL means at most ~1 s of the last utterance line is lost; on relaunch, sessions are readable; `transcript.md` regenerated on demand from JSONL if missing.
13. **Port in use:** auto-bump `server_port`; report chosen port to pywebview.
14. **WebSocket disconnect:** UI auto-reconnects with backoff; on reconnect, server resends current status + recent utterances.

---

## 9. Testing Strategy

### 9.1 Unit tests (`tests/unit/`)
- **Segmenter boundaries:** feed synthetic 16 kHz PCM (sine bursts separated by silence). Assert: correct number of utterances, respect for `min_speech_ms` (short blip dropped), `silence_end_ms` boundary, `max_utterance_seconds` forced cut, pre-roll inclusion. Use webrtcvad path for determinism (silero mockable) and/or a fake VAD returning scripted probabilities.
- **Translator via mocks:** `NllbTranslator` with the model call mocked Г?" assert lang-code mapping (jaГ+'jpn_Jpan, etc.), the gating logic (only translates when enabled Г^ langГ% target Г^ langГ^^source set), and unmapped-language pass-through. `GeminiTranslator.available()` with/without key.
- **Summarizer via mocks:** patch `subprocess.run` / API client. Assert prompt assembly (timestamps, speaker labels, translation-vs-original selection), chunking above `summary_max_chars`, `available()` detection of missing binary (`shutil.which` mocked), graceful error string on non-zero exit / timeout.
- **Store round-trip:** create session Г+' append N `UtteranceRecord`s Г+' read back JSONL Г+' assert equality; rename_speaker updates all matching records + meta + md; finalize sorts by start; partial-trailing-line tolerance; WAV writer produces valid 16 kHz mono PCM16 (read back with soundfile, check frames).
- **Diarizer online-clustering logic:** with a **fake embedder** returning controlled vectors Г?" assert: identical vectors cluster together, distant vectors create new speakers, `sim_threshold` boundary behavior, `min_embed_ms` short-utterance skip, `max_speakers` cap, rename propagation, "You" always for mic.
- **Config:** load/save round-trip, defaults, validator rejection of bad enums/ranges, unknown-key tolerance, secret non-logging.

### 9.2 Integration tests (`tests/integration/`)
- **Pipeline on a known WAV:** replace live capture with a `FileCaptureSource` that streams a bundled short WAV (with real speech + a silent gap) through the *real* segmenter and a *real or small* Whisper model (use `small`, `compute_type=int8`, CPU allowed in CI-lite; mark GPU-only tests to skip without CUDA). Assert: non-empty transcript, Г%Э1 utterance, files written (`transcript.jsonl`, `transcript.md`, `meta.json`), records well-formed. Translation + diarization can be mocked or run with tiny inputs.
- **Server API:** FastAPI `TestClient` Г?" settings get/put validation, sessions list/open, summarize with a mocked provider returns markdown + writes `summary.md`, rename endpoint. WebSocket receives an `utterance` message when the pipeline (driven by the file source) emits one.

### 9.3 Manual smoke test (user step; documented, not automatable here)
- Run one real online meeting. Verify: both "You" and "Them" audio captured; live transcript + translation appear with low latency; speaker labels look sane and are renameable; stop Г+' summarize (Claude CLI) Г+' structured notes; Re-diarize (with HF token) relabels cleanly; session folder contains all artifacts.
- **Explicitly NOT auto-testable here** and called out in the test docs: (a) live WASAPI loopback capture on real hardware, (b) real-time GPU performance / latency under `large-v3`, (c) device-change hot-swap, (d) actual translation/summary quality. These require the user's machine + a real meeting.

### 9.4 Tooling
`pytest`, `pytest-asyncio` (WS/async), `numpy` for synthetic PCM, `soundfile` for WAV assertions. GPU/model-heavy tests marked `@pytest.mark.gpu` and skipped when CUDA/model absent.

---

## 10. Dependencies & Environment

### 10.1 Assumptions
- Windows 11 (WASAPI loopback is **Windows-specific** Г?" the app does not run on macOS/Linux in v1).
- Python 3.12.
- NVIDIA GPU with a working CUDA-enabled PyTorch already installed; faster-whisper already installed and functional on GPU.
- Internet access on **first run only** to download models (thereafter cached under the HF/torch cache).

### 10.2 Python packages (`requirements.txt`)
```
# --- STT (already installed, pinned for clarity) ---
faster-whisper>=1.0.0

# --- audio capture (Windows WASAPI) ---
soundcard>=0.4.3          # preferred loopback capture
PyAudioWPatch>=0.2.12     # fallback WASAPI loopback

# --- resampling / audio io ---
numpy>=1.26
scipy>=1.11
soxr>=0.3.7               # high-quality streaming resample (optional but recommended)
soundfile>=0.12           # WAV read/write

# --- VAD ---
silero-vad>=5.1           # preferred (torch)
webrtcvad>=2.0.10         # fallback

# --- translation (NLLB) ---
transformers>=4.44
sentencepiece>=0.2
ctranslate2>=4.3          # optional faster NLLB backend
torch                     # provided by existing CUDA install (do not reinstall blindly)

# --- speaker embeddings (tier-1) ---
speechbrain>=1.0          # ECAPA-TDNN
resemblyzer>=0.1.4        # fallback embedder

# --- diarization (tier-2) ---
pyannote.audio>=3.1

# --- backend / server ---
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic>=2.7
websockets>=12

# --- desktop shell ---
pywebview>=5.1

# --- misc ---
python-dateutil>=2.9
```
Notes:
- **Do not** let `requirements.txt` clobber the user's working CUDA `torch`/`faster-whisper`. In practice, install the rest with `--no-deps` where needed, or document that torch is pre-provided. Provide a `requirements.txt` plus a `requirements-notes.md` warning about torch.
- Optional providers (`google-generativeai` for Gemini) are **not** required; documented as extras installed only if the user enables that provider. Ollama and Claude/Codex CLIs are external executables, not pip deps.

### 10.3 Model downloads (first run, sizes approximate)
- Whisper `large-v3` (CTranslate2): ~3 GB VRAM at fp16; model files ~1.5 GB. `medium` ~0.8 GB files.
- NLLB-200 distilled-600M: ~2.5 GB files, ~1.5Г?"2 GB VRAM (or CPU).
- ECAPA-TDNN (SpeechBrain): ~80 MB, ~1 GB VRAM on GPU.
- pyannote 3.1 pipeline: ~30Г?"100 MB, gated (needs HF token + accept terms).
Document total first-run download (~5Г?"7 GB) and that subsequent runs are offline-capable.

### 10.4 VRAM budget guidance
Rough concurrent GPU residency with defaults: Whisper large-v3 (~3 GB) + NLLB (~1.5 GB) + ECAPA (~1 GB) Г%^ 5.5 GB. On <8 GB cards, recommend either `whisper_model=medium`, `translation_device=cpu`, or `diarization_device=cpu`. Provide these as documented Settings knobs (A7). A single `torch` device lock serializes GPU submissions to avoid fragmentation spikes.

---

## 11. Repo Layout & Entry Point

### 11.1 Layout
```
ai-record/
  README.md
  requirements.txt
  requirements-notes.md          # torch/CUDA caveat, model sizes
  docs/
    SPEC.md                      # this document
  main.py                        # thin launcher -> ai_record.__main__
  ai_record/
    __init__.py
    __main__.py                  # `python -m ai_record` entry
    config.py                    # Settings (pydantic) + load/save
    capture.py                   # WASAPI dual-stream capture + resample
    ring_buffer.py               # RingBuffer helper
    segmenter.py                 # per-source VAD segmentation
    transcriber.py               # faster-whisper wrapper
    translator.py                # Translator interface + NLLB + Gemini stub
    diarizer.py                  # RealtimeDiarizer (T1) + OfflineDiarizer (T2)
    summarizer.py                # Summarizer interface + Claude/Codex/Gemini/Ollama
    store.py                     # SessionStore + WavWriter
    pipeline.py                  # wires capture->segment->transcribe->translate->diarize->store/broadcast
    server.py                    # FastAPI app, REST + WS, lifecycle
    app.py                       # starts uvicorn thread + opens pywebview window
    lang_maps.py                 # Whisper<->NLLB code maps, denylists
  web/
    index.html
    app.js
    styles.css
    assets/                      # bundled fonts/icons (no CDN)
  sessions/                      # created at runtime (gitignored)
  tests/
    unit/
    integration/
    fixtures/                    # small known WAVs
```
App data dir: on Windows, sessions + settings default under the repo (`./sessions`, `./settings.json`) for v1 simplicity; document that a future version may move to `%LOCALAPPDATA%\ai-record\`.

### 11.2 Entry point (`python -m ai_record` / `main.py`)
`app.py`:
1. Load `Settings`.
2. Start Uvicorn (FastAPI `server.app`) in a background thread bound to `127.0.0.1:<port>` (with port-bump). Wait until health-ready.
3. Open a pywebview window (frameless, on-top, compact size) pointing at `http://127.0.0.1:<port>`.
4. `webview.start()` blocks on the GUI loop; on window close, signal the server to stop capture (if active) and shut down Uvicorn, then exit.
`main.py` is a one-liner: `from ai_record.app import main; main()`. `__main__.py` calls the same.

---

## 12. Milestones / Phases

- **M0 Г?" Skeleton:** repo layout, `config.py`, FastAPI server with `/health` + static UI shell, pywebview window opens. (No audio.)
- **M1 Г?" Capture:** `capture.py` dual WASAPI streams + resample + ring buffers + WAV persistence; status/device-missing handling; a debug endpoint showing RMS levels. Manual verify audio flows.
- **M2 Г?" Segment + Transcribe:** `segmenter.py` + `transcriber.py`; live utterances pushed over WS; compact bar shows transcript. Hallucination guards. This is the first end-to-end "it transcribes a meeting" milestone.
- **M3 Г?" Storage + Sessions:** `store.py` autosave JSONL/MD/WAV, sessions list/open, `transcript.md` finalize. Crash-safety verified.
- **M4 Г?" Translation:** `translator.py` NLLB + gating + "Dап<ch" toggle + inline translation in UI + Gemini stub.
- **M5 Г?" Diarization T1:** real-time embedding + online clustering + renameable labels.
- **M6 Г?" Summarize:** `summarizer.py` Claude CLI default + provider selection + summary panel + graceful missing-CLI.
- **M7 Г?" Diarization T2:** pyannote re-diarize button + HF token flow + transcript relabel + backups.
- **M8 Г?" Expanded UI + polish:** full transcript view, search, settings panel, theme, design pass (anti-slop), error surfacing.
- **M9 Г?" Tests + docs:** unit + integration suites, README, first-run consent modal, manual smoke test checklist.
- **Future (out of v1):** `.exe` packaging (PyInstaller), overlapping-speech separation, app-data-dir migration, per-tenant model presets.

---

## 13. Risks & Open Questions

- **Loopback library reliability.** `soundcard` loopback on Windows can be finicky across driver/format combos; `pyaudiowpatch` is the fallback but has its own device-index quirks. Risk: some machines only work on one backend. Mitigation: `audio_backend` override + robust device probing. Needs real-hardware validation (not auto-testable).
- **Real-time performance under large-v3.** On modest GPUs, `large-v3` per-utterance latency may exceed comfort; backpressure/auto-downgrade helps but the default may need to be `medium` for some users. Open: pick default per detected VRAM at first run?
- **Concurrent GPU residency.** Whisper + NLLB + ECAPA + (occasionally) pyannote can exceed 8 GB VRAM. Mitigated by CPU-offload knobs, but the *default* config assumes ~8Г?"12 GB. Open: dynamic device placement based on detected VRAM.
- **NLLB JAГ+'VI quality.** Explicitly moderate; Gemini improves it but breaks the "fully local" promise. This is a documented tradeoff, not a bug.
- **Diarization accuracy on short utterances / overlapping speech.** Tier-1 online clustering will mislabel short/overlapping segments; tier-2 pyannote is the corrective, but overlapping speech remains a known v1 limitation (non-goal).
- **Timestamp alignment for tier-2.** Utterance timestamps must map accurately onto `audio_them.wav` sample positions for pyannote overlap-majority relabelling. Any drift between capture start and the session clock, or resampler latency, could misalign. Mitigation: derive timestamps from cumulative sample counts of the persisted stream, not wall-clock.
- **Secrets at rest.** `hf_token` / `gemini_api_key` stored plaintext in `settings.json` (single-user desktop). Acceptable v1; hardening (OS keyring) noted as future.
- **CLI summarizer variability.** `claude -p` / `codex exec` output format and flags can change; long transcripts via arg vs stdin. Mitigation: prefer stdin/temp-file, timeout, and treat output as opaque markdown.
- **Legal exposure.** The core function (recording without a platform indicator) is legally sensitive; the consent gate + honest framing (A3) are mandatory, but ultimately the user bears compliance responsibility.
- **pywebview frameless drag/controls on Windows.** Custom window chrome (drag region, always-on-top toggling) can be fiddly with the EdgeChromium backend. Needs verification; fallback to a thin native title bar if frameless drag proves unstable.

---

*End of specification.*

codex
The first read confirms the spec is long and the tool truncated the middle, so I’m narrowing to headings and the high-risk sections instead of re-reading the whole file blindly.
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command 'rg -n "''^(##|###) " .'"\\docs\\SPEC.md" in C:\Users\DucNguyen\Code\ai-record
 succeeded in 357ms:
9:## 1. Overview
27:### 1.1 Why loopback capture
33:## 2. Goals & Non-Goals
35:### 2.1 Goals (v1, in scope)
50:### 2.2 Non-Goals (v1, explicitly out of scope)
62:## 3. Legal & Consent (READ FIRST)
81:## 4. Architecture
83:### 4.1 High-level model
92:### 4.2 ASCII data-flow diagram
155:### 4.3 Concurrency & backpressure model
165:### 4.4 Timebase
171:## 5. Components
175:### 5.1 `capture.py` — audio capture
217:### 5.2 `segmenter.py` — VAD segmentation
251:### 5.3 `transcriber.py` — speech-to-text
292:### 5.4 `translator.py` — live translation
341:### 5.5 `diarizer.py` — speaker diarization (two tiers)
379:### 5.6 `summarizer.py` — AI summarization
425:### 5.7 `store.py` — session storage
529:### 5.8 `server.py` — FastAPI backend
567:### 5.9 `web/` — front-end UI (served by FastAPI, wrapped in pywebview)
603:### 5.10 `config.py` — settings
620:## 6. Data Model
626:## 7. Settings / Config Reference
690:## 8. Error Handling
711:## 9. Testing Strategy
713:### 9.1 Unit tests (`tests/unit/`)
721:### 9.2 Integration tests (`tests/integration/`)
725:### 9.3 Manual smoke test (user step; documented, not automatable here)
729:### 9.4 Tooling
734:## 10. Dependencies & Environment
736:### 10.1 Assumptions
742:### 10.2 Python packages (`requirements.txt`)
790:### 10.3 Model downloads (first run, sizes approximate)
797:### 10.4 VRAM budget guidance
802:## 11. Repo Layout & Entry Point
804:### 11.1 Layout
842:### 11.2 Entry point (`python -m ai_record` / `main.py`)
852:## 12. Milestones / Phases
868:## 13. Risks & Open Questions

codex
I’ve got the section map. Next I’m reading the component sections that drive the review: capture, segmentation, STT, translation, diarization, summarization, storage, and server.
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=175; $i -le 424; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }' in C:\Users\DucNguyen\Code\ai-record
 declined in 0ms:
`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=175; $i -le 424; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
2026-07-05T02:48:59.605513Z ERROR codex_core::tools::router: error=`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=175; $i -le 424; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=425; $i -le 619; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }' in C:\Users\DucNguyen\Code\ai-record
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=620; $i -le 710; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }' in C:\Users\DucNguyen\Code\ai-record
 declined in 0ms:
`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=425; $i -le 619; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
 declined in 0ms:
`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=620; $i -le 710; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
2026-07-05T02:48:59.674312Z ERROR codex_core::tools::router: error=`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=425; $i -le 619; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
2026-07-05T02:48:59.675142Z ERROR codex_core::tools::router: error=`"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '$lines = Get-Content -LiteralPath .'"\\docs\\SPEC.md; for ("'$i=620; $i -le 710; $i++) { if ($i -le $lines.Length) { '"'{0}: {1}' -f "'$i, $lines[$i-1] } }'` rejected: blocked by policy
codex
That line-numbering command was blocked by the local policy, so I’m switching to simpler read-only `Get-Content | Select-Object` slices. The citations will use the spec’s section numbers rather than line numbers.
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 424 | Select-Object -Skip 174" in C:\Users\DucNguyen\Code\ai-record
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 619 | Select-Object -Skip 424" in C:\Users\DucNguyen\Code\ai-record
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 710 | Select-Object -Skip 619" in C:\Users\DucNguyen\Code\ai-record
 succeeded in 267ms:
### 5.7 `store.py` Г?" session storage

**Responsibility.** Durable, crash-safe persistence of each session; autosave every finalized utterance; expose read APIs for listing/opening sessions; support tier-2 rewrite and speaker renames.

**Public interface.**
```python
class WavWriter:
    def __init__(self, path: str, samplerate=16000, channels=1): ...
    def write(self, pcm: np.ndarray) -> None
    def close(self) -> None

class SessionStore:
    def __init__(self, sessions_root: str): ...
    def create(self, title: str) -> Session          # makes folder + opens files
    def append_utterance(self, rec: UtteranceRecord) -> None   # autosave (jsonl + md)
    def rename_speaker(self, session_id, old, new) -> int      # updates records + md
    def write_summary(self, session_id, markdown: str) -> None
    def rewrite_after_rediarize(self, session_id, new_labels) -> None
    def list_sessions(self) -> list[SessionMeta]
    def load_session(self, session_id) -> SessionData
    def finalize(self, session_id) -> None            # sort md, write meta, close wavs
```

**Session folder layout.**
```
sessions/
  20260705-142530-standup/
    meta.json            # session metadata (see below)
    transcript.jsonl     # one JSON object per utterance (append-only, authoritative)
    transcript.md        # human-readable, rendered/sorted by start time
    summary.md           # created on demand (may be absent)
    audio_you.wav        # 16 kHz mono PCM16 mic capture (may be absent if mic missing)
    audio_them.wav       # 16 kHz mono PCM16 loopback capture (may be absent)
    transcript.jsonl.pre-rediarize   # backup, only if tier-2 ran
    summary.md.bak       # backup, only if re-summarized
```
- `session_id` = folder name = `<YYYYMMDD-HHMMSS>-<slug(title)>`. Title default = "meeting"; slug = lowercased, non-alnumГ+'`-`, trimmed, max 40 chars.
- `sessions_root` default `./sessions/` under the app data dir (see A10).

**Autosave.** `append_utterance` is called for every finalized utterance and must be cheap + durable:
- Append one line to `transcript.jsonl` (open in `"a"`, `encoding="utf-8"`), then `f.flush()` and `os.fsync(fileno)` on a throttle (fsync at most every `fsync_interval_ms`, default 1000 ms, to bound overhead while bounding data loss to ~1 s). WAV writers similarly flush periodically.
- Append a rendered line to `transcript.md` incrementally (unsorted, in completion order) so a crash still leaves readable text; the sorted, clean `transcript.md` is regenerated on `finalize()`.
- Crash safety: because JSONL is append-only + flushed, a crash loses at most the last unflushed line. On next open, a partial trailing line (no newline) is ignored/truncated.

**JSONL utterance record schema.** One object per line:
```json
{
  "id": "u_000123",                // stable per-utterance id, zero-padded seq
  "session_id": "20260705-142530-standup",
  "seq": 123,                      // completion order
  "source": "them",                // "you" | "them"
  "speaker": "Speaker 2",          // display label (renameable / re-diarized)
  "start": 842.31,                 // seconds since session_t0
  "end": 846.07,
  "duration": 3.76,
  "text": "ДмsК-°a,'Ж<a,?a?_a?-a,╪a?+a?,",     // original transcription
  "lang": "ja",                    // detected language (ISO-639-1)
  "lang_prob": 0.98,
  "translation": "HAЬy bаз_t О`азu cuапTc hап?p.",  // Vietnamese, or null if not translated
  "translation_provider": "nllb",  // "nllb" | "gemini" | null
  "translation_error": false,
  "no_speech_prob": 0.04,
  "avg_logprob": -0.31,
  "diarization_source": "realtime",// "realtime" | "offline" | "manual"
  "created_at": "2026-07-05T14:39:12.031+07:00",
  "schema": 1
}
```
- Missing/optional fields (`translation`, etc.) are `null`. `schema` version enables future migrations.

**`meta.json` schema.**
```json
{
  "session_id": "20260705-142530-standup",
  "title": "standup",
  "created_at": "2026-07-05T14:25:30+07:00",
  "ended_at": "2026-07-05T15:02:11+07:00",
  "duration_sec": 2201,
  "sources": {"you": true, "them": true},
  "whisper_model": "large-v3",
  "compute_type": "float16",
  "translate_enabled": true,
  "target_lang": "vi",
  "source_languages": ["ja", "en"],
  "translation_provider": "nllb",
  "diarization_enabled": true,
  "speakers": {"Speaker 1": "Tanaka-san", "Speaker 2": "Suzuki"},  // rename map
  "summary_provider": "claude_cli",
  "summarized_at": null,
  "rediarized_at": null,
  "app_version": "1.0",
  "schema": 1
}
```

**`transcript.md` rendering.** Sorted by `start`. Format per utterance:
```
**[14:39:12] Speaker 2 (ja):** ДмsК-°a,'Ж<a,?a?_a?-a,╪a?+a?,
> HAЬy bаз_t О`азu cuапTc hап?p.
```
(Translation line only when present. Timestamp shown as wall-clock derived from `created_at`/`start`.)

**Renames.** `rename_speaker` updates all matching records' `speaker` in `transcript.jsonl` (rewrite the file atomically: write to temp, `os.replace`), updates `meta.json.speakers`, and re-renders `transcript.md`. Returns count updated.

### 5.8 `server.py` Г?" FastAPI backend

**Responsibility.** Host the HTTP API + WebSocket, own the pipeline lifecycle, serve the web UI, bridge worker threads to the event loop.

**Dependencies.** `fastapi`, `uvicorn`, `pydantic`, the components above, `config.Settings`.

**Server config.** Bind `127.0.0.1` on a fixed default port `8848` (configurable `server_port`); if occupied, try next 10 ports and report the chosen one to pywebview. CORS locked to localhost. No auth (localhost-only, single user).

**REST endpoints.**
| Method | Path | Body / Params | Returns |
|-------|------|---------------|---------|
| `POST` | `/api/capture/start` | `{title?: str}` | `{session_id, sources: {you, them}}` Г?" starts pipeline; errors if both sources fail |
| `POST` | `/api/capture/stop` | Г?" | `{session_id, finalized: true}` Г?" stops capture, finalizes session |
| `GET` | `/api/capture/status` | Г?" | `{recording, session_id, sources, dropped_frames, backpressure, model}` |
| `GET` | `/api/sessions` | Г?" | `[SessionMeta]` (list, newest first) |
| `GET` | `/api/sessions/{id}` | Г?" | full session: meta + utterances (+ summary if present) |
| `POST` | `/api/sessions/{id}/summarize` | `{provider?}` | `{markdown}` or `{error}` (runs summarizer) |
| `POST` | `/api/sessions/{id}/rediarize` | Г?" | `{status:"started"}`; completion via WS |
| `POST` | `/api/sessions/{id}/speakers/rename` | `{old, new}` | `{updated: n}` |
| `GET` | `/api/settings` | Г?" | full settings object |
| `PUT` | `/api/settings` | partial settings | updated settings (validated) |
| `GET` | `/api/health` | Г?" | `{ok, gpu, cuda, models_loaded}` |
| `GET` | `/` and `/static/*` | Г?" | serves `web/` UI |

**WebSocket** `GET /ws`:
- Server Г+' client message types:
  - `{"type":"utterance", "record": <UtteranceRecord>}` Г?" a new finalized, translated, diarized utterance.
  - `{"type":"status", "recording":bool, "sources":{Г?▌}, "backpressure":bool, "model":str, "note":str}`.
  - `{"type":"rename", "old":str, "new":str}`.
  - `{"type":"rediarize", "state":"started|progress|done|error", "detail":Г?▌}`.
  - `{"type":"summary", "state":"started|done|error", "markdown"?:str, "error"?:str}`.
  - `{"type":"error", "code":str, "message":str}` (device lost, OOM downgrade, etc.).
- Multiple clients (compact + expanded can both be open, or reconnect) supported; broadcast to all. On connect, server sends current status + optionally the last N utterances of the active session.

**Lifecycle.** `start` builds ring buffers, WAV writers, segmenters, transcriber (loads model), translator/diarizer (lazily), starts capture + worker threads, creates the session in `store`. `stop` signals `stop_event`, joins threads (with timeout), flushes/closes WAVs, `finalize()`s the session. The pipeline objects are held on an app-state singleton.

**Bridge.** Server stores `self.loop = asyncio.get_running_loop()` at startup; worker threads push WS messages via `loop.call_soon_threadsafe`. See A4.3.

### 5.9 `web/` Г?" front-end UI (served by FastAPI, wrapped in pywebview)

**Responsibility.** Present the compact bar and expanded window; connect to `/ws`; call REST; render live transcript, translation, speaker labels; expose settings and actions.

**Tech.** Plain, dependency-light: a single-page app in vanilla JS + modern CSS (or a tiny framework like Preact/Alpine if bundled locally Г?" **no CDN**, everything served from `web/`). No build step required for v1 (ship static files). Two logical views toggle within one page (compact Г+" expanded) so a single pywebview window resizes rather than opening a second window.

**pywebview host.** Frameless (`frameless=True`), always-on-top (`on_top=True`), small default size (compact Г%^ 460A-150). `resizable=True`. Custom drag region (a top strip with `-webkit-app-region: drag` semantics Г?" for pywebview implement drag via a JS `pywebview.api` call or a draggable header using `window.moveTo`, or set `easy_drag`). Provide window controls (close, minimize) since the frame is hidden. Expand button resizes the window (Г%^ 900A-640) and switches to the expanded layout; collapse returns to compact.

**Compact bar (default).**
- Left: **Start/Stop** button (turns red "Г-? Stop" while recording).
- A **status dot**: grey (idle) / green (recording, healthy) / amber (falling behind / one source only) / red (error). Tooltip shows detail.
- The **2Г?"3 most recent** transcript lines, each: `Speaker: original` and, if translated, an inline muted Vietnamese line beneath.
- A **"Dап<ch"** (translate) on/off toggle switch.
- An **expand** button (ГЫ).
- Minimal chrome; content updates live via WS.

**Expanded window.**
- **Header:** title (editable), recording controls, status dot, search box, expand/collapse, settings gear.
- **Transcript pane:** full scrolling list. Each utterance row: timestamp A· speaker label (click to rename inline) A· original text A· translation (muted, below). Auto-scroll to bottom unless the user has scrolled up (then show a "jump to latest" pill). Source visually distinguished (You vs Them Г?" e.g. subtle left border color).
- **Search box:** filters/highlights utterances by substring across original + translation + speaker.
- **Settings panel** (drawer/modal), fields (bound to `/api/settings`):
  - Whisper model size (select: small / medium / large-v2 / large-v3).
  - Translate on/off (mirrors "Dап<ch").
  - Source-language selection (multi-select chips: ja, en, zh, ko, fr, Г?▌; empty = any).
  - Translation provider (select: NLLB / Gemini).
  - Summarizer provider (select: Claude CLI / Codex CLI / Gemini / Ollama).
  - Diarization on/off; realtime diarization on/off; diarization device (GPU/CPU).
  - HF token field (for tier-2), Gemini key field (masked). Never echo secrets back in full; show `Г?ЫГ?ЫГ?ЫГ?Ы` with a "change" affordance.
  - Legal & Consent link (reopens A3 text).
- **Summarize button:** calls `/api/sessions/{id}/summarize`; shows spinner then renders the returned structured Markdown (in a panel with copy button). If provider unavailable, shows the graceful notice.
- **Re-diarize button:** calls `/api/sessions/{id}/rediarize`; shows progress; on done, transcript relabels live.
- **Sessions list:** open past sessions (read-only view + summarize/re-diarize).
- **Speaker rename:** inline edit on any label Г+' `POST Г?▌/speakers/rename` Г+' live update everywhere.

**Aesthetic.** Clean, modern, minimal Г?" **not "AI slop."** Concretely: one restrained accent color; system font stack (Segoe UI / Inter if bundled locally); generous line-height and spacing; muted secondary text for translations/timestamps; subtle borders not heavy shadows; no gradients-for-gradient's-sake, no emoji-spam, no purple-glow. Light and dark mode via `prefers-color-scheme`. Fast, no layout jank on new utterances (virtualize or cap DOM nodes if the transcript is very long).

### 5.10 `config.py` Г?" settings

**Responsibility.** Define, load, validate, persist all settings as JSON. See full reference in A7.

**Public interface.**
```python
class Settings(BaseModel):     # pydantic
    ...  # all fields in A7 with defaults + validators
    @classmethod
    def load(cls, path: str) -> "Settings"
    def save(self, path: str) -> None
    def update(self, partial: dict) -> "Settings"   # validate + persist
```
- File location: app data dir `settings.json` (see A10). If absent, created with defaults. Unknown keys ignored with a warning; invalid values rejected (422 on the API). Secrets (`hf_token`, `gemini_api_key`) stored in the same JSON on the local machine (single-user desktop); documented as plaintext-at-rest (v1 acceptable; note as a hardening item). Never log secret values.

---


 succeeded in 267ms:
### 5.1 `capture.py` ƒ?" audio capture

**Responsibility.** Open and run two simultaneous WASAPI streams (loopback of default output = "Them"; default microphone = "You"), convert both to 16 kHz mono float32, push frames into per-source ring buffers, and also tee raw audio to the per-source WAV writers in `store.py`. Handle device changes and missing devices.

**Public interface.**
```python
class AudioFrame:  # lightweight
    source: str          # "you" | "them"
    pcm: np.ndarray      # float32, mono, 16000 Hz, shape (N,)
    n_samples: int
    t_start: float       # seconds since session_t0

class CaptureSource:
    source: str                     # "you" | "them"
    available: bool
    device_name: str | None

class CaptureManager:
    def __init__(self, ring_you: RingBuffer, ring_them: RingBuffer,
                 wav_you: WavWriter | None, wav_them: WavWriter | None,
                 settings: Settings, on_status): ...
    def start(self) -> list[CaptureSource]:   # returns which sources came up
    def stop(self) -> None
    def sources_status(self) -> list[CaptureSource]
    # callback on_status(source, event) for device add/remove/error
```

**Dependencies.** `soundcard` (preferred) or `pyaudiowpatch` (fallback); `numpy`; `scipy.signal` or `soxr`/`resampy` for resampling; `store.WavWriter`.

**Key details.**
- **Library selection.** Try `soundcard` first: it exposes loopback recorders on Windows via `soundcard.default_speaker()`'s loopback and `soundcard.default_microphone()`. If import/init fails, fall back to `pyaudiowpatch` (a PyAudio fork exposing WASAPI loopback). Selection is automatic but overridable by setting `audio_backend: "auto"|"soundcard"|"pyaudiowpatch"`.
  - *soundcard path:* obtain loopback microphone for the default speaker via `soundcard.get_microphone(id=default_speaker.name, include_loopback=True)`; record with `.recorder(samplerate=native, channels=ƒ?İ, blocksize=ƒ?İ)`.
  - *pyaudiowpatch path:* use `get_default_wasapi_loopback()` to find the loopback device index; open an input stream on it. Open the mic as a normal WASAPI input stream.
- **Two independent streams.** Each runs in its own thread with its own recorder context manager. The loopback stream's native format is whatever the output mixer runs at (commonly 48 kHz stereo). The mic may be 44.1/48 kHz mono/stereo.
- **Downmix to mono.** Average channels (`pcm.mean(axis=1)`) if multi-channel.
- **Resample to 16 kHz.** Use a high-quality polyphase resampler. Preferred: `soxr.resample(x, in_rate, 16000)` (fast, good quality). Fallback: `scipy.signal.resample_poly` with computed up/down from `gcd(in_rate, 16000)`. Resampling is stateful across blocks: maintain per-stream filter state or use overlap; with `soxr` use a streaming `soxr.ResampleStream` to avoid block-edge artifacts.
- **Frame size.** Emit frames of a fixed hop (e.g. 20 ms = 320 samples @16 kHz) to feed VAD cleanly; internally read larger blocks from the device (e.g. 100ƒ?"200 ms) and re-chunk.
- **Raw persistence.** As frames are produced (post-resample), also append them to `audio_you.wav` / `audio_them.wav` via `store.WavWriter` (16 kHz, mono, 16-bit PCM). This is required for tier-2 re-diarization (A5.5) and is always on while capturing (unless `persist_audio: false`, which disables tier-2).
- **Device-change handling.** WASAPI default-device changes (user switches headset, plugs in HDMI) invalidate a stream. Detect via: (a) recorder raising/returning an error, or (b) a periodic (every 2 s) check of the current default device id vs the one the stream was opened on. On change: stop the affected stream, attempt to reopen on the new default device (up to `device_reopen_retries`, default 5, with 500 ms backoff), emit `on_status(source, "reopened"|"lost")`. Do not touch the other stream. During a gap, VAD simply sees silence; the session continues.
- **Missing-device handling.** If loopback cannot be opened (no output device, or exclusive-mode conflict), start with only the mic and mark `them.available=false`. If the mic is missing or blocked (Windows privacy setting), start with only loopback and mark `you.available=false`. If **both** fail, `start()` returns an empty list; the server reports an error and does not enter the recording state. At least one source is required.
- **No exclusive mode.** Always open shared-mode WASAPI so we don't seize the device from the meeting app.

### 5.2 `segmenter.py` ƒ?" VAD segmentation

**Responsibility.** Convert a continuous 16 kHz mono stream (via its ring buffer) into discrete utterance chunks bounded by natural pauses, dropping silence, keeping latency low. One instance per source.

**Public interface.**
```python
class Utterance:
    source: str          # "you" | "them"
    pcm: np.ndarray      # float32 16k mono, the utterance audio
    start: float         # seconds since session_t0
    end: float

class Segmenter:
    def __init__(self, source: str, settings: Settings): ...
    def run(self, ring: RingBuffer, out_queue: queue.Queue, stop_event): ...
    # pulls frames, emits Utterance to out_queue
```

**Dependencies.** `silero-vad` (preferred, torch model) or `webrtcvad` (fallback, pure C, no GPU); `numpy`.

**Key algorithm (streaming VAD state machine).**
- VAD is evaluated on fixed frames (Silero works on 30 ms / 512-sample windows @16 kHz; webrtcvad on 10/20/30 ms). Produce a per-frame speech probability (Silero) or boolean (webrtcvad, aggressiveness `vad_aggressiveness` 0ƒ?"3, default 2).
- State machine per source:
  - **IDLE** ƒ+' accumulate a short rolling pre-roll buffer (`pre_roll_ms`, default 300 ms) so we don't clip word onsets.
  - Transition to **SPEECH** when `speech` sustained for `speech_start_ms` (default 150 ms). Prepend pre-roll to the utterance.
  - In **SPEECH**, append frames. Track trailing silence.
  - End the utterance (**ƒ+' IDLE, emit**) when trailing silence exceeds `silence_end_ms` (default 600 ms) **or** the utterance reaches `max_utterance_seconds` (default 15 s ƒ?" force a cut mid-speech to bound latency; the next chunk continues).
  - Discard utterances shorter than `min_speech_ms` (default 250 ms) as noise.
- **Max-length cut:** when forced, cut at the most recent low-energy frame within the last 500 ms if possible (avoid slicing mid-word); otherwise cut hard. Mark `forced_cut=true` internally (not persisted) so the transcriber can optionally overlap-pad.
- **Silero specifics.** Use the packaged VAD (`silero_vad` pip, or torch.hub). Keep the model on CPU (tiny) to leave GPU headroom for Whisper, unless `vad_device: "cuda"`. Reset internal RNN state between utterances.
- **webrtcvad fallback.** No probabilities ƒ?" use a hangover counter (N consecutive speech/nonspeech frames) to emulate start/stop hysteresis.
- **Latency budget.** End-of-utterance latency ƒ%^ `silence_end_ms` (0.6 s) + transcription time. This is the primary tunable for perceived responsiveness.
- **Two independent instances** ("you", "them") run concurrently and never share state.

### 5.3 `transcriber.py` ƒ?" speech-to-text

**Responsibility.** Transcribe each finalized `Utterance` to text using faster-whisper on the GPU; detect language; guard against hallucinations on near-silent input.

**Public interface.**
```python
class Transcript:
    source: str
    start: float
    end: float
    text: str
    lang: str            # ISO-639-1, e.g. "en", "ja", "vi", "zh", "ko"
    lang_prob: float
    avg_logprob: float
    no_speech_prob: float

class Transcriber:
    def __init__(self, settings: Settings): ...
    def load(self) -> None            # loads model, may fall back on OOM
    def transcribe(self, utt: Utterance) -> Transcript | None   # None if dropped
    def current_model(self) -> str
```

**Dependencies.** `faster-whisper` (CTranslate2 backend), CUDA torch runtime present; `numpy`.

**Key details.**
- **Model.** `WhisperModel(model_size, device="cuda", compute_type="float16")`. `model_size` from settings, default `large-v3`; also allow `medium`, `small`, `large-v2`. Model loads once at capture start (or lazily on first utterance) and is reused.
- **Per-utterance transcription.** Call `model.transcribe(pcm, language=None or fixed, vad_filter=True, beam_size=ƒ?İ, temperature=[0.0, 0.2, 0.4], condition_on_previous_text=False)`.
  - `condition_on_previous_text=False` ƒ?" critical: prevents cross-utterance hallucination drift and keeps utterances independent.
  - `vad_filter=True` ƒ?" built-in Silero VAD as a **secondary** guard (segmenter already gated, but this trims residual silence inside the chunk). Configurable `whisper_vad_filter` (default true).
  - `language`: if `force_language` is set (e.g. user knows the meeting is Japanese), pass it; otherwise `None` for auto-detect. Auto-detect returns `info.language`, `info.language_probability`.
  - `beam_size`: default 5 (quality) but `beam_size=1` when in low-latency mode (`latency_mode: "fast"`).
- **Concatenate segments.** faster-whisper returns segment iterator; join `.text`, take earliest start / latest end, average `avg_logprob`, take max `no_speech_prob`.
- **Hallucination guards** (Whisper invents text like "Thank you." / "Thanks for watching" on silence/noise). Drop the transcript (return `None`) when ANY:
  - `no_speech_prob > no_speech_threshold` (default 0.6) AND `avg_logprob < logprob_drop_threshold` (default ƒ^'1.0).
  - Text (after strip) matches a configurable **hallucination denylist** (case-insensitive, whole-utterance): `["thank you", "thanks for watching", "please subscribe", "a?", "a?"Šİ-Š?'a?,a,Sa?Oa?"a?+a?"a?-a?,a?_a?-a?Y", "†--†1", ...]` ƒ?" provide a default list in config, user-extendable.
  - Text is empty or only punctuation/whitespace.
  - Utterance audio RMS below `min_rms` (belt-and-suspenders vs VAD false-positives).
- **GPU OOM fallback.** Wrap load + transcribe in try/except for `RuntimeError`/CUDA OOM. On OOM: (1) `torch.cuda.empty_cache()`, (2) reload at the next smaller model (`large-v3`ƒ+'`medium`ƒ+'`small`), (3) if still failing, switch `compute_type` to `int8_float16` then `int8`. Emit a status event describing the downgrade. Persist the effective model in session metadata. Never crash the pipeline on OOM.
- **Threading.** Single transcription worker (the GPU is serialized). See A4.3.

### 5.4 `translator.py` ƒ?" live translation

**Responsibility.** Translate foreign-language utterance text into Vietnamese, live, when enabled. Pluggable providers behind one interface.

**Public interface.**
```python
class Translator(Protocol):
    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str: ...
    def available(self) -> bool: ...

class NllbTranslator(Translator):   # default
    def __init__(self, settings: Settings): ...

class GeminiTranslator(Translator): # stub, off by default
    def __init__(self, settings: Settings): ...
```

**Dependencies.** Default: `transformers` (+ `sentencepiece`) or `ctranslate2` for NLLB-200 distilled-600M; `torch`. Gemini stub: `google-generativeai` (optional, only if user enables + provides key).

**When to translate.** The pipeline calls `translate()` for an utterance **only if ALL**:
1. `translate_enabled` is true (Settings / "D ¯<ch" toggle), AND
2. detected `lang != target_lang` (`target_lang` default `"vi"`), AND
3. `lang` ƒ^^ `source_languages` (user-selected set of source languages to translate; empty set = "translate any non-target language").

Otherwise the utterance is passed through with `translation=None`.

**NLLB details.**
- Model: `facebook/nllb-200-distilled-600M`. Load once; keep on GPU (`device="cuda"`) if VRAM allows, else CPU (still usable for short text). `compute_type` int8 on CTranslate2 for speed.
- **Language code mapping (Whisper ISO-639-1 ƒ+' NLLB FLORES code).** Maintain a dict; ship at least:
  | Whisper | NLLB |
  |--------|------|
  | `ja` | `jpn_Jpan` |
  | `vi` | `vie_Latn` |
  | `en` | `eng_Latn` |
  | `zh` | `zho_Hans` |
  | `ko` | `kor_Hang` |
  | `fr` | `fra_Latn` |
  | `de` | `deu_Latn` |
  | `es` | `spa_Latn` |
  | `ru` | `rus_Cyrl` |
  | `th` | `tha_Thai` |
  Target `vi` ƒ+' `vie_Latn`. If a detected `lang` has no mapping, skip translation (pass through) and log once.
- Inference: set `tokenizer.src_lang = <nllb_src>`, generate with `forced_bos_token_id = tokenizer.convert_tokens_to_ids(<nllb_tgt>)`, `max_length` scaled to input length, `num_beams` default 3.
- **Quality note (must appear in code comment + docs):** NLLB Japaneseƒ+'Vietnamese quality is *moderate*; for higher-quality JAƒ+'VI (or other hard pairs), the user can switch `translation_provider` to `gemini`, which improves results at the cost of sending text to Google. This is a deliberate quality/privacy tradeoff exposed in Settings.

**GeminiTranslator (stub).** Implements the same interface. `available()` returns true only if `gemini_api_key` is set. `translate()` calls the Gemini API with a terse system prompt ("Translate the following {src} text to Vietnamese. Output only the translation."). Off by default. If selected but no key, `available()` is false ƒ+' UI shows a clear "set your Gemini key" message and falls back to NLLB (or no translation).

**Error handling.** Any translation exception ƒ+' return original text unchanged is **wrong** (would look like a translation). Instead return `None`/empty and mark `translation_error` on the record; the UI shows the original with a small "translation failed" hint. Never block the pipeline.

### 5.5 `diarizer.py` ƒ?" speaker diarization (two tiers)

**Responsibility.** Assign a speaker label to each utterance. Tier 1 is real-time and rough; Tier 2 is offline and accurate.

**Public interface.**
```python
class RealtimeDiarizer:   # Tier 1
    def __init__(self, settings: Settings): ...
    def label(self, utt: Utterance) -> str      # "You" for mic; "Speaker N" for loopback
    def reset(self) -> None
    def rename(self, old_label: str, new_label: str) -> None

class OfflineDiarizer:    # Tier 2
    def __init__(self, settings: Settings): ...
    def rediarize(self, session_dir: str) -> list[SpeakerSpan]   # relabels transcript
```

**Tier 1 ƒ?" real-time online clustering.**
- **"You" (mic source):** always labelled `"You"`. No embedding needed.
- **"Them" (loopback):** for each utterance, extract a fixed-dim speaker embedding and assign it online.
  - **Embedding model:** ECAPA-TDNN via SpeechBrain (`speechbrain/spkrec-ecapa-voxceleb`) ƒ?" preferred. Fallback: Resemblyzer (`VoiceEncoder`) ƒ?" lighter, CPU-friendly. Selectable via `diarization_embedder: "ecapa"|"resemblyzer"`.
  - **Online clustering algorithm:** maintain a list of centroids `{label: (mean_embedding, count)}`.
    1. Compute embedding `e` (L2-normalized) for the utterance.
    2. If utterance shorter than `min_embed_ms` (default 800 ms), skip clustering and label `"Speaker ?"` (uncertain) ƒ?" too short for a reliable embedding.
    3. Find nearest centroid by cosine similarity. If `max_sim >= sim_threshold` (default 0.70) assign that label and update the centroid as a running mean (weighted by count, capped so it stays adaptive).
    4. Else create a new label `"Speaker {n+1}"` with `e` as its centroid.
  - **Cap** the number of speakers at `max_speakers` (default 8); beyond that, force-assign to nearest to avoid runaway label creation on noisy embeddings.
  - **Renaming:** `rename()` updates the label map; new incoming utterances that match the centroid keep the new name. Renames also propagate to already-emitted records via a store update (A5.7) and a WS `rename` event.
- **Toggle / cost:** controlled by `diarization_enabled` (Settings, default **ON**) and `diarization_realtime` (default ON). When realtime diarization is off, all "Them" utterances are labelled `"Them"` (single bucket). The embedding model adds VRAM (~1 GB ECAPA on GPU) and per-utterance latency (~50ƒ?"150 ms). If VRAM is tight, run the embedder on CPU (`diarization_device: "cpu"`) ƒ?" slower but non-blocking to Whisper. Document this VRAM/latency tradeoff in Settings help text.

**Tier 2 ƒ?" offline accurate re-diarization.**
- Triggered by the UI "Re-diarize" button ƒ+' REST ƒ+' runs `OfflineDiarizer.rediarize(session_dir)`.
- **Requires** the persisted audio. Re-diarize primarily on `audio_them.wav` (remote speakers) ƒ?" "You" stays "You". Optionally also process a **mixed** track if desired, but v1: run pyannote on `audio_them.wav` only, keep mic as "You".
- **Model:** `pyannote/speaker-diarization-3.1` pipeline via `pyannote.audio`. `Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)`. Run on GPU (`pipeline.to(torch.device("cuda"))`).
- **HuggingFace token** (free) required; user accepts the model's gated terms once on HF and pastes the token into Settings (`hf_token`). If missing, tier-2 is **disabled** and the button shows a clear message + link to instructions. See A6 & A8.
- **Relabelling algorithm:** pyannote returns speaker-homogeneous time spans with cluster ids (SPEAKER_00, ƒ?İ). For each existing "Them" utterance record, compute the overlap-weighted majority pyannote speaker over `[start, end]` and assign a stable label (`"Speaker A/B/ƒ?İ"`). Preserve any user renames by offering a mapping step (v1: apply pyannote clusters as fresh labels, but keep a `diarization_source` field so the UI shows "re-diarized" state). Rewrite `transcript.jsonl` speaker fields (new file version) and re-render `transcript.md`; keep a backup `transcript.jsonl.pre-rediarize`.
- Long-running: runs in a background thread; report progress/completion via WS + REST status.

### 5.6 `summarizer.py` ƒ?" AI summarization

**Responsibility.** Produce structured Markdown notes from the transcript **text** (not audio), once, on demand, post-meeting. Pluggable providers behind one interface.

**Public interface.**
```python
class Summarizer(Protocol):
    def summarize(self, transcript_text: str, meta: dict) -> str: ...  # returns markdown
    def available(self) -> tuple[bool, str]:  ...  # (ok, reason_if_not)

class ClaudeCliSummarizer(Summarizer):   # default
class CodexCliSummarizer(Summarizer):
class GeminiSummarizer(Summarizer):
class OllamaSummarizer(Summarizer):
```

**Selection.** `summarizer_provider` setting: `"claude_cli" | "codex_cli" | "gemini" | "ollama"`, default `"claude_cli"`.

**Input assembly.** Build a plain-text transcript from `transcript.jsonl`, one line per utterance: `"[mm:ss] <SpeakerLabel>: <text>"`, using the translated Vietnamese where translation exists (configurable `summary_use_translation`, default true) else original. Include a short header with meeting title/date/duration/languages. If the transcript is very long, chunk to fit context (`summary_max_chars`, default 48000) ƒ?" map-reduce: summarize chunks then summarize the summaries. Document the chunking.

**Prompt (default).** The prompt must let the model **self-organize** into whatever sections fit the content ƒ?" do not hard-force a fixed template. Ship this default (Vietnamese-first output):
```
You are an assistant that writes clear, well-structured meeting notes in Vietnamese.
Read the transcript below (speaker-labelled). Produce concise notes in Markdown.
Organize into whatever sections actually fit this meeting ƒ?" for example (only if relevant):
key points, decisions, action items (with owner if stated), open questions / Q&A,
risks or warnings, and misc notes. Omit sections that don't apply. Do not invent content
not supported by the transcript. Keep it skimmable.

TRANSCRIPT:
<transcript_text>
```
Prompt text lives in config (`summary_prompt`) and is user-editable.

**Provider implementations.**
- **ClaudeCliSummarizer (default):** shell out headless: `claude -p "<prompt+transcript>"` (or pass the prompt via stdin to avoid arg-length limits ƒ?" **preferred**: `claude -p` reading prompt from stdin / a temp prompt file). Capture stdout as the markdown. Use `subprocess.run([...], capture_output=True, text=True, timeout=summary_timeout_s, encoding="utf-8")`.
- **CodexCliSummarizer:** `codex exec` with the prompt (via stdin/temp file), capture stdout.
- **GeminiSummarizer:** call Gemini API with the prompt (needs `gemini_api_key`).
- **OllamaSummarizer:** POST to local Ollama (`http://localhost:11434/api/generate`, model `ollama_model` default `llama3.1`), stream/collect `response`.

**"CLI not installed" / unavailable handling.** `available()` probes: for CLI providers, check the binary exists (`shutil.which("claude")` / `"codex"`) and optionally a `--version`. If missing, `summarize()` is not called; UI shows: *"Claude CLI not found. Install it, or choose another summarizer (Codex / Gemini / Ollama) in Settings."* Same for `codex`, for a missing Ollama server (connection refused), and for a missing Gemini key. Never crash; return a clear error string that the UI renders as a notice (not saved as `summary.md`).

**Output.** On success, save markdown to `<session_dir>/summary.md` (overwrite allowed; keep a `.bak` of the previous). Return it over REST/WS so the UI shows it. Record `summary_provider` and `summarized_at` in session metadata.

**Windows subprocess notes.** Use `text=True, encoding="utf-8"`; set `creationflags=subprocess.CREATE_NO_WINDOW` so no console flashes. Prefer passing the prompt via a temp file path or stdin (arg length + quoting on Windows is fragile for long transcripts).


 succeeded in 234ms:
## 6. Data Model

Covered inline in A5.7 (JSONL utterance schema, `meta.json`, session folder layout, `transcript.md` format). Summary of persisted artifacts per session: `meta.json`, `transcript.jsonl` (authoritative append-only), `transcript.md` (rendered), `summary.md` (on demand), `audio_you.wav` / `audio_them.wav` (16 kHz mono PCM16, required for tier-2), plus backups on rewrite. Schema versioning via the `schema` integer on records and meta.

---

## 7. Settings / Config Reference

All keys, types, defaults. Persisted in `settings.json`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `consent_acknowledged` | bool | `false` | User accepted the Legal & Consent modal. |
| `consent_acknowledged_at` | str/null | `null` | ISO timestamp of acknowledgement. |
| `server_port` | int | `8848` | Localhost port (auto-bumps if busy). |
| `sessions_root` | str | `"./sessions"` | Root dir for session folders (under app data). |
| `audio_backend` | enum | `"auto"` | `auto`\|`soundcard`\|`pyaudiowpatch`. |
| `persist_audio` | bool | `true` | Write per-source WAVs (needed for tier-2). |
| `target_sample_rate` | int | `16000` | Pipeline sample rate (do not change in v1). |
| `frame_ms` | int | `20` | VAD frame hop in ms. |
| `vad_engine` | enum | `"silero"` | `silero`\|`webrtcvad`. |
| `vad_device` | enum | `"cpu"` | `cpu`\|`cuda` for Silero VAD. |
| `vad_aggressiveness` | int | `2` | webrtcvad only, 0Г?"3. |
| `pre_roll_ms` | int | `300` | Audio kept before speech onset. |
| `speech_start_ms` | int | `150` | Sustained speech to start an utterance. |
| `silence_end_ms` | int | `600` | Trailing silence to end an utterance (latency knob). |
| `min_speech_ms` | int | `250` | Drop utterances shorter than this. |
| `max_utterance_seconds` | int | `15` | Force-cut long utterances. |
| `whisper_model` | enum | `"large-v3"` | `small`\|`medium`\|`large-v2`\|`large-v3`. |
| `whisper_compute_type` | enum | `"float16"` | `float16`\|`int8_float16`\|`int8`. |
| `latency_mode` | enum | `"quality"` | `quality`(beam 5)\|`fast`(beam 1). |
| `whisper_vad_filter` | bool | `true` | Secondary VAD filter in faster-whisper. |
| `force_language` | str/null | `null` | Force Whisper language (e.g. `"ja"`), else auto. |
| `no_speech_threshold` | float | `0.6` | Hallucination guard. |
| `logprob_drop_threshold` | float | `-1.0` | Hallucination guard. |
| `min_rms` | float | `0.005` | Drop near-silent utterances. |
| `hallucination_denylist` | list[str] | (see A5.3) | Whole-utterance texts to drop. |
| `auto_downgrade_on_backpressure` | bool | `false` | Auto-shrink model if falling behind. |
| `backpressure_warn_seconds` | int | `8` | Sustained backlog Г+' warn. |
| `translate_enabled` | bool | `false` | Live translation on/off ("Dап<ch"). |
| `target_lang` | str | `"vi"` | Translation target. |
| `source_languages` | list[str] | `[]` | Langs to translate; empty = any non-target. |
| `translation_provider` | enum | `"nllb"` | `nllb`\|`gemini`. |
| `nllb_model` | str | `"facebook/nllb-200-distilled-600M"` | HF model id. |
| `translation_device` | enum | `"cuda"` | `cuda`\|`cpu` for NLLB. |
| `gemini_api_key` | str/null | `null` | For Gemini translate/summarize (masked). |
| `diarization_enabled` | bool | `true` | Master diarization toggle. |
| `diarization_realtime` | bool | `true` | Tier-1 online clustering on/off. |
| `diarization_embedder` | enum | `"ecapa"` | `ecapa`\|`resemblyzer`. |
| `diarization_device` | enum | `"cuda"` | `cuda`\|`cpu` for embeddings/pyannote. |
| `sim_threshold` | float | `0.70` | Cosine sim to merge into existing speaker. |
| `min_embed_ms` | int | `800` | Min utterance length to embed/cluster. |
| `max_speakers` | int | `8` | Cap on tier-1 speakers. |
| `hf_token` | str/null | `null` | HuggingFace token for pyannote (tier-2). |
| `pyannote_model` | str | `"pyannote/speaker-diarization-3.1"` | Tier-2 pipeline. |
| `summarizer_provider` | enum | `"claude_cli"` | `claude_cli`\|`codex_cli`\|`gemini`\|`ollama`. |
| `summary_prompt` | str | (see A5.6) | Editable summarization prompt. |
| `summary_use_translation` | bool | `true` | Feed Vietnamese text to summarizer when available. |
| `summary_max_chars` | int | `48000` | Chunking threshold (map-reduce beyond). |
| `summary_timeout_s` | int | `300` | Subprocess/API timeout. |
| `ollama_model` | str | `"llama3.1"` | Ollama model name. |
| `ollama_url` | str | `"http://localhost:11434"` | Ollama endpoint. |
| `fsync_interval_ms` | int | `1000` | Autosave durability throttle. |
| `theme` | enum | `"auto"` | `auto`\|`light`\|`dark`. |
| `app_version` | str | `"1.0"` | Read-only. |

Validators: enums constrained; thresholds range-checked; `source_languages` items must be known ISO codes; changing `whisper_model` mid-session takes effect at next `start`.

---

## 8. Error Handling

Dedicated behaviors (all surface a clear UI notice via WS `error`/`status`; none crash the pipeline):

1. **No loopback device / output missing:** start with mic only, `them.available=false`, status dot amber, note "Recording microphone only Г?" no system audio device." Transcript proceeds for "You".
2. **Mic blocked / missing (Windows privacy or no input device):** start with loopback only, `you.available=false`, note "Recording system audio only Г?" microphone unavailable (check Windows mic privacy)." 
3. **Both sources fail:** `start` returns error; do not enter recording state; UI shows actionable message (check audio devices).
4. **Device change mid-session:** auto-reopen with retries (A5.1); brief gap tolerated; note on lost/reopened.
5. **GPU OOM (load or inference):** empty cache Г+' downgrade model Г+' downgrade compute_type (float16Г+'int8_float16Г+'int8) (A5.3). Emit note describing the effective config. Persist to meta.
6. **Whisper hallucination on silence:** prevented by VAD gating + `min_speech_ms` + `no_speech_prob`/`avg_logprob` thresholds + denylist + `min_rms` (A5.3). Dropped utterances are silently not emitted (optionally logged at debug).
7. **Translation failure / unmapped language:** pass through original, mark `translation_error`/`translation=null`; UI shows a subtle "translation failed" hint. Unmapped language Г+' skip translation, log once.
8. **Summarizer CLI not installed / provider unavailable:** `available()` false Г+' graceful message naming the missing tool and suggesting an alternative provider; no `summary.md` written.
9. **HF token missing for tier-2:** Re-diarize button disabled/greyed with tooltip "Add a free HuggingFace token in Settings to enable accurate re-diarization" + link to model-terms acceptance.
10. **pyannote/model download failure or network offline:** clear error; tier-2 stays available once model cached.
11. **Backpressure (GPU can't keep up):** amber status, "falling behind"; optional auto-downgrade. Data still captured to WAV, so nothing is lost even if live transcript lags.
12. **Crash / power loss:** append-only fsynced JSONL means at most ~1 s of the last utterance line is lost; on relaunch, sessions are readable; `transcript.md` regenerated on demand from JSONL if missing.
13. **Port in use:** auto-bump `server_port`; report chosen port to pywebview.
14. **WebSocket disconnect:** UI auto-reconnects with backoff; on reconnect, server resends current status + recent utterances.

---


codex
I have the implementation details now. I’m checking the environment/risk sections because several defaults and assumptions contradict the component details.
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 801 | Select-Object -Skip 733" in C:\Users\DucNguyen\Code\ai-record
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 883 | Select-Object -Skip 801" in C:\Users\DucNguyen\Code\ai-record
exec
"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-Content -LiteralPath .\\docs\\SPEC.md -TotalCount 170 | Select-Object -Skip 80" in C:\Users\DucNguyen\Code\ai-record
 succeeded in 232ms:
## 10. Dependencies & Environment

### 10.1 Assumptions
- Windows 11 (WASAPI loopback is **Windows-specific** ƒ?" the app does not run on macOS/Linux in v1).
- Python 3.12.
- NVIDIA GPU with a working CUDA-enabled PyTorch already installed; faster-whisper already installed and functional on GPU.
- Internet access on **first run only** to download models (thereafter cached under the HF/torch cache).

### 10.2 Python packages (`requirements.txt`)
```
# --- STT (already installed, pinned for clarity) ---
faster-whisper>=1.0.0

# --- audio capture (Windows WASAPI) ---
soundcard>=0.4.3          # preferred loopback capture
PyAudioWPatch>=0.2.12     # fallback WASAPI loopback

# --- resampling / audio io ---
numpy>=1.26
scipy>=1.11
soxr>=0.3.7               # high-quality streaming resample (optional but recommended)
soundfile>=0.12           # WAV read/write

# --- VAD ---
silero-vad>=5.1           # preferred (torch)
webrtcvad>=2.0.10         # fallback

# --- translation (NLLB) ---
transformers>=4.44
sentencepiece>=0.2
ctranslate2>=4.3          # optional faster NLLB backend
torch                     # provided by existing CUDA install (do not reinstall blindly)

# --- speaker embeddings (tier-1) ---
speechbrain>=1.0          # ECAPA-TDNN
resemblyzer>=0.1.4        # fallback embedder

# --- diarization (tier-2) ---
pyannote.audio>=3.1

# --- backend / server ---
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic>=2.7
websockets>=12

# --- desktop shell ---
pywebview>=5.1

# --- misc ---
python-dateutil>=2.9
```
Notes:
- **Do not** let `requirements.txt` clobber the user's working CUDA `torch`/`faster-whisper`. In practice, install the rest with `--no-deps` where needed, or document that torch is pre-provided. Provide a `requirements.txt` plus a `requirements-notes.md` warning about torch.
- Optional providers (`google-generativeai` for Gemini) are **not** required; documented as extras installed only if the user enables that provider. Ollama and Claude/Codex CLIs are external executables, not pip deps.

### 10.3 Model downloads (first run, sizes approximate)
- Whisper `large-v3` (CTranslate2): ~3 GB VRAM at fp16; model files ~1.5 GB. `medium` ~0.8 GB files.
- NLLB-200 distilled-600M: ~2.5 GB files, ~1.5ƒ?"2 GB VRAM (or CPU).
- ECAPA-TDNN (SpeechBrain): ~80 MB, ~1 GB VRAM on GPU.
- pyannote 3.1 pipeline: ~30ƒ?"100 MB, gated (needs HF token + accept terms).
Document total first-run download (~5ƒ?"7 GB) and that subsequent runs are offline-capable.

### 10.4 VRAM budget guidance
Rough concurrent GPU residency with defaults: Whisper large-v3 (~3 GB) + NLLB (~1.5 GB) + ECAPA (~1 GB) ƒ%^ 5.5 GB. On <8 GB cards, recommend either `whisper_model=medium`, `translation_device=cpu`, or `diarization_device=cpu`. Provide these as documented Settings knobs (A7). A single `torch` device lock serializes GPU submissions to avoid fragmentation spikes.

---


 succeeded in 239ms:
## 11. Repo Layout & Entry Point

### 11.1 Layout
```
ai-record/
  README.md
  requirements.txt
  requirements-notes.md          # torch/CUDA caveat, model sizes
  docs/
    SPEC.md                      # this document
  main.py                        # thin launcher -> ai_record.__main__
  ai_record/
    __init__.py
    __main__.py                  # `python -m ai_record` entry
    config.py                    # Settings (pydantic) + load/save
    capture.py                   # WASAPI dual-stream capture + resample
    ring_buffer.py               # RingBuffer helper
    segmenter.py                 # per-source VAD segmentation
    transcriber.py               # faster-whisper wrapper
    translator.py                # Translator interface + NLLB + Gemini stub
    diarizer.py                  # RealtimeDiarizer (T1) + OfflineDiarizer (T2)
    summarizer.py                # Summarizer interface + Claude/Codex/Gemini/Ollama
    store.py                     # SessionStore + WavWriter
    pipeline.py                  # wires capture->segment->transcribe->translate->diarize->store/broadcast
    server.py                    # FastAPI app, REST + WS, lifecycle
    app.py                       # starts uvicorn thread + opens pywebview window
    lang_maps.py                 # Whisper<->NLLB code maps, denylists
  web/
    index.html
    app.js
    styles.css
    assets/                      # bundled fonts/icons (no CDN)
  sessions/                      # created at runtime (gitignored)
  tests/
    unit/
    integration/
    fixtures/                    # small known WAVs
```
App data dir: on Windows, sessions + settings default under the repo (`./sessions`, `./settings.json`) for v1 simplicity; document that a future version may move to `%LOCALAPPDATA%\ai-record\`.

### 11.2 Entry point (`python -m ai_record` / `main.py`)
`app.py`:
1. Load `Settings`.
2. Start Uvicorn (FastAPI `server.app`) in a background thread bound to `127.0.0.1:<port>` (with port-bump). Wait until health-ready.
3. Open a pywebview window (frameless, on-top, compact size) pointing at `http://127.0.0.1:<port>`.
4. `webview.start()` blocks on the GUI loop; on window close, signal the server to stop capture (if active) and shut down Uvicorn, then exit.
`main.py` is a one-liner: `from ai_record.app import main; main()`. `__main__.py` calls the same.

---

## 12. Milestones / Phases

- **M0 Г?" Skeleton:** repo layout, `config.py`, FastAPI server with `/health` + static UI shell, pywebview window opens. (No audio.)
- **M1 Г?" Capture:** `capture.py` dual WASAPI streams + resample + ring buffers + WAV persistence; status/device-missing handling; a debug endpoint showing RMS levels. Manual verify audio flows.
- **M2 Г?" Segment + Transcribe:** `segmenter.py` + `transcriber.py`; live utterances pushed over WS; compact bar shows transcript. Hallucination guards. This is the first end-to-end "it transcribes a meeting" milestone.
- **M3 Г?" Storage + Sessions:** `store.py` autosave JSONL/MD/WAV, sessions list/open, `transcript.md` finalize. Crash-safety verified.
- **M4 Г?" Translation:** `translator.py` NLLB + gating + "Dап<ch" toggle + inline translation in UI + Gemini stub.
- **M5 Г?" Diarization T1:** real-time embedding + online clustering + renameable labels.
- **M6 Г?" Summarize:** `summarizer.py` Claude CLI default + provider selection + summary panel + graceful missing-CLI.
- **M7 Г?" Diarization T2:** pyannote re-diarize button + HF token flow + transcript relabel + backups.
- **M8 Г?" Expanded UI + polish:** full transcript view, search, settings panel, theme, design pass (anti-slop), error surfacing.
- **M9 Г?" Tests + docs:** unit + integration suites, README, first-run consent modal, manual smoke test checklist.
- **Future (out of v1):** `.exe` packaging (PyInstaller), overlapping-speech separation, app-data-dir migration, per-tenant model presets.

---

## 13. Risks & Open Questions

- **Loopback library reliability.** `soundcard` loopback on Windows can be finicky across driver/format combos; `pyaudiowpatch` is the fallback but has its own device-index quirks. Risk: some machines only work on one backend. Mitigation: `audio_backend` override + robust device probing. Needs real-hardware validation (not auto-testable).
- **Real-time performance under large-v3.** On modest GPUs, `large-v3` per-utterance latency may exceed comfort; backpressure/auto-downgrade helps but the default may need to be `medium` for some users. Open: pick default per detected VRAM at first run?
- **Concurrent GPU residency.** Whisper + NLLB + ECAPA + (occasionally) pyannote can exceed 8 GB VRAM. Mitigated by CPU-offload knobs, but the *default* config assumes ~8Г?"12 GB. Open: dynamic device placement based on detected VRAM.
- **NLLB JAГ+'VI quality.** Explicitly moderate; Gemini improves it but breaks the "fully local" promise. This is a documented tradeoff, not a bug.
- **Diarization accuracy on short utterances / overlapping speech.** Tier-1 online clustering will mislabel short/overlapping segments; tier-2 pyannote is the corrective, but overlapping speech remains a known v1 limitation (non-goal).
- **Timestamp alignment for tier-2.** Utterance timestamps must map accurately onto `audio_them.wav` sample positions for pyannote overlap-majority relabelling. Any drift between capture start and the session clock, or resampler latency, could misalign. Mitigation: derive timestamps from cumulative sample counts of the persisted stream, not wall-clock.
- **Secrets at rest.** `hf_token` / `gemini_api_key` stored plaintext in `settings.json` (single-user desktop). Acceptable v1; hardening (OS keyring) noted as future.
- **CLI summarizer variability.** `claude -p` / `codex exec` output format and flags can change; long transcripts via arg vs stdin. Mitigation: prefer stdin/temp-file, timeout, and treat output as opaque markdown.
- **Legal exposure.** The core function (recording without a platform indicator) is legally sensitive; the consent gate + honest framing (A3) are mandatory, but ultimately the user bears compliance responsibility.
- **pywebview frameless drag/controls on Windows.** Custom window chrome (drag region, always-on-top toggling) can be fiddly with the EdgeChromium backend. Needs verification; fallback to a thin native title bar if frameless drag proves unstable.

---

*End of specification.*

 succeeded in 243ms:
## 4. Architecture

### 4.1 High-level model

ai-record is a single Python process that runs:
- A **FastAPI + Uvicorn** server (HTTP + WebSocket) on `127.0.0.1` (localhost only, never `0.0.0.0`).
- A **capture/processing pipeline** running in background threads and asyncio tasks.
- A **pywebview** window that loads the local web UI (`http://127.0.0.1:<port>`).

The pipeline is a chain of producer/consumer stages connected by bounded queues. Each source ("You", "Them") has its own capture Г+' ring buffer Г+' segmenter. Finalized utterances from both sources are merged into a single work queue consumed by the transcription worker, then translation, then real-time diarization, then persistence + WebSocket broadcast.

### 4.2 ASCII data-flow diagram

```
                          WINDOWS AUDIO SUBSYSTEM (WASAPI)
        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?   Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
        Г", Default OUTPUT device          Г",   Г", Default INPUT (mic)      Г",
        Г", (speakers) Г?" LOOPBACK          Г",   Г",                          Г",
        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~   Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г", raw frames (native SR)          Г", raw frames (native SR)
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", capture.py        Г",              Г", capture.py        Г",
              Г",  loopback stream  Г",              Г",  mic stream       Г",
              Г",  Г+' resample 16k   Г",              Г",  Г+' resample 16k   Г",
              Г",  Г+' mono           Г",              Г",  Г+' mono           Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г", 16k mono float32 frames          Г",
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", RingBuffer "Them" Г",              Г", RingBuffer "You"  Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г-м                                  Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", segmenter.py      Г",              Г", segmenter.py      Г",
              Г", VAD (Silero)      Г",              Г", VAD (Silero)      Г",
              Г", Г+' utterance chunk Г",              Г", Г+' utterance chunk Г",
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                        Г",  Utterance{source, pcm, t0, t1}  Г",
                        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г",  utterance_queue  Г",  (bounded, backpressure)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", transcriber.py    Г",  faster-whisper (GPU, fp16)
                             Г", text + lang       Г",  VAD filter (secondary)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", translator.py     Г",  NLLB (if enabled & langГ% vi)
                             Г", + translation     Г",
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                             Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                             Г", diarizer.py (T1)  Г",  ECAPA embed + online cluster
                             Г", speaker label     Г",  ("You" is fixed for mic)
                             Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"кГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
                                       Г-м
                        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"'Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
                        Г-м                              Г-м
              Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?        Г"OГ"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?
              Г", store.py          Г",        Г", server.py WebSocket   Г",
              Г", append JSONL +    Г",        Г", push live utterance   Г",
              Г", transcript.md +   Г",        Г", Г+' UI (compact/expand) Г",
              Г", audio_*.wav       Г",        Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~
              Г""Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"?Г"~

   POST-MEETING (on demand):
     summarizer.py  Г"?Г"? reads transcript TEXT Г"?Г"?Г-з summary.md
     diarizer.py T2 Г"?Г"? reads audio_*.wav (pyannote) Г"?Г"?Г-з relabel transcript
```

### 4.3 Concurrency & backpressure model

- **Capture threads (2):** one per source. Each is a dedicated OS thread (audio callbacks / blocking record loops). They write fixed-size frames into a lock-free-ish `RingBuffer` (a `numpy` circular buffer guarded by a `threading.Lock`, or `collections.deque` of frames). Capture must never block on downstream work; if the ring buffer is full it overwrites oldest data and increments a `dropped_frames` counter (logged, surfaced as a warning if sustained).
- **Segmenter threads (2):** one per source. Each pulls frames from its ring buffer, runs VAD, and emits `Utterance` objects onto the shared bounded `utterance_queue` (`queue.Queue(maxsize=32)`).
- **Transcription worker (1 thread):** the GPU is a single serialized resource. One worker pulls from `utterance_queue` and runs faster-whisper. **Backpressure:** if the queue is full, segmenters block on `put()` Г?" but since segments are only produced at speech boundaries and transcription is faster-than-realtime on GPU for `large-v3` with short utterances, this is rarely hit. If it is hit repeatedly (queue full > `backpressure_warn_seconds`), the app: (a) logs a warning, (b) surfaces a "falling behind" status dot, and (c) optionally auto-downgrades the model per settings (`auto_downgrade_on_backpressure`).
- **Translation + T1 diarization:** run inline on the transcription worker thread *or* on a small follow-on worker (`post_queue`, maxsize=32). Recommended: keep transcription worker lean (STT only) and run translate+diarize on a separate `post_worker` thread so GPU STT is never stalled by translation model inference. Both translation (NLLB) and diarization embeddings can share the GPU; serialize GPU access with a single `torch` device lock if VRAM is tight (see A11.5).
- **Persistence + broadcast:** the `post_worker` (or a dedicated `sink` thread) writes to `store.py` (append-only, fast) and publishes to an asyncio broadcast. Because FastAPI/WebSocket lives on the asyncio event loop and workers are threads, cross into the loop via `asyncio.run_coroutine_threadsafe(broadcast(msg), loop)` or an `asyncio.Queue` fed through `loop.call_soon_threadsafe`.
- **Bridge (threads Г+" asyncio):** the server holds a reference to the running event loop. Worker threads enqueue outgoing WS messages using `loop.call_soon_threadsafe(async_queue.put_nowait, msg)`. A single async task drains `async_queue` and fans out to all connected WS clients.
- **Ordering:** utterances may finish transcription slightly out of wall-clock order across sources (a long "Them" utterance vs a short "You" one). Each utterance carries `start`/`end` timestamps (seconds from session start). The UI orders by `start`. `store.py` appends in completion order to JSONL but each record has authoritative timestamps; the rendered `transcript.md` is sorted by `start` on finalize/close.

### 4.4 Timebase

A single monotonic session clock starts at capture start (`session_t0 = time.perf_counter()`). All utterance `start`/`end` are seconds relative to `session_t0`, derived from sample counts in each source's stream (sample_index / 16000) to keep audio and transcript aligned for tier-2 re-diarization. Capture start for both streams is recorded; a small per-stream offset (measured start delta) is stored so audio files and timestamps line up.

---


codex
## Critical

1. **Default real-time stack is not credible on 8-12GB GPUs** (`§4.3`, `§5.3`, `§5.4`, `§5.5`, `§7`, `§10.4`). The spec defaults to `large-v3` fp16, beam 5 quality mode, GPU NLLB, and GPU ECAPA. The claim that `large-v3` is “faster-than-realtime” enough for dual-stream meetings is too optimistic, especially once translation and embeddings contend for the same GPU.  
   **Fix:** default to hardware presets: <=8GB should use `medium` or `small`, `int8_float16`, beam 1, CPU translation, CPU/resemblyzer or no realtime diarization. Use `large-v3` fp16 only on explicit quality mode or >=16GB after a benchmark passes.

2. **Fallback ladder is incomplete and starts too late** (`§4.3`, `§5.3`, `§8.11`, `§13`). Backpressure warning after 8 seconds means the live transcript is already behind. Auto-downgrade defaults to false.  
   **Fix:** make `auto_downgrade_on_backpressure=true` by default and define this ladder: beam 5 -> beam 1 -> move NLLB to CPU -> move/disable realtime diarization -> `large-v3 int8_float16` -> `medium int8_float16` -> `small int8` -> disable live translation -> audio-only capture with offline catch-up.

3. **Post-processing can starve transcription** (`§4.3`, `§5.4`, `§5.5`). A “post_worker” that runs NLLB and ECAPA under a shared GPU lock can block STT, which is the only stage that must stay live.  
   **Fix:** emit/store the STT result before translation/diarization. Run translation and T1 diarization as lower-priority async updates. If GPU is shared, use a priority GPU scheduler where STT always preempts post-processing, or default post-processing to CPU.

4. **The timebase design is not sound across independent devices** (`§4.4`, `§5.1`, `§5.5`, `§13`). Loopback and mic clocks can drift. A one-time start offset does not keep streams aligned over a long meeting, and device reopen creates discontinuities. Tier-2 pyannote spans are in `audio_them.wav` time, not global session time.  
   **Fix:** store per-source audio sample positions on every utterance: `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `source_offset_sec`, and discontinuities. Tier-2 relabeling must overlap pyannote spans against `audio_them.wav` sample time, not raw session timestamps.

5. **WASAPI capture is under-specified for real Windows failures** (`§5.1`, `§8.1`, `§8.4`). The spec does not pin actual sample-rate discovery, channel count, sample format conversion, pyaudio byte decoding, soundcard loopback device identity, or silent-loopback detection.  
   **Fix:** define a backend contract that reports actual opened sample rate, channels, format, device id, and block duration. Add source-health telemetry: RMS, zero-frame counter, overrun count, reopen count. Warn if loopback is open but silent for N seconds while recording.

6. **Crash-safety claim is false for queued work and WAVs** (`§1`, `§5.7`, `§8.12`). Autosaving only finalized utterances means a crash loses anything still in VAD/STT/translation queues. WAV files may also have invalid headers after power loss.  
   **Fix:** persist capture audio independently in crash-recoverable chunks, for example rolling 1-minute WAVs with finalized headers or raw PCM plus sidecar sample counts. On startup, detect incomplete sessions and offer offline transcription recovery for missing transcript tail.

7. **Local API has no real protection** (`§3`, `§4.1`, `§5.8`, `§5.9`). Binding to `127.0.0.1` is necessary but not sufficient. Any local process, and some browser-based CSRF patterns, could hit `/api/capture/start`. The consent gate is described mainly as UI behavior.  
   **Fix:** enforce consent server-side in `POST /api/capture/start`. Generate a random per-launch API token, inject it into the pywebview URL/session, require it for REST and WebSocket, and reject unexpected `Origin` headers.

8. **CLI summarizer is a prompt-injection and privacy hazard** (`§5.6`, `§7`, `§13`). Passing transcript text to `claude -p` or `codex exec` is not just summarization; those are agentic CLIs that may have filesystem or network abilities. Meeting participants can literally say instructions that become prompt injection.  
   **Fix:** treat transcript as untrusted data. Use stdin only, no shell, isolated cwd, no tool/network mode if the CLI supports it, and a hard delimiter/system instruction. If the CLI cannot be sandboxed, do not make it the default provider.

9. **The spec says “locked / implementation-ready” while leaving core decisions open** (`§13`). Default model selection, dynamic VRAM placement, timestamp alignment, and loopback reliability are still open questions.  
   **Fix:** resolve those into normative requirements before coding. Add measurable acceptance criteria: p50/p95 STT latency, max backlog, recovery behavior, supported GPU presets, and Windows audio test matrix.

## Important

1. **Realtime diarization algorithm is naive** (`§5.5`, `§7`). A fixed cosine threshold of `0.70` is not portable across ECAPA, Resemblyzer, room acoustics, languages, or utterance length. Running means will drift after false assignments.  
   **Fix:** add confidence scoring, model-specific thresholds, minimum accumulated speech per speaker, and “unknown” labels. Do not update centroids on low-confidence, short, noisy, or overlapping utterances.

2. **`max_speakers` force-assignment hides failures** (`§5.5`, `§7`). Forcing new speakers into nearest existing clusters after 8 speakers silently corrupts diarization.  
   **Fix:** label as `Speaker ?` or `overflow_unknown` and surface a warning instead of pretending the nearest centroid is correct.

3. **Overlapping speech is waved away but still affects core output** (`§2.2`, `§5.5`, `§13`). “Best effort” is acceptable, but the spec still applies majority relabeling and centroid updates as if segments are speaker-homogeneous.  
   **Fix:** mark overlap/low-confidence regions, skip centroid updates for them, and preserve multiple possible speakers in offline diarization where pyannote reports overlap.

4. **Backpressure policy loses live transcript work** (`§4.3`). Segmenters block on `utterance_queue.put()`, capture overwrites ring-buffer data, and the spec says “data still captured to WAV” as if that solves live STT loss.  
   **Fix:** persist utterance audio jobs before enqueueing STT. On overload, drop/defer translation and T1 first, never raw capture or STT jobs.

5. **WebSocket fanout can block or drop silently** (`§4.3`, `§5.8`). `loop.call_soon_threadsafe(async_queue.put_nowait, msg)` can raise `QueueFull` inside the event loop callback, and a slow WebSocket client can stall broadcast.  
   **Fix:** use per-client bounded queues, drop/coalesce status messages, replay durable utterances by `seq`, and log queue drops explicitly.

6. **Store rewrite races are not handled** (`§5.7`, `§5.8`). Rename and rediarize rewrite `transcript.jsonl` while capture may append; summarize may read while writes are in progress.  
   **Fix:** add a session-level read/write lock. Disallow rediarize during active capture or run it against a snapshot. Make rewrites atomic in the same directory with `os.replace`.

7. **Secrets handling contradicts the UI promise** (`§5.9`, `§5.10`, `§7`, `§13`). `GET /api/settings` returns the “full settings object”, but HF/Gemini secrets must never be echoed. Plaintext-at-rest should not be accepted casually for a privacy app.  
   **Fix:** use Windows Credential Manager via `keyring`; return redacted settings; expose separate write-only secret update endpoints. At minimum set owner-only ACLs on settings.

8. **Translation path needs a latency budget** (`§5.4`, `§7`). NLLB beam 3 per utterance can lag badly, and language detection on short utterances is unreliable.  
   **Fix:** translate after STT emission, batch short adjacent utterances by language/speaker, use CTranslate2 int8 by default, and add a translation backlog policy that skips stale translations.

9. **Whisper chunking will hurt accuracy on forced cuts** (`§5.2`, `§5.3`). `max_utterance_seconds=15` plus `condition_on_previous_text=False` is safe against hallucination but loses context mid-sentence.  
   **Fix:** add overlap padding for forced cuts, persist `forced_cut`, and optionally stitch adjacent forced chunks before summarization.

10. **Session storage under the repo is a privacy footgun** (`§7`, `§11.1`). Meeting audio, transcripts, summaries, and plaintext settings under `./sessions` risk accidental git add or repo sync.  
   **Fix:** default immediately to `%LOCALAPPDATA%\\ai-record\\`, not as future work. Keep repo-local storage only as a dev override.

11. **No delete/retention workflow** (`§5.8`, `§5.9`). A privacy-focused recorder needs first-class deletion.  
   **Fix:** add session delete, delete-audio-only, retention settings, and secure confirmation UI.

12. **Model download and capability preflight is missing** (`§10.1`, `§10.3`, `§12`). First-run downloads, gated pyannote access, CUDA mismatch, and offline mode will fail during recording if not checked.  
   **Fix:** add a preflight screen: CUDA ok, faster-whisper load ok, model cache status, disk space, HF terms accepted, CLI availability, and estimated preset.

## Minor

1. **The spec file appears mojibaked** (`§1`, `§5.3`, `§5.9`, examples throughout). UI strings, arrows, Vietnamese text, and denylist entries are corrupted.  
   **Fix:** normalize the file to UTF-8 and replace corrupted examples before implementation.

2. **Several placeholders remain** (`§5.1`, `§5.3`, `§5.5`, `§5.8`). Examples like `channels=...`, beam size placeholders, and broken speaker labels are not buildable.  
   **Fix:** replace placeholders with concrete defaults and valid enum values.

3. **Broken section references reduce trust** (`§4.3`, `§5.1`, `§13`). References like `A11.5` do not exist; VRAM guidance is actually `§10.4`.  
   **Fix:** run a markdown link/reference check and normalize all section citations.

4. **Data schema lacks fields needed by the design** (`§5.7`, `§6`). Records need `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `forced_cut`, diarization confidence, and effective model/provider metadata.  
   **Fix:** add these to schema v1 now; migrations are harder later.

## Suggestions/Improvements

1. **Cut v1 scope** (`§2.1`, `§12`). Ship capture + storage + STT + consent + recovery first. Make live NLLB, realtime diarization, and pyannote rediarization optional post-MVP gates after benchmark proof.

2. **Add a real benchmark harness** (`§9`, `§13`). Use dual-stream WAV fixtures and report RTF, p95 latency, queue depth, VRAM peak, and dropped frames for 8GB, 12GB, and CPU-degraded presets.

3. **Use progressive UI updates** (`§5.8`, `§5.9`). Show transcript immediately after STT, then patch in translation and speaker label later. This makes latency tolerable even when post-processing is slow.

4. **Add degraded-mode UX** (`§8`, `§10.4`). The app should clearly show “recording audio only”, “STT catching up”, “translation paused”, or “speaker labels offline-only” instead of one generic amber dot.

## Verdict

The spec is not implementation-ready. The core product is plausible, but the current design overcommits on real-time GPU performance, has an invalid timebase model for independent Windows audio streams, overstates crash safety, and under-secures a sensitive local recording API plus agentic summarizer. Resolve the defaults/fallback ladder, audio timeline model, crash recovery format, server-side consent/auth, and v1 scope before writing production code.
tokens used
76,863
## Critical

1. **Default real-time stack is not credible on 8-12GB GPUs** (`§4.3`, `§5.3`, `§5.4`, `§5.5`, `§7`, `§10.4`). The spec defaults to `large-v3` fp16, beam 5 quality mode, GPU NLLB, and GPU ECAPA. The claim that `large-v3` is “faster-than-realtime” enough for dual-stream meetings is too optimistic, especially once translation and embeddings contend for the same GPU.  
   **Fix:** default to hardware presets: <=8GB should use `medium` or `small`, `int8_float16`, beam 1, CPU translation, CPU/resemblyzer or no realtime diarization. Use `large-v3` fp16 only on explicit quality mode or >=16GB after a benchmark passes.

2. **Fallback ladder is incomplete and starts too late** (`§4.3`, `§5.3`, `§8.11`, `§13`). Backpressure warning after 8 seconds means the live transcript is already behind. Auto-downgrade defaults to false.  
   **Fix:** make `auto_downgrade_on_backpressure=true` by default and define this ladder: beam 5 -> beam 1 -> move NLLB to CPU -> move/disable realtime diarization -> `large-v3 int8_float16` -> `medium int8_float16` -> `small int8` -> disable live translation -> audio-only capture with offline catch-up.

3. **Post-processing can starve transcription** (`§4.3`, `§5.4`, `§5.5`). A “post_worker” that runs NLLB and ECAPA under a shared GPU lock can block STT, which is the only stage that must stay live.  
   **Fix:** emit/store the STT result before translation/diarization. Run translation and T1 diarization as lower-priority async updates. If GPU is shared, use a priority GPU scheduler where STT always preempts post-processing, or default post-processing to CPU.

4. **The timebase design is not sound across independent devices** (`§4.4`, `§5.1`, `§5.5`, `§13`). Loopback and mic clocks can drift. A one-time start offset does not keep streams aligned over a long meeting, and device reopen creates discontinuities. Tier-2 pyannote spans are in `audio_them.wav` time, not global session time.  
   **Fix:** store per-source audio sample positions on every utterance: `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `source_offset_sec`, and discontinuities. Tier-2 relabeling must overlap pyannote spans against `audio_them.wav` sample time, not raw session timestamps.

5. **WASAPI capture is under-specified for real Windows failures** (`§5.1`, `§8.1`, `§8.4`). The spec does not pin actual sample-rate discovery, channel count, sample format conversion, pyaudio byte decoding, soundcard loopback device identity, or silent-loopback detection.  
   **Fix:** define a backend contract that reports actual opened sample rate, channels, format, device id, and block duration. Add source-health telemetry: RMS, zero-frame counter, overrun count, reopen count. Warn if loopback is open but silent for N seconds while recording.

6. **Crash-safety claim is false for queued work and WAVs** (`§1`, `§5.7`, `§8.12`). Autosaving only finalized utterances means a crash loses anything still in VAD/STT/translation queues. WAV files may also have invalid headers after power loss.  
   **Fix:** persist capture audio independently in crash-recoverable chunks, for example rolling 1-minute WAVs with finalized headers or raw PCM plus sidecar sample counts. On startup, detect incomplete sessions and offer offline transcription recovery for missing transcript tail.

7. **Local API has no real protection** (`§3`, `§4.1`, `§5.8`, `§5.9`). Binding to `127.0.0.1` is necessary but not sufficient. Any local process, and some browser-based CSRF patterns, could hit `/api/capture/start`. The consent gate is described mainly as UI behavior.  
   **Fix:** enforce consent server-side in `POST /api/capture/start`. Generate a random per-launch API token, inject it into the pywebview URL/session, require it for REST and WebSocket, and reject unexpected `Origin` headers.

8. **CLI summarizer is a prompt-injection and privacy hazard** (`§5.6`, `§7`, `§13`). Passing transcript text to `claude -p` or `codex exec` is not just summarization; those are agentic CLIs that may have filesystem or network abilities. Meeting participants can literally say instructions that become prompt injection.  
   **Fix:** treat transcript as untrusted data. Use stdin only, no shell, isolated cwd, no tool/network mode if the CLI supports it, and a hard delimiter/system instruction. If the CLI cannot be sandboxed, do not make it the default provider.

9. **The spec says “locked / implementation-ready” while leaving core decisions open** (`§13`). Default model selection, dynamic VRAM placement, timestamp alignment, and loopback reliability are still open questions.  
   **Fix:** resolve those into normative requirements before coding. Add measurable acceptance criteria: p50/p95 STT latency, max backlog, recovery behavior, supported GPU presets, and Windows audio test matrix.

## Important

1. **Realtime diarization algorithm is naive** (`§5.5`, `§7`). A fixed cosine threshold of `0.70` is not portable across ECAPA, Resemblyzer, room acoustics, languages, or utterance length. Running means will drift after false assignments.  
   **Fix:** add confidence scoring, model-specific thresholds, minimum accumulated speech per speaker, and “unknown” labels. Do not update centroids on low-confidence, short, noisy, or overlapping utterances.

2. **`max_speakers` force-assignment hides failures** (`§5.5`, `§7`). Forcing new speakers into nearest existing clusters after 8 speakers silently corrupts diarization.  
   **Fix:** label as `Speaker ?` or `overflow_unknown` and surface a warning instead of pretending the nearest centroid is correct.

3. **Overlapping speech is waved away but still affects core output** (`§2.2`, `§5.5`, `§13`). “Best effort” is acceptable, but the spec still applies majority relabeling and centroid updates as if segments are speaker-homogeneous.  
   **Fix:** mark overlap/low-confidence regions, skip centroid updates for them, and preserve multiple possible speakers in offline diarization where pyannote reports overlap.

4. **Backpressure policy loses live transcript work** (`§4.3`). Segmenters block on `utterance_queue.put()`, capture overwrites ring-buffer data, and the spec says “data still captured to WAV” as if that solves live STT loss.  
   **Fix:** persist utterance audio jobs before enqueueing STT. On overload, drop/defer translation and T1 first, never raw capture or STT jobs.

5. **WebSocket fanout can block or drop silently** (`§4.3`, `§5.8`). `loop.call_soon_threadsafe(async_queue.put_nowait, msg)` can raise `QueueFull` inside the event loop callback, and a slow WebSocket client can stall broadcast.  
   **Fix:** use per-client bounded queues, drop/coalesce status messages, replay durable utterances by `seq`, and log queue drops explicitly.

6. **Store rewrite races are not handled** (`§5.7`, `§5.8`). Rename and rediarize rewrite `transcript.jsonl` while capture may append; summarize may read while writes are in progress.  
   **Fix:** add a session-level read/write lock. Disallow rediarize during active capture or run it against a snapshot. Make rewrites atomic in the same directory with `os.replace`.

7. **Secrets handling contradicts the UI promise** (`§5.9`, `§5.10`, `§7`, `§13`). `GET /api/settings` returns the “full settings object”, but HF/Gemini secrets must never be echoed. Plaintext-at-rest should not be accepted casually for a privacy app.  
   **Fix:** use Windows Credential Manager via `keyring`; return redacted settings; expose separate write-only secret update endpoints. At minimum set owner-only ACLs on settings.

8. **Translation path needs a latency budget** (`§5.4`, `§7`). NLLB beam 3 per utterance can lag badly, and language detection on short utterances is unreliable.  
   **Fix:** translate after STT emission, batch short adjacent utterances by language/speaker, use CTranslate2 int8 by default, and add a translation backlog policy that skips stale translations.

9. **Whisper chunking will hurt accuracy on forced cuts** (`§5.2`, `§5.3`). `max_utterance_seconds=15` plus `condition_on_previous_text=False` is safe against hallucination but loses context mid-sentence.  
   **Fix:** add overlap padding for forced cuts, persist `forced_cut`, and optionally stitch adjacent forced chunks before summarization.

10. **Session storage under the repo is a privacy footgun** (`§7`, `§11.1`). Meeting audio, transcripts, summaries, and plaintext settings under `./sessions` risk accidental git add or repo sync.  
   **Fix:** default immediately to `%LOCALAPPDATA%\\ai-record\\`, not as future work. Keep repo-local storage only as a dev override.

11. **No delete/retention workflow** (`§5.8`, `§5.9`). A privacy-focused recorder needs first-class deletion.  
   **Fix:** add session delete, delete-audio-only, retention settings, and secure confirmation UI.

12. **Model download and capability preflight is missing** (`§10.1`, `§10.3`, `§12`). First-run downloads, gated pyannote access, CUDA mismatch, and offline mode will fail during recording if not checked.  
   **Fix:** add a preflight screen: CUDA ok, faster-whisper load ok, model cache status, disk space, HF terms accepted, CLI availability, and estimated preset.

## Minor

1. **The spec file appears mojibaked** (`§1`, `§5.3`, `§5.9`, examples throughout). UI strings, arrows, Vietnamese text, and denylist entries are corrupted.  
   **Fix:** normalize the file to UTF-8 and replace corrupted examples before implementation.

2. **Several placeholders remain** (`§5.1`, `§5.3`, `§5.5`, `§5.8`). Examples like `channels=...`, beam size placeholders, and broken speaker labels are not buildable.  
   **Fix:** replace placeholders with concrete defaults and valid enum values.

3. **Broken section references reduce trust** (`§4.3`, `§5.1`, `§13`). References like `A11.5` do not exist; VRAM guidance is actually `§10.4`.  
   **Fix:** run a markdown link/reference check and normalize all section citations.

4. **Data schema lacks fields needed by the design** (`§5.7`, `§6`). Records need `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `forced_cut`, diarization confidence, and effective model/provider metadata.  
   **Fix:** add these to schema v1 now; migrations are harder later.

## Suggestions/Improvements

1. **Cut v1 scope** (`§2.1`, `§12`). Ship capture + storage + STT + consent + recovery first. Make live NLLB, realtime diarization, and pyannote rediarization optional post-MVP gates after benchmark proof.

2. **Add a real benchmark harness** (`§9`, `§13`). Use dual-stream WAV fixtures and report RTF, p95 latency, queue depth, VRAM peak, and dropped frames for 8GB, 12GB, and CPU-degraded presets.

3. **Use progressive UI updates** (`§5.8`, `§5.9`). Show transcript immediately after STT, then patch in translation and speaker label later. This makes latency tolerable even when post-processing is slow.

4. **Add degraded-mode UX** (`§8`, `§10.4`). The app should clearly show “recording audio only”, “STT catching up”, “translation paused”, or “speaker labels offline-only” instead of one generic amber dot.

## Verdict

The spec is not implementation-ready. The core product is plausible, but the current design overcommits on real-time GPU performance, has an invalid timebase model for independent Windows audio streams, overstates crash safety, and under-secures a sensitive local recording API plus agentic summarizer. Resolve the defaults/fallback ladder, audio timeline model, crash recovery format, server-side consent/auth, and v1 scope before writing production code.
