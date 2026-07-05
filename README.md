# ai-record

A **local, crash-safe meeting scribe** for Windows. While you're in an online
meeting, ai-record captures the system audio (WASAPI loopback = "Them") and your
microphone ("You"), segments each stream on voice activity, and transcribes it in
near-real-time with faster-whisper on the GPU — writing every finalized utterance
to durable, crash-safe storage as it goes.

Everything runs on your machine. No audio or transcript leaves the computer.

This repository currently implements **Milestone M1 — Core recorder** of
[`docs/SPEC.md`](docs/SPEC.md) (v2.0). See *Milestones* below for what M2–M4 add.

---

## ⚠️ Legal & consent (read first)

ai-record records your computer's audio output (loopback) plus your microphone.
Because it does **not** use the meeting platform's recording feature, **the
platform shows no recording indicator to other participants.** This is a
technical consequence of loopback capture — the app is listening to your speakers
like any audio app — and **not** a stealth feature.

**Recording other people without their knowledge or consent may be illegal.**
Many jurisdictions have two-party (all-party) consent laws. ai-record is intended
for personal note-taking of meetings you participate in. **You are solely
responsible** for complying with the law and, where required, obtaining consent
from and disclosing the recording to all participants.

On first run the app shows a consent modal you must acknowledge. The server
**enforces** this: `POST /api/capture/start` returns `403` until consent is
acknowledged.

---

## What M1 gives you

- **Dual WASAPI capture** (loopback + mic) behind a backend contract that reports
  the actual opened format, resamples to 16 kHz mono, and emits health telemetry.
- **Crash-safe raw audio**: rolling per-minute WAV segments + a `samples.idx`
  sidecar. A crash/power-loss loses at most ~1 minute of audio.
- **Per-source VAD segmentation** into sample-accurate utterances.
- **STT-first pipeline**: a single GPU faster-whisper worker with hallucination
  guards + an OOM/backpressure fallback ladder. Each transcript is persisted and
  broadcast immediately.
- **Durable session storage** (JSONL schema v2 + incremental `transcript.md`) in
  `%LOCALAPPDATA%\ai-record\sessions\`, with atomic rewrites and a per-session lock.
- **FastAPI server** on `127.0.0.1` with per-launch token auth, Origin allow-list,
  a server-side consent gate, REST control + a live WebSocket (bounded per-client
  queues, `since_seq` catch-up).
- **Desktop UI** (pywebview) with a first-run consent modal, preflight screen, a
  compact bar (Start/Stop, status, latest lines) and an expandable full transcript
  view with search + settings.
- **Incomplete-session recovery** on startup: offline-transcribe the untranscribed
  audio tail of a session that never finalized.

---

## Install

Windows 11, Python 3.12, NVIDIA GPU with a working CUDA `torch` for real STT.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt      # see requirements-notes.md re: torch
```

> Do **not** blindly reinstall `torch`/`faster-whisper` — see
> [`requirements-notes.md`](requirements-notes.md). First run downloads models
> (~4–6 GB); later runs are offline-capable.

## Run

```powershell
python -m ai_record       # or:  python main.py
```

This runs preflight, starts the localhost server on a free port (default 8848),
and opens the frameless always-on-top window at
`http://127.0.0.1:<port>?token=<per-launch-token>`. If pywebview is unavailable
the URL is printed so you can open it in a browser.

Sessions are written to `%LOCALAPPDATA%\ai-record\sessions\`. Settings live in
`%LOCALAPPDATA%\ai-record\settings.json`; secrets (HF token, Gemini key) live in
Windows Credential Manager via `keyring`, never in the JSON.

## Test

Tests run on CPU with **no GPU, no audio hardware, and no model downloads**.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest
```

---

## Milestones (from the spec)

- **M1 — Core recorder** *(this build)*: dual capture, crash-safe WAV, VAD, STT,
  storage, server (token/consent), preflight, live UI, recovery.
- **M2 — Live translation**: NLLB CT2 int8 (CPU) with gating/batching/staleness +
  progressive translation patches + Gemini stub.
- **M3 — Realtime diarization (Tier 1)**: online speaker clustering with confidence,
  "Speaker ?" for unknown/overflow, renameable labels, patch UI.
- **M4 — Offline enrichment**: pyannote Tier-2 re-diarization (sample-time),
  hardened Claude-CLI summarizer, delete/retention UI, expanded-UI polish.

M2–M4 features are left as clean extension points in M1 (a `patch` WS message
type exists; translation/speaker fields are present-but-null; the fallback ladder,
preset stack, and provider interfaces are already wired).

## Layout

```
ai_record/
  __main__.py     entrypoint (preflight → server thread → pywebview)
  config.py       Settings + presets + VRAM detect + keychain Secrets
  preflight.py    CUDA / model-cache / disk readiness report
  audio/          ringbuffer · capture (backend contract) · vad · segmenter
  transcriber.py  faster-whisper wrapper (guards + OOM ladder) + MockTranscriber
  store.py        WavWriter · RawSegmentWriter · SessionStore (schema 2, recovery)
  pipeline.py     capture→segment→STT(emit)→store/broadcast + fallback ladder
  server.py       FastAPI: token/Origin auth, consent gate, REST + WS
  web/            vanilla HTML/CSS/JS UI (no build, no CDN)
tests/            unit + integration (CPU-only)
docs/SPEC.md      the authoritative specification (v2.0)
```
