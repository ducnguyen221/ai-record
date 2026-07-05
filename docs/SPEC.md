# ai-record — Software Specification

**Version:** 2.0 (implementation-ready; revised after adversarial review)
**Target platform:** Windows 11, Python 3.12, NVIDIA GPU with CUDA-enabled PyTorch
**Reference machine:** NVIDIA RTX 4070, 12 GB VRAM (the `gpu_12gb` preset is the default this build is tuned for)
**Status:** Design resolved. v2 integrates the `codex-spec-review-01` adversarial review: hardware presets replace fixed defaults, the timebase is sample-accurate, crash-safety and local-API protection are hardened, the summarizer is sandboxed, and the build is sequenced into gated milestones (M1–M4). An engineer can build v1 directly from this document via the M1→M4 milestone plan (§12). Genuinely hardware-dependent unknowns (real loopback reliability across drivers, real-world RTF on the 4070) are deferred to the benchmark harness (§9), not left as open design questions.

> **Reading note.** All internal cross-references use the `§N` form (e.g. `§5.7`, `§10.4`). The v1 review flagged the file as "mojibaked"; that was a false alarm — the file is valid UTF-8 and the corruption was only a PowerShell console-rendering artifact. No re-encoding is performed. What v2 *does* fix is literal placeholders (`channels=…`, `beam_size=…`, `Speaker …`) and broken references (`A11.5`, `A5.7`), now replaced with concrete values.

---

## 1. Overview

**ai-record** is a *local* meeting-scribe desktop application. While the user is in an online meeting (Microsoft Teams, Zoom, Google Meet, Webex, a browser call, or any app that plays audio through the speakers), ai-record:

1. **Captures** two audio streams simultaneously on Windows:
   - The **system audio mix** via WASAPI *loopback* of the default output device — i.e. everything the speakers play, which includes remote participants. Labelled **"Them"**.
   - The **default microphone** — the local user's own voice. Labelled **"You"**.
2. **Persists raw capture crash-safely** to per-minute WAV segments (with valid headers) plus a sidecar sample index, independently of transcription, so a crash or power loss loses at most the current minute (§5.1).
3. **Segments** each stream independently into utterances using voice-activity detection (VAD), dropping silence.
4. **Transcribes** each finalized utterance to text in near-real-time with faster-whisper on the GPU, detecting the spoken language. The STT result is **emitted, persisted, and broadcast immediately** — it never waits for translation or diarization (§4.5).
5. **Translates** foreign-language utterances to Vietnamese (optional, toggleable) as a lower-priority async pass that **patches** the already-shown utterance, using a local NLLB-200 CTranslate2 int8 model on CPU by default.
6. **Diarizes speakers** in two tiers: a rough real-time online-clustering pass (for the "Them" stream) that patches speaker labels in later, and an accurate offline `pyannote.audio` re-diarization pass on demand after the meeting.
7. **Summarizes** the transcript on demand, post-meeting, by shelling out to a local AI CLI (Claude Code by default) with the transcript treated as **untrusted data** in a hardened sandbox (§5.6).
8. **Persists** every session (transcript, structured records, raw audio per source, summary) to `%LOCALAPPDATA%\ai-record\sessions\`, autosaving each utterance so a crash never loses finalized data, and supports first-class delete + retention.

Everything runs **on the user's machine**. No audio or transcript leaves the computer except when the user explicitly invokes a cloud-based translation or summarization provider (both off/local by default).

The UI is a small **frameless, always-on-top** window (a compact bar by default, expandable to a full transcript view), implemented as a local web app served by FastAPI and wrapped in **pywebview**.

### 1.1 Why loopback capture

ai-record does **not** use any meeting platform's official recording API. It records the operating system's audio output (loopback) plus the microphone. This is the same technique used by mainstream AI note-takers (Otter, tl;dv, Fireflies, Fathom). A direct and honest technical consequence is that **the meeting platform displays no "recording" indicator**, because from the platform's perspective nothing is being recorded — the OS is simply playing audio and ai-record is listening to the speaker output like any other audio app. See the **Legal & Consent** section (§3); this behavior must be framed to the user as a technical consequence, never marketed as an anti-detection or stealth feature.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1, in scope)

All of the following ship in v1. The **build** is sequenced into gated milestones M1–M4 (§12), each independently runnable and benchmark-gated, but the feature scope below is the v1 target.

- Dual-stream WASAPI loopback + microphone capture, resampled to 16 kHz mono, behind a **backend contract** that reports the actually-opened format (§5.1).
- **Crash-safe raw capture** to rolling per-minute WAV segments + sample-index sidecar, with offline recovery on relaunch (§5.1).
- Per-source VAD segmentation into utterances with low latency.
- **Hardware-preset-driven** real-time GPU transcription with language detection and hallucination guards; **STT-first progressive pipeline** (§4.5).
- Live optional translation of foreign speech → Vietnamese (local NLLB CTranslate2 int8 default; Gemini stub pluggable), applied as a late **patch** with a latency budget (§5.4).
- Two-tier speaker diarization: real-time rough (online clustering with confidence + "unknown" labels) + offline accurate (`pyannote.audio`), applied as late patches / relabels.
- On-demand post-meeting AI summarization via pluggable CLI/local providers (Claude CLI default), **hardened against prompt injection** (§5.6).
- Durable session storage in `%LOCALAPPDATA%` with crash-safe autosave, a defined JSONL schema (`schema: 2`), per-session concurrency locking, and first-class delete + retention (§5.7).
- FastAPI backend with WebSocket live push + REST control endpoints, **per-launch token auth + Origin checks + server-side consent gate** (§5.8).
- Compact + expanded frameless always-on-top UI with progressive updates, explicit degraded-mode states, settings, search, renameable speaker labels (§5.9).
- **Preflight/readiness screen** (CUDA, model cache, disk, HF terms, CLI availability, auto-selected preset) (§5.9).
- OS-keychain-backed secrets, JSON-persisted settings with documented keys/defaults, redacted settings API (§5.10, §7).
- **Auto-VRAM-detect + fallback ladder** so the app degrades gracefully rather than falling behind (§4.3, §4.4).
- Graceful error handling for missing devices, GPU OOM, missing CLIs, missing HF token.
- Unit + integration test suites, acceptance criteria, a benchmark harness, and a Windows audio test matrix; documented manual smoke test (§9).

### 2.2 Non-Goals (v1, explicitly out of scope)

- Perfect **overlapping-speech** separation (when two people talk at once). Best-effort only — and overlap regions are explicitly *marked* and *excluded* from centroid updates rather than silently mislabelled (§5.5).
- `.exe` / installer packaging (PyInstaller etc.) — noted as future work.
- Cloud STT (Whisper stays local). Cloud is only optionally used for translation/summarization.
- Non-Windows platforms (macOS/Linux). WASAPI loopback is Windows-specific.
- Mobile / web-hosted / multi-user deployments.
- Speaker *identification* against a named voiceprint database (we only cluster into anonymous, renameable "Speaker N").
- Real-time translation of the user's own outgoing speech beyond the same pipeline treatment (no TTS back-translation).

---

## 3. Legal & Consent (READ FIRST)

> **This section MUST be surfaced in the app** (a first-run modal that the user must acknowledge, plus a permanent link in Settings) **and enforced server-side** (§5.8). The text below is the normative content.

ai-record captures audio by recording your computer's audio output (WASAPI loopback) together with your microphone. Because it does **not** use the meeting platform's recording feature, **the meeting platform will not show a recording indicator to other participants.** This is a technical consequence of loopback capture — the app is listening to your speakers the same way any audio app does — and **not** a feature designed to hide recording from anyone.

**Recording other people without their knowledge or consent may be illegal.** Many jurisdictions have wiretap / eavesdropping / "two-party (all-party) consent" laws (for example, several U.S. states such as California, Florida, and Illinois; and various national laws in the EU and elsewhere) that make it unlawful to record a conversation unless **everyone** being recorded has consented. Rules differ widely by country, state, and context (workplace, private call, public meeting).

**ai-record is intended for personal note-taking of meetings you are a participant in.** You, the user, are solely responsible for complying with the law that applies to you and to the other participants. Where the law requires it, **you must obtain consent from, and/or disclose the recording to, all participants** before recording. When in doubt, ask and disclose; many organizations require an explicit verbal or written notice at the start of a call.

The developers of ai-record provide the software "as is" and are not liable for unlawful use. Using this software to record others without required consent may expose you to civil and/or criminal liability.

**Implementation requirements tied to this section:**
- On first run, show a modal containing the above text with an "I understand and agree" button. Persist acknowledgement in settings (`consent_acknowledged: true`, with `consent_acknowledged_at` timestamp). Do not enable the Start button until acknowledged.
- **Server-side enforcement (normative):** `POST /api/capture/start` MUST return **403** unless `consent_acknowledged` is `true` in settings. The UI gate is a convenience; the server gate is the guarantee. See §5.8.
- Provide a "Legal & Consent" link in the expanded window's Settings that reopens this text at any time.
- Do **not** add any UI copy, tooltips, or marketing that describes the no-indicator behavior as "undetectable", "stealth", "invisible recording", or similar. Describe it factually if at all.

---

## 4. Architecture

### 4.1 High-level model

ai-record is a single Python process that runs:
- A **FastAPI + Uvicorn** server (HTTP + WebSocket) on `127.0.0.1` (localhost only, never `0.0.0.0`), protected by a **per-launch API token** and **Origin allow-listing** (§5.8).
- A **capture/processing pipeline** running in background threads and asyncio tasks.
- A **pywebview** window that loads the local web UI (`http://127.0.0.1:<port>?token=<per-launch-token>`).

The pipeline is a chain of producer/consumer stages connected by bounded queues. Each source ("You", "Them") has its own capture → crash-safe writer + ring buffer → segmenter. Finalized utterances are merged into a single **STT work queue** consumed by the single transcription worker. The transcription worker emits/persists/broadcasts the STT result **immediately**; translation and Tier-1 diarization run as **lower-priority async post-processing** that patch the already-shown utterance (§4.5).

### 4.2 ASCII data-flow diagram

```
                          WINDOWS AUDIO SUBSYSTEM (WASAPI)
        ┌───────────────────────────────┐   ┌──────────────────────────┐
        │ Default OUTPUT device          │   │ Default INPUT (mic)      │
        │ (speakers) — LOOPBACK          │   │                          │
        └───────────────┬───────────────┘   └─────────────┬────────────┘
                        │ raw frames (native SR)          │ raw frames (native SR)
                        ▼                                  ▼
              ┌───────────────────┐              ┌───────────────────┐
              │ capture.py        │              │ capture.py        │
              │ backend contract  │              │ backend contract  │
              │ report SR/ch/fmt  │              │ report SR/ch/fmt  │
              │ → resample 16k    │              │ → resample 16k    │
              │ → mono            │              │ → mono            │
              │ + source-health   │              │ + source-health   │
              └───┬───────────┬───┘              └───┬───────────┬───┘
                  │           │                      │           │
   crash-safe raw │           │ 16k mono float32     │           │ crash-safe raw
   per-min WAV +  ▼           ▼                      ▼           ▼  per-min WAV +
   samples.idx  RingBuffer  (sample counter,     RingBuffer   samples.idx
                "them"       source_epoch_id)      "you"
                  │                                  │
                  ▼                                  ▼
              ┌───────────────────┐              ┌───────────────────┐
              │ segmenter.py      │              │ segmenter.py      │
              │ VAD (Silero)      │              │ VAD (Silero)      │
              │ → utterance chunk │              │ → utterance chunk │
              └─────────┬─────────┘              └─────────┬─────────┘
                        │  Utterance{source, pcm,          │
                        │   audio_start_sample,            │
                        │   audio_end_sample,              │
                        │   source_epoch_id, forced_cut}   │
                        └──────────────┬───────────────────┘
                                       ▼
                             ┌───────────────────┐
                             │  stt_queue        │  (bounded, backpressure→ladder)
                             └─────────┬─────────┘
                                       ▼
                             ┌───────────────────────────────┐
                             │ transcriber.py  (STT worker)  │  faster-whisper (preset)
                             │ text + lang + stt_latency_ms  │  STT ALWAYS preempts
                             └─────────┬─────────────────────┘
                          EMIT+PERSIST+BROADCAST immediately (utterance msg)
                                       │
                                       ▼
                             ┌───────────────────┐
                             │  post_queue       │  (lower priority, CPU-default)
                             └─────────┬─────────┘
                        ┌──────────────┴──────────────┐
                        ▼                              ▼
              ┌───────────────────┐        ┌───────────────────────┐
              │ translator.py     │        │ diarizer.py (T1)      │
              │ NLLB CT2 int8 CPU │        │ Resemblyzer/ECAPA emb │
              │ (staleness skip)  │        │ online cluster + conf │
              └─────────┬─────────┘        └───────────┬───────────┘
                        └──────────────┬───────────────┘
                                       ▼
                        PATCH already-shown utterance (patch msg) + store update

   POST-MEETING (on demand):
     summarizer.py  ── reads transcript TEXT (untrusted, sandboxed) ──► summary.md
     diarizer.py T2 ── reads audio_them.wav (pyannote, SAMPLE time) ──► relabel transcript
```

### 4.3 Hardware presets & VRAM auto-detect

v1 replaced "default `large-v3` fp16 everywhere" with **hardware presets**. Setting `hardware_preset` (§7) selects the whole real-time stack. Default is `auto`, which detects VRAM at startup via:

```python
import torch
if not torch.cuda.is_available():
    preset = "cpu"
else:
    total = torch.cuda.get_device_properties(0).total_memory  # bytes
    gb = total / (1024**3)
    preset = ("gpu_16gb_plus" if gb > 15 else
              "gpu_12gb"      if gb >= 10 else
              "gpu_8gb")
```

The selected preset is shown on the preflight screen (§5.9), persisted in session `meta.json`, and recorded per-utterance as `effective_model` / `effective_compute_type`.

| Preset | Trigger | Whisper | Compute | Beam | Translation (NLLB) | T1 embedder | Realtime diarization |
|--------|---------|---------|---------|------|--------------------|-------------|----------------------|
| `cpu` | no CUDA | `small` | `int8` (CPU) | 1 | CPU int8 | Resemblyzer (CPU) | **off** (offline-only) |
| `gpu_8gb` | `<10 GB` | `medium` | `int8_float16` | 1 | CT2 int8 on **CPU** | Resemblyzer (CPU) | optional (default off) |
| **`gpu_12gb`** *(default; reference RTX 4070)* | `10–15 GB` | `large-v3` | `int8_float16` | 1 fast / 5 quality | CT2 int8 on **CPU** | Resemblyzer (**CPU**); ECAPA-GPU only if user opts in | **on** |
| `gpu_16gb_plus` | `>15 GB` | `large-v3` | `float16` | 5 | CT2 int8 on GPU allowed | ECAPA on GPU allowed | **on** |

Notes:
- On the **default `gpu_12gb` preset**, translation and speaker embeddings run on **CPU** so they never contend with STT for the GPU. STT is the only stage that must stay live; it keeps the GPU to itself.
- `cpu` preset warns heavily on the preflight screen: real-time transcription may not keep up; live translation and realtime diarization are disabled; the app is still useful as a crash-safe recorder with offline catch-up (§5.1) and offline diarization/summarization.
- A user may override individual knobs (`whisper_model`, `whisper_compute_type`, `translation_device`, `diarization_device`, `diarization_embedder`) after a preset is chosen; explicit overrides win over the preset but are validated against available VRAM with a warning.

### 4.4 Fallback ladder (auto-downgrade on backpressure)

`auto_downgrade_on_backpressure = true` **by default**. The ladder triggers its **first step as soon as backlog exceeds a small threshold** — `backlog > backpressure_utt_threshold` (default 2 utterances) **or** oldest queued utterance age `> backpressure_lag_seconds` (default 3 s) — **not** after 8 s. Each step is applied in order; when backlog clears for `recovery_stable_seconds` (default 30 s) the app may step back **up** one rung (hysteresis, never oscillating faster than once per step).

Ordered ladder (top = first, lightest degradation):

1. **beam 5 → beam 1** (quality→fast) for Whisper.
2. **Move NLLB translation to CPU** (if it was on GPU).
3. **Move realtime diarization embedder to CPU**, then **disable realtime diarization** (labels become "Them"/"Speaker ?", offline tier-2 still available).
4. **Whisper → `large-v3` `int8_float16`** (if it was fp16).
5. **Whisper → `medium` `int8_float16`.**
6. **Whisper → `small` `int8`.**
7. **Disable live translation** (STT continues; utterances stored untranslated; user can batch-translate offline later).
8. **Audio-only capture with offline catch-up** — stop feeding the STT queue live, keep capturing crash-safe WAV (§5.1); transcribe the backlog after the meeting via the recovery flow. The UI shows "recording audio only" (§5.9).

Every ladder transition emits a `status` WS message with the new effective config and is logged. The trigger and each step are covered by the acceptance criteria (§9.5).

### 4.5 STT-first progressive pipeline

The transcription worker **emits, persists, and broadcasts the STT result immediately** (a `utterance` WS message + a `transcript.jsonl` append + `transcript.md` line). Translation and Tier-1 diarization then run as **lower-priority async post-processing** on a separate `post_worker`, and their results are delivered as **`patch`** WS messages plus in-place store updates (§5.7). The UI therefore shows *text first*, then fills in the *translation* and *speaker label* moments later.

Rules (normative):
- Post-processing MUST NOT hold a GPU lock that can block STT. On the `gpu_12gb` default, translation + embeddings default to **CPU**, so no GPU lock is involved. If a user forces them onto the GPU (`gpu_16gb_plus`, or explicit override), a **priority GPU scheduler** is used: STT submissions always preempt post-processing, i.e. the `post_worker` acquires the device lock only in short slices and yields whenever the STT worker is waiting.
- Post-processing is **best-effort and skippable**: if the `post_queue` backs up, translation obeys the staleness policy (§5.4) and diarization may downgrade/skip per the ladder (§4.4) — but the STT text is already durable and shown.
- The `patch` message carries the utterance `id`/`seq` and only the changed fields (`translation`, `translation_provider`, `translation_error`, `speaker`, `diarization_confidence`, `diarization_source`).

### 4.6 Concurrency & backpressure model

- **Capture threads (2):** one per source. Each is a dedicated OS thread (audio callbacks / blocking record loops). Each thread does three things per block, in this order: (1) append the resampled PCM to the **crash-safe raw writer** (§5.1) — the durable path; (2) advance the source's **sample counter** and stamp `source_epoch_id`; (3) write frames into the per-source `RingBuffer` for live processing. Capture must never block on downstream live work; if the ring buffer is full it overwrites oldest data and increments a `dropped_frames` counter (logged, surfaced as a degraded state). **Raw capture is never dropped** — only the live ring buffer is lossy, and the crash-safe WAV is the source of truth for offline catch-up.
- **Segmenter threads (2):** one per source. Each pulls frames from its ring buffer, runs VAD, and emits `Utterance` objects (carrying `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `forced_cut`) onto the shared bounded `stt_queue` (`queue.Queue(maxsize=64)`).
- **Transcription worker (1 thread):** the GPU is a single serialized resource for STT. One worker pulls from `stt_queue`, runs faster-whisper, and immediately emits/persists/broadcasts (§4.5). **Backpressure:** governed by the ladder (§4.4), triggered at backlog > 2 utterances or > 3 s — segmenters do *not* silently block for 8 s. When the ladder reaches step 8, the STT queue stops being fed live and the audio is caught up offline.
- **Post worker (1 thread):** drains `post_queue` (`maxsize=64`), runs translation (§5.4) and T1 diarization (§5.5), emits `patch` messages + store updates. Lower priority than STT; never holds a GPU lock that blocks STT (§4.5).
- **Persistence:** `store.py` is append-only for utterances (fast, fsync-throttled) and uses a **per-session read/write lock** for full-file rewrites (rename, re-diarize) which are atomic via temp + `os.replace` in the same directory (§5.7).
- **Bridge (threads ↔ asyncio):** the server holds a reference to the running event loop. Worker threads hand outgoing WS messages to the broadcaster via a thread-safe enqueue. **The `call_soon_threadsafe(put_nowait)` QueueFull hazard is fixed** by giving the broadcaster its own unbounded internal handoff *and* per-client bounded queues (§4.7); the enqueue path never raises `QueueFull` inside an event-loop callback.
- **Ordering:** utterances may finish STT slightly out of wall-clock order across sources. Each utterance carries sample-derived `start`/`end` (see §4.8). The UI orders by `start`. `store.py` appends in completion order (`seq`) to JSONL but each record has authoritative timestamps; the rendered `transcript.md` is sorted by `start` on finalize/close.

### 4.7 WebSocket robustness

- One **broadcaster task** on the event loop owns fan-out. Worker threads enqueue messages via `loop.call_soon_threadsafe(broadcaster.submit, msg)`, where `broadcaster.submit` appends to a plain `collections.deque` (no `QueueFull`). The broadcaster then distributes to each connected client.
- **Per-client bounded outgoing queues** (`ws_client_queue_max`, default 256). Message classes:
  - **Durable** (`utterance`, `patch`, `rename`, `rediarize:done`, `summary:done`): must not be dropped. If a client's queue is full, the client is marked lagging; the client recovers missed durable events by `seq` via the REST catch-up endpoint `GET /api/sessions/{id}/utterances?since_seq=N` (§5.8). If it stays full past `ws_client_slow_deadline_s` (default 10 s) the server closes that slow client (it will reconnect and replay).
  - **Coalescible / droppable** (`status`): only the latest matters; under load the queue keeps just the most recent `status` and drops older ones.
- Every drop/coalesce/slow-client-close is **logged** with counts, and surfaced in `/api/capture/status` as `ws_drops`.

### 4.8 Sample-accurate timebase

The single-offset model of v1 is **replaced**. Wall-clock and a single start delta cannot keep two independent WASAPI devices aligned over a long meeting (clocks drift; device reopen creates discontinuities).

- **Each source owns a sample counter.** From the moment a source's stream opens, every resampled 16 kHz frame advances that source's cumulative sample count. `sample / 16000` is that source's audio time.
- **`source_epoch_id`** starts at 0 for a source and **increments on every device reopen / discontinuity** (device change, driver glitch, exclusive-mode preemption, gap). Within an epoch, sample time is contiguous and gap-free (silence during a gap is *not* written; instead the epoch boundary is recorded). The crash-safe `samples.idx` sidecar (§5.1) records, per epoch, the wall-clock open time and the starting cumulative sample so any sample can be mapped back to wall-clock and vice-versa.
- **Every utterance record stores** `audio_start_sample`, `audio_end_sample` (in that source's WAV sample time), `source_epoch_id`, and `source_offset_sec` (the source's wall-clock offset from session start at that epoch). `start`/`end` (seconds since session start, for UI ordering) are derived from these, not measured with `perf_counter`.
- **Tier-2 relabeling works in `audio_them.wav` sample time.** pyannote spans are in the concatenated `audio_them.wav` sample timeline (which is exactly the "them" source's per-epoch samples written in order). Overlap-majority relabeling compares each "them" utterance's `[audio_start_sample, audio_end_sample]` against pyannote spans **on that sample timeline**, never against wall-clock (§5.5).
- **Drift handling.** Over a long meeting the "you" and "them" sample timelines may diverge from each other and from wall-clock (independent crystals + resampler). This is expected and acceptable: cross-source ordering uses each source's own sample→session-time mapping (piecewise-linear per epoch), and any residual skew only affects *relative interleaving* of the two transcripts, never the *within-source* alignment that tier-2 depends on. The benchmark harness (§9) measures observed drift over a 60-minute fixture.

---

## 5. Components

Each subsection: **Responsibility · Public interface · Dependencies · Key algorithms / details.**

### 5.1 `capture.py` — audio capture (backend contract + crash-safe raw)

**Responsibility.** Open and run two simultaneous WASAPI streams (loopback of default output = "Them"; default microphone = "You") behind a **uniform backend contract**, convert both to 16 kHz mono float32, maintain per-source sample counters + `source_epoch_id`, tee raw audio to the **crash-safe raw writer**, and push frames into per-source ring buffers. Emit per-source health telemetry. Handle device changes, missing devices, and silent loopback.

**Backend contract (both backends implement).**
```python
class OpenedFormat:
    sample_rate: int          # ACTUAL opened rate (e.g. 48000)
    channels: int             # ACTUAL opened channel count (e.g. 2)
    sample_format: str        # "float32" | "int16"  (drives byte decoding)
    device_id: str            # backend-specific stable id of the opened device
    device_name: str
    block_frames: int         # frames per read block
    block_duration_ms: float  # block_frames / sample_rate * 1000

class SourceHealth:
    rms: float                # rolling RMS of the last ~1 s (post-resample)
    silent_frames: int        # cumulative count of near-zero frames
    overrun_count: int        # capture overruns/xruns reported by backend
    underrun_count: int
    reopen_count: int         # == current source_epoch_id
    last_epoch_open_wall: str # ISO time the current epoch opened

class AudioBackend(Protocol):
    def open(self, role: str, settings: "Settings") -> OpenedFormat: ...
    def read(self) -> tuple[np.ndarray, int]:   # (raw pcm block, frames); decodes bytes→float32
    def close(self) -> None: ...
    def current_device_id(self) -> str: ...      # for device-change polling
```

**Public interface.**
```python
class AudioFrame:
    source: str          # "you" | "them"
    pcm: np.ndarray      # float32, mono, 16000 Hz, shape (N,)
    n_samples: int
    audio_start_sample: int   # cumulative sample index (this source, this epoch base applied)
    source_epoch_id: int

class CaptureSource:
    source: str
    available: bool
    opened: OpenedFormat | None
    health: SourceHealth

class CaptureManager:
    def __init__(self, ring_you: RingBuffer, ring_them: RingBuffer,
                 raw_you: RawSegmentWriter | None, raw_them: RawSegmentWriter | None,
                 settings: Settings, on_status): ...
    def start(self) -> list[CaptureSource]:   # returns which sources came up
    def stop(self) -> None
    def sources_status(self) -> list[CaptureSource]
    # callback on_status(source, event, detail) for add/remove/error/silent/reopened
```

**Dependencies.** `soundcard` (preferred) or `PyAudioWPatch` (fallback); `numpy`; `soxr` (streaming resample) with `scipy.signal.resample_poly` fallback; `store.RawSegmentWriter`.

**Key details.**
- **Library selection.** Try `soundcard` first (`audio_backend: "auto"` default; overridable to `"soundcard"` / `"pyaudiowpatch"`).
  - *soundcard path:* obtain the loopback microphone for the default speaker via `soundcard.get_microphone(id=str(default_speaker.name), include_loopback=True)`; the opened recorder reports `samplerate`/`channels`; record with `.recorder(samplerate=native_rate, channels=native_channels, blocksize=1024)`. Capture the **actual** `device.id`/`name` for the contract.
  - *pyaudiowpatch path:* `get_default_wasapi_loopback()` yields the loopback device dict — read `defaultSampleRate`, `maxInputChannels`; open an input stream on it in shared mode. **Decode bytes explicitly:** PyAudio returns raw `bytes`; interpret per `sample_format` — `paInt16` → `np.frombuffer(buf, np.int16).astype(np.float32)/32768.0`, `paFloat32` → `np.frombuffer(buf, np.float32)`. Report the actual format in `OpenedFormat.sample_format`.
- **Two independent streams**, each in its own thread with its own recorder context. Loopback native format is commonly 48 kHz stereo; the mic may be 44.1/48 kHz mono/stereo — the contract reports whatever actually opened.
- **Downmix to mono.** Average channels (`pcm.mean(axis=1)`) if `channels > 1`.
- **Resample to 16 kHz.** `soxr.ResampleStream(in_rate, 16000, num_channels=1, dtype="float32")` per stream (stateful, no block-edge artifacts). Fallback: `scipy.signal.resample_poly` with `up/down = 16000/in_rate` reduced by `gcd`, carrying overlap.
- **Sample counter + epoch.** Each source maintains `cum_samples` (post-resample). On open (initial or reopen) start a new epoch: `source_epoch_id += 1` on reopen; record `(epoch_id, wall_open_iso, cum_samples_at_open)` to the crash-safe sidecar. `AudioFrame.audio_start_sample = cum_samples` before appending the block; then `cum_samples += n`.
- **Crash-safe raw persistence (replaces "tee to one big WAV").** The `RawSegmentWriter` (§5.7) writes **rolling per-minute WAV segments** (`audio_them.000.wav`, `.001.wav`, …) each with a valid header flushed on close, plus a running `samples.idx` sidecar recording, per segment and per epoch, the starting cumulative sample and wall-clock. Segments roll every `raw_segment_seconds` (default 60). A crash/power-loss loses at most the current (open) segment's tail — at most ~1 minute. On `finalize()` the segments are concatenated into the canonical `audio_them.wav` / `audio_you.wav` (still 16 kHz mono PCM16) used by tier-2; the per-minute segments and `samples.idx` are retained until successful concatenation. This is always on while capturing unless `persist_audio: false` (which disables tier-2 and offline recovery).
- **Source-health telemetry.** Maintain `SourceHealth` per source, updated each block: rolling RMS, `silent_frames` (frames with RMS < `silence_rms_eps`, default 1e-4), backend-reported overruns/underruns, `reopen_count == source_epoch_id`. Exposed via `/api/capture/status` and `status` WS messages, and drives the degraded-mode UX (§5.9).
- **Silent-loopback detection.** If the loopback source is *open* (available) but its rolling RMS stays ≈0 for `silent_loopback_warn_s` (default 20 s) **while recording**, emit `on_status("them","silent", …)` → UI warning: *"No audio detected from your speakers — is system audio actually playing? Check the default output device."* Do not stop capture; the meeting audio may genuinely be silent.
- **Device-change handling.** Detect via backend error or a periodic (every 2 s) `current_device_id()` vs the opened id. On change: close the affected stream, **increment `source_epoch_id`**, attempt reopen on the new default (up to `device_reopen_retries`, default 5, 500 ms backoff), emit `on_status(source, "reopened"|"lost")`. Do not touch the other stream. The gap is recorded as an epoch boundary (§4.8); no silence is fabricated into the WAV.
- **Missing-device handling.** If loopback can't open, start mic-only (`them.available=false`); if mic can't open, start loopback-only (`you.available=false`); if **both** fail, `start()` returns `[]`, the server does not enter recording state and returns an actionable error. At least one source is required.
- **No exclusive mode.** Always open shared-mode WASAPI so we never seize the device from the meeting app.

### 5.2 `segmenter.py` — VAD segmentation

**Responsibility.** Convert a continuous 16 kHz mono stream (via its ring buffer) into discrete utterance chunks bounded by natural pauses, dropping silence, keeping latency low. One instance per source. Emit sample-accurate bounds.

**Public interface.**
```python
class Utterance:
    source: str               # "you" | "them"
    pcm: np.ndarray           # float32 16k mono, the utterance audio
    start: float              # seconds since session start (derived from samples)
    end: float
    audio_start_sample: int   # this source's WAV sample time
    audio_end_sample: int
    source_epoch_id: int
    source_offset_sec: float
    forced_cut: bool          # True if cut by max_utterance_seconds, not a natural pause

class Segmenter:
    def __init__(self, source: str, settings: Settings): ...
    def run(self, ring: RingBuffer, out_queue: queue.Queue, stop_event): ...
```

**Dependencies.** `silero-vad` (preferred) or `webrtcvad` (fallback); `numpy`.

**Key algorithm (streaming VAD state machine).**
- VAD on fixed frames (Silero: 512-sample / 32 ms windows @16 kHz; webrtcvad: 10/20/30 ms). Per-frame speech probability (Silero) or boolean (webrtcvad, `vad_aggressiveness` 0–3, default 2).
- State machine per source:
  - **IDLE** → keep a rolling pre-roll buffer (`pre_roll_ms`, default 300 ms).
  - → **SPEECH** when speech sustained for `speech_start_ms` (default 150 ms); prepend pre-roll.
  - In **SPEECH**, append frames; track trailing silence.
  - **End (emit)** when trailing silence exceeds `silence_end_ms` (default 600 ms) **or** the utterance reaches `max_utterance_seconds` (default 15 s → forced cut; set `forced_cut=True`).
  - Discard utterances shorter than `min_speech_ms` (default 250 ms).
- **Forced-cut handling.** When forced, cut at the most recent low-energy frame within the last 500 ms if possible; else cut hard. Set `forced_cut=True` on the emitted utterance (now **persisted**, §5.7). The transcriber re-includes `forced_cut_overlap_ms` of prior audio on the *next* chunk so the forced boundary isn't mid-word without context (§5.3).
- **Sample bounds.** Compute `audio_start_sample`/`audio_end_sample` from the ring buffer's frame sample indices (carried from capture, §4.8), not from a wall clock. `start`/`end` seconds are `sample / 16000` mapped through the epoch base.
- **Silero specifics.** Keep the VAD model on CPU (tiny) unless `vad_device: "cuda"`; reset RNN state between utterances.
- **webrtcvad fallback.** Hangover counter emulates start/stop hysteresis.
- **Two independent instances** ("you", "them") run concurrently, never share state.

### 5.3 `transcriber.py` — speech-to-text (preset-driven, STT-first)

**Responsibility.** Transcribe each finalized `Utterance` using faster-whisper per the active hardware preset; detect language; guard against hallucinations; **emit/persist/broadcast immediately**; record STT latency and effective model.

**Public interface.**
```python
class Transcript:
    source: str
    start: float
    end: float
    text: str
    lang: str            # ISO-639-1
    lang_prob: float
    avg_logprob: float
    no_speech_prob: float
    stt_latency_ms: int          # queue-exit → text-ready
    effective_model: str         # actual model used (may differ from requested after ladder/OOM)
    effective_compute_type: str

class Transcriber:
    def __init__(self, settings: Settings, preset: Preset): ...
    def load(self) -> None            # loads model per preset; may fall back on OOM
    def transcribe(self, utt: Utterance) -> Transcript | None   # None if dropped
    def current_model(self) -> tuple[str, str]   # (model, compute_type)
    def apply_ladder_step(self, step: LadderStep) -> None  # live model/beam swap (§4.4)
```

**Dependencies.** `faster-whisper` (CTranslate2), CUDA torch runtime; `numpy`.

**Key details.**
- **Model from preset (§4.3).** `WhisperModel(model, device=("cuda"|"cpu"), compute_type=compute_type)`. Default preset `gpu_12gb` → `large-v3` `int8_float16`. Loaded once at capture start and reused. `cpu` preset → `small` `int8` on CPU.
- **Per-utterance transcription.** `model.transcribe(pcm, language=(force_language or None), vad_filter=whisper_vad_filter, beam_size=beam, temperature=[0.0, 0.2, 0.4], condition_on_previous_text=False)`.
  - `condition_on_previous_text=False` — prevents cross-utterance hallucination drift.
  - `beam` = **1** in fast mode (default on `gpu_8gb`/`gpu_12gb`) and **5** only in quality mode / `gpu_16gb_plus`. The ladder can force beam 1 live (§4.4).
  - **Forced-cut overlap.** If the previous chunk for this source had `forced_cut=True`, prepend the last `forced_cut_overlap_ms` (default 200 ms) of that chunk's audio to this chunk before transcription so mid-speech cuts keep context. De-duplicate any repeated leading tokens in post. `forced_cut` is persisted; adjacent forced chunks may be stitched before summarization (§5.6).
- **STT-first emission.** The moment segments are joined, the worker returns the `Transcript`; the pipeline (§4.5) **immediately** persists + broadcasts a `utterance` message. Translation/diarization happen later as patches.
- **Hallucination guards.** Drop (return `None`) when ANY: `no_speech_prob > no_speech_threshold` (0.6) AND `avg_logprob < logprob_drop_threshold` (−1.0); text matches the `hallucination_denylist` (default includes `["thank you", "thanks for watching", "please subscribe", "ご視聴ありがとうございました", "字幕"]`, user-extendable); text empty/punctuation-only; utterance RMS below `min_rms` (0.005).
- **GPU OOM fallback.** try/except CUDA OOM: `torch.cuda.empty_cache()` → step down the ladder's model rungs (`large-v3 int8_float16` → `medium int8_float16` → `small int8`) → last resort CPU. Emit a `status` note; update `effective_model`/`effective_compute_type` (persisted per-utterance and in meta). Never crash.
- **Threading.** Single STT worker; GPU serialized for STT; STT preempts post-processing (§4.5).

### 5.4 `translator.py` — live translation (late patch, latency budget)

**Responsibility.** Translate foreign utterance text into Vietnamese as a **post-STT patch**, within a latency budget, skipping stale work. Pluggable providers.

**Public interface.**
```python
class Translator(Protocol):
    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str: ...
    def translate_batch(self, texts: list[str], src_lang: str, tgt_lang: str="vi") -> list[str]: ...
    def available(self) -> bool: ...

class NllbTranslator(Translator):   # default, CTranslate2 int8
    def __init__(self, settings: Settings, preset: Preset): ...

class GeminiTranslator(Translator): # stub, off by default
    def __init__(self, settings: Settings): ...
```

**Dependencies.** Default: `ctranslate2` NLLB-200 distilled-600M (int8) + `transformers`/`sentencepiece` tokenizer. Gemini stub: `google-generativeai` (optional).

**When to translate.** Only if ALL: `translate_enabled` true; detected `lang != target_lang` (default `vi`); `lang ∈ source_languages` (empty = any non-target). Runs on the `post_worker` **after** the STT text is already shown, then emits a `patch`.

**Latency budget & staleness.**
- **CTranslate2 int8 by default**, on **CPU** on the `gpu_12gb`/`gpu_8gb` presets (keeps GPU for STT); `num_beams` default 2 (down from 3) for speed. GPU allowed only on `gpu_16gb_plus`.
- **Language-detection gating.** Whisper lang detection on very short utterances is unreliable. Require `duration >= translate_min_duration_s` (default 1.0 s) **and** `lang_prob >= translate_min_lang_prob` (default 0.6) before trusting `lang` for the translate decision. Below that, **defer**: translate only if a later signal confirms, else leave `translation=null` (no wrong-language translation).
- **Batching.** The `post_worker` may coalesce **adjacent same-source, same-`lang`, same-`speaker`** short utterances (each `< translate_batch_max_s`, default 4 s) within a `translate_batch_window_ms` (default 400 ms) into one `translate_batch` call, then patch each utterance with its slice.
- **Staleness skip.** If an utterance waiting for translation is older than `translation_max_staleness_s` (default 8 s) — i.e. the translator is falling behind — **skip** it (mark `translation=null`, `translation_error=false`, and a `stale_skipped=true` note in the patch) rather than falling further behind. The user can batch-translate skipped utterances offline post-meeting.

**NLLB details.**
- Model `facebook/nllb-200-distilled-600M` (CTranslate2 converted, int8). Whisper ISO-639-1 → NLLB FLORES map (ship at least): `ja→jpn_Jpan`, `vi→vie_Latn`, `en→eng_Latn`, `zh→zho_Hans`, `ko→kor_Hang`, `fr→fra_Latn`, `de→deu_Latn`, `es→spa_Latn`, `ru→rus_Cyrl`, `th→tha_Thai`. Target `vi→vie_Latn`. Unmapped `lang` → skip (pass through), log once.
- Set source language on the tokenizer / CT2 request; force target BOS to the NLLB target token; `max_length` scaled to input length.
- **Quality note (code comment + docs):** NLLB JA→VI is *moderate*; for higher quality the user can switch `translation_provider` to `gemini` (sends text to Google — a deliberate quality/privacy tradeoff in Settings).

**GeminiTranslator (stub).** Same interface. `available()` true only if `gemini_api_key` is set (from keychain, §5.10). Terse prompt ("Translate the following {src} text to Vietnamese. Output only the translation."). Off by default; if selected without a key, `available()` false → UI prompts to set the key, falls back to NLLB / no translation.

**Error handling.** Any translation exception → `translation=null` + `translation_error=true` in the patch; UI shows the original with a small "translation failed" hint. Never return the source text as if it were a translation. Never block STT or the pipeline.

### 5.5 `diarizer.py` — speaker diarization (two tiers, confidence-aware)

**Responsibility.** Assign speaker labels. Tier 1 real-time (patch, rough, confidence-scored). Tier 2 offline (accurate, sample-time).

**Public interface.**
```python
class Assignment:
    speaker: str                 # "You" | "Speaker N" | "Speaker ?" (unknown)
    confidence: float | None     # cosine-margin-based, None for mic/unknown
    is_overlap: bool
    forced_overflow: bool        # hit max_speakers → labelled "Speaker ?"

class RealtimeDiarizer:   # Tier 1
    def __init__(self, settings: Settings, preset: Preset): ...
    def label(self, utt: Utterance) -> Assignment
    def reset(self) -> None
    def rename(self, old_label: str, new_label: str) -> None

class OfflineDiarizer:    # Tier 2
    def __init__(self, settings: Settings): ...
    def rediarize(self, session_dir: str) -> list[SpeakerSpan]
```

**Tier 1 — real-time online clustering (runs on `post_worker`, patches labels in).**
- **"You" (mic):** always `"You"`, `confidence=None`.
- **"Them" (loopback):** per utterance, embed and assign online.
  - **Embedder from preset (§4.3).** Default on `gpu_12gb` is **Resemblyzer on CPU** (light, non-blocking to STT). ECAPA-TDNN (`speechbrain/spkrec-ecapa-voxceleb`) on GPU only if the user opts in (`diarization_embedder: "ecapa"` + `diarization_device: "cuda"`), permitted by default only on `gpu_16gb_plus`.
  - **Model-specific thresholds (replaces single 0.70).** `sim_threshold_ecapa` (default 0.75) and `sim_threshold_resemblyzer` (default 0.70); the active threshold is chosen by the embedder in use.
  - **Confidence.** `confidence = clamp((max_sim − second_best_sim) / margin_scale, 0, 1)` (a margin score), or a calibrated distance for a single centroid. Persisted as `diarization_confidence`.
  - **Algorithm.** Maintain centroids `{label: (mean_embedding, accumulated_speech_sec, count)}`.
    1. If utterance `duration < min_embed_ms` (default 800 ms) **or** `is_overlap` (see below): label `"Speaker ?"`, `confidence=low`; **do not** create or update any centroid.
    2. Compute L2-normalized embedding `e`. Nearest centroid by cosine similarity.
    3. If `max_sim >= active_threshold`: assign that label with computed confidence. **Only update the centroid** (running mean, capped) **if** `confidence >= centroid_update_min_conf` (default 0.6) AND not overlap AND `duration >= min_embed_ms`. Otherwise assign the label but leave the centroid unchanged (avoids drift after a shaky assignment).
    4. Else create `"Speaker {n+1}"` with `e` as centroid — but a centroid is only **trusted** (used as a match target) once its `accumulated_speech_sec >= min_speaker_speech_s` (default 3 s); until then it can still match but is flagged low-confidence.
  - **Overlap flag.** A cheap proxy for realtime: mark `is_overlap=true` when the utterance's energy envelope shows sustained two-talker characteristics (or when the segmenter reports concurrent speech on both sources with time overlap). Overlap utterances get `"Speaker ?"` and never update centroids.
  - **`max_speakers` overflow (replaces silent nearest-force).** Cap at `max_speakers` (default 8). Beyond it, label `"Speaker ?"` with `forced_overflow=true` and emit a `status` warning — **never** silently force to nearest.
  - **Renaming.** `rename()` updates the label map; new matches keep the new name; renames propagate to emitted records via a store update (§5.7) + a `rename` WS event.
- **Toggle / cost.** `diarization_enabled` (default ON) + `diarization_realtime` (default ON, but the ladder or `cpu` preset can turn it off). When off, "Them" utterances are labelled `"Them"`. The ladder (§4.4) moves the embedder to CPU then disables it before touching STT.

**Tier 2 — offline accurate re-diarization (sample-time).**
- Triggered by the UI "Re-diarize" button → REST → `OfflineDiarizer.rediarize(session_dir)`. **Disallowed during active capture** (returns 409) or run against a finalized snapshot (§5.7).
- **Requires** persisted audio; runs on `audio_them.wav` (remote speakers). "You" stays "You".
- **Model:** `pyannote/speaker-diarization-3.1` via `pyannote.audio`, `Pipeline.from_pretrained(..., use_auth_token=hf_token)`, `.to(torch.device("cuda"))` when available. `hf_token` from keychain (§5.10). Missing token → tier-2 disabled, button shows a link to accept model terms (§8).
- **Relabelling in sample time (§4.8).** pyannote returns speaker-homogeneous spans on the `audio_them.wav` timeline. For each "them" utterance, compute overlap-weighted majority pyannote speaker over `[audio_start_sample, audio_end_sample]` **converted to that same sample timeline**, and assign a stable label (`"Speaker A/B/…"`). **Where pyannote reports overlapping speakers** over the span, preserve multiple candidates (store primary + `speaker_alt`) and mark `is_overlap`. Set `diarization_source="offline"`. Rewrite `transcript.jsonl` atomically (backup `transcript.jsonl.pre-rediarize`), re-render `transcript.md`.
- Long-running: background thread; progress/completion via WS `rediarize` + REST status.

### 5.6 `summarizer.py` — AI summarization (hardened, untrusted input)

**Responsibility.** Produce structured Markdown notes from the transcript **text** (not audio), on demand, post-meeting, treating the transcript as **untrusted** (it may contain spoken prompt-injection).

**Public interface.**
```python
class Summarizer(Protocol):
    def summarize(self, transcript_text: str, meta: dict) -> str: ...  # returns markdown
    def available(self) -> tuple[bool, str]: ...

class ClaudeCliSummarizer(Summarizer):   # default
class CodexCliSummarizer(Summarizer):
class GeminiSummarizer(Summarizer):
class OllamaSummarizer(Summarizer):
```

**Selection.** `summarizer_provider`: `"claude_cli" | "codex_cli" | "gemini" | "ollama"`, default `"claude_cli"` (per user request).

**Input assembly.** Plain-text transcript from `transcript.jsonl`, one line per utterance: `"[mm:ss] <SpeakerLabel>: <text>"`, using Vietnamese translation where present (`summary_use_translation`, default true) else original. Short header (title/date/duration/languages). Adjacent `forced_cut` chunks from the same speaker may be stitched into one line. If very long, chunk to `summary_max_chars` (default 48000) map-reduce.

**Prompt hardening (normative).** The transcript is UNTRUSTED DATA:
- Build a message with a **system instruction** stating that everything between the delimiters is a *meeting transcript to be summarized*, is **data not instructions**, and that any instructions found inside it must be ignored, not executed.
- Wrap the transcript in explicit, hard-to-forge delimiters, e.g. `<<<AI_RECORD_TRANSCRIPT_BEGIN>>>` … `<<<AI_RECORD_TRANSCRIPT_END>>>`.
- **Never** pass the transcript (or prompt) as a shell argument; **never** `shell=True`. Pass prompt **+ transcript via STDIN only**.
- Run the CLI with a **restricted / no-tools** flag where available:
  - **Claude CLI (default):** non-interactive print mode reading from stdin, with tool permissions denied — e.g. `claude -p --permission-mode=deny --allowedTools "" < stdin` (use the currently-supported no-tools flags; the intent is *print mode, no tool/file/network actions*). Verify flags at build time against the installed CLI.
  - **Codex CLI:** `codex exec` in a **read-only sandbox** (e.g. `--sandbox read-only`, no approvals/no network).
- `subprocess.run([...], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=summary_timeout_s, cwd=<isolated temp dir>, creationflags=subprocess.CREATE_NO_WINDOW, env=<minimal env>)`. Isolated cwd so any accidental file write lands in a throwaway dir.
- **Residual risk (documented):** even sandboxed, an agentic CLI processing attacker-controlled text carries residual prompt-injection risk (at worst a misleading summary; with tools truly disabled, no side effects). The **safest** option for untrusted transcripts is a **non-agentic** provider — **Gemini** (API, no local tools) or **Ollama** (local, no tools). These are one setting away. Claude CLI remains the **default per user preference**, run in the hardened no-tools mode above; the Settings help text states this tradeoff plainly.

**Provider implementations.**
- **ClaudeCliSummarizer (default):** as above (stdin, no-tools print mode, isolated cwd, `CREATE_NO_WINDOW`).
- **CodexCliSummarizer:** `codex exec` read-only sandbox, prompt+transcript via stdin.
- **GeminiSummarizer:** Gemini API (needs `gemini_api_key`); no local tools — recommended for untrusted transcripts.
- **OllamaSummarizer:** POST `http://localhost:11434/api/generate`, model `ollama_model` (default `llama3.1`); local, no tools.

**Availability & output.** `available()` probes CLI presence (`shutil.which`) / key / Ollama reachability; if unavailable, `summarize()` isn't called and the UI shows a clear notice naming an alternative provider. On success, save to `<session_dir>/summary.md` (keep `.bak`), return over REST/WS, record `summary_provider` + `summarized_at` in meta.

### 5.7 `store.py` — session storage (schema 2, concurrency, delete/retention)

**Responsibility.** Durable, crash-safe persistence; autosave every finalized utterance; **in-place patch updates** for late translation/diarization; per-session locking; atomic rewrites; delete + retention; crash-safe raw segments; read APIs.

**Public interface.**
```python
class WavWriter:
    def __init__(self, path: str, samplerate=16000, channels=1): ...
    def write(self, pcm: np.ndarray) -> None
    def close(self) -> None

class RawSegmentWriter:                     # crash-safe rolling per-minute segments
    def __init__(self, session_dir: str, source: str, seconds=60): ...
    def write(self, pcm: np.ndarray, cum_sample: int, epoch_id: int) -> None
    def mark_epoch(self, epoch_id: int, wall_iso: str, cum_sample: int) -> None
    def close_and_concat(self) -> str        # → canonical audio_<source>.wav

class SessionStore:
    def __init__(self, sessions_root: str): ...
    def create(self, title: str) -> Session
    def append_utterance(self, rec: UtteranceRecord) -> None      # autosave (jsonl + md)
    def patch_utterance(self, session_id, seq, fields: dict) -> None  # late translation/speaker
    def utterances_since(self, session_id, since_seq: int) -> list[UtteranceRecord]  # WS catch-up
    def rename_speaker(self, session_id, old, new) -> int
    def write_summary(self, session_id, markdown: str) -> None
    def rewrite_after_rediarize(self, session_id, new_labels) -> None
    def list_sessions(self) -> list[SessionMeta]
    def load_session(self, session_id) -> SessionData
    def delete_session(self, session_id) -> None
    def delete_audio_only(self, session_id) -> None               # keep transcript, drop WAVs
    def apply_retention(self) -> int                              # prune per retention_days
    def finalize(self, session_id) -> None                        # sort md, concat raw, write meta
    def detect_incomplete(self) -> list[SessionMeta]              # no ended_at → recovery
    def recover_offline(self, session_id, transcriber) -> int     # transcribe untranscribed tail
```

**Concurrency.** A **per-session `RWLock`**. `append_utterance`/`patch_utterance` take the write lock briefly; full-file rewrites (`rename_speaker`, `rewrite_after_rediarize`) take the write lock and write to a temp file then `os.replace` **in the same directory** (atomic). `load_session`/reads take the read lock. **Tier-2 re-diarize and rename are disallowed during active capture** (server returns 409) — or operate on a finalized snapshot. Summarize reads under the read lock.

**Session folder layout** (`%LOCALAPPDATA%\ai-record\sessions\` by default — §5.10/§11):
```
sessions/
  20260705-142530-standup/
    meta.json
    transcript.jsonl                 # authoritative, append-only (+ in-place patch by seq)
    transcript.md                    # rendered, sorted by start
    summary.md                       # on demand
    audio_you.000.wav ...            # crash-safe per-minute raw segments (during capture)
    audio_them.000.wav ...
    samples.idx                      # per-segment/epoch: start cum_sample + wall time
    audio_you.wav / audio_them.wav   # canonical, produced on finalize (concat of segments)
    transcript.jsonl.pre-rediarize   # backup (tier-2)
    summary.md.bak                   # backup (re-summarize)
```
- `session_id` = `<YYYYMMDD-HHMMSS>-<slug(title)>`; slug lowercased, non-alnum→`-`, ≤40 chars; default title "meeting".

**Autosave & crash safety.**
- `append_utterance`: append one line to `transcript.jsonl` (`"a"`, UTF-8), `flush()` + throttled `os.fsync` (`fsync_interval_ms`, default 1000 ms). Append a rendered `transcript.md` line (completion order); sorted clean `transcript.md` regenerated on `finalize()`.
- `patch_utterance(seq, fields)`: updates the record's fields in `transcript.jsonl`. Implemented as **atomic rewrite** (temp + `os.replace`) under the write lock, OR, for speed on hot paths, an append-only patch-log (`patches.jsonl`) reconciled on read/finalize — implementation may choose, but the on-read view MUST reflect the latest field values. The `transcript.md` line for that utterance is re-rendered.
- **Raw crash-safe path (§5.1).** `RawSegmentWriter` rolls per-minute WAVs with valid headers; `samples.idx` records epoch/segment sample bases. A crash loses at most the current minute of raw audio and at most ~1 s of finalized-utterance JSONL.
- **Incomplete-session detection & recovery.** On app start, `detect_incomplete()` finds sessions with no `ended_at` in `meta.json`. The UI offers **offline recovery**: concat the raw segments, find the last transcribed `audio_end_sample` per source, and transcribe the **untranscribed tail** (`recover_offline`) using the current transcriber, appending the recovered utterances. Then finalize.

**JSONL utterance record schema (`schema: 2`).** One object per line:
```json
{
  "id": "u_000123",
  "session_id": "20260705-142530-standup",
  "seq": 123,
  "source": "them",
  "speaker": "Speaker 2",
  "speaker_alt": null,
  "start": 842.31,
  "end": 846.07,
  "duration": 3.76,
  "audio_start_sample": 13476960,
  "audio_end_sample": 13537120,
  "source_epoch_id": 0,
  "source_offset_sec": 0.0,
  "forced_cut": false,
  "text": "会議を始めましょう。",
  "lang": "ja",
  "lang_prob": 0.98,
  "translation": "Hãy bắt đầu cuộc họp.",
  "translation_provider": "nllb",
  "translation_error": false,
  "stale_skipped": false,
  "no_speech_prob": 0.04,
  "avg_logprob": -0.31,
  "diarization_source": "realtime",
  "diarization_confidence": 0.82,
  "is_overlap": false,
  "forced_overflow": false,
  "effective_model": "large-v3",
  "effective_compute_type": "int8_float16",
  "stt_latency_ms": 640,
  "created_at": "2026-07-05T14:39:12.031+07:00",
  "schema": 2
}
```
- **New in schema 2** (vs schema 1): `speaker_alt`, `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `source_offset_sec`, `forced_cut`, `translation_error`(bool now explicit), `stale_skipped`, `diarization_confidence`, `is_overlap`, `forced_overflow`, `effective_model`, `effective_compute_type`, `stt_latency_ms`. (`diarization_source` and `translation_provider` already existed.)
- **Migration from schema 1.** A reader that encounters `"schema": 1` records fills the new fields with safe defaults: sample fields `null`, `source_epoch_id: 0`, `source_offset_sec: 0.0`, `forced_cut/is_overlap/forced_overflow/stale_skipped: false`, `diarization_confidence: null`, `effective_model/compute_type` from `meta.json`, `stt_latency_ms: null`, `speaker_alt: null`. Records are up-converted lazily on next rewrite. `load_session` tolerates mixed-schema files.

**`meta.json` schema (`schema: 2`).**
```json
{
  "session_id": "20260705-142530-standup",
  "title": "standup",
  "created_at": "2026-07-05T14:25:30+07:00",
  "ended_at": "2026-07-05T15:02:11+07:00",
  "duration_sec": 2201,
  "sources": {"you": true, "them": true},
  "hardware_preset": "gpu_12gb",
  "whisper_model": "large-v3",
  "compute_type": "int8_float16",
  "translate_enabled": true,
  "target_lang": "vi",
  "source_languages": ["ja", "en"],
  "translation_provider": "nllb",
  "diarization_enabled": true,
  "diarization_realtime": true,
  "speakers": {"Speaker 1": "Tanaka-san", "Speaker 2": "Suzuki"},
  "summary_provider": "claude_cli",
  "summarized_at": null,
  "rediarized_at": null,
  "recovered": false,
  "app_version": "2.0",
  "schema": 2
}
```
Note: `ended_at` is written only on clean finalize; its absence is the incomplete-session signal for recovery.

**`transcript.md` rendering.** Sorted by `start`:
```
**[14:39:12] Speaker 2 (ja):** 会議を始めましょう。
> Hãy bắt đầu cuộc họp.
```
(Translation line only when present.)

**Renames.** `rename_speaker` rewrites all matching `speaker` fields atomically (temp + `os.replace`), updates `meta.json.speakers`, re-renders `transcript.md`, returns count.

**Delete & retention.** `delete_session` removes the folder (after a confirmation UI, §5.9). `delete_audio_only` removes WAVs + raw segments but keeps transcript/summary and sets `sources`→audio-deleted markers. `retention_days` (default **0 = keep forever**); when > 0, `apply_retention()` runs on startup and prunes sessions older than N days (also confirmed/announced in UI). Deletion is a first-class privacy feature.

### 5.8 `server.py` — FastAPI backend (token auth, Origin, consent, catch-up)

**Responsibility.** Host the HTTP API + WebSocket, own the pipeline lifecycle, serve the UI, bridge worker threads to the loop, enforce auth + consent, expose secret + delete endpoints.

**Dependencies.** `fastapi`, `uvicorn`, `pydantic`, the components above, `config.Settings`, `keyring`.

**Server config & protection.**
- Bind `127.0.0.1` only, default port `8848` (configurable `server_port`; auto-bump up to 10 ports; report chosen port to pywebview).
- **Per-launch API token.** On startup generate `token = secrets.token_urlsafe(32)`. It is injected into the pywebview URL (`http://127.0.0.1:<port>?token=<token>`). **Every** REST call and the WebSocket handshake MUST present the token (header `X-AI-Record-Token`, or `?token=` query for the WS/URL). Missing/wrong token → **401**.
- **Origin allow-listing.** Reject any request whose `Origin`/`Referer` header is present and not in the allow-list (the pywebview origin / `http://127.0.0.1:<port>` / `http://localhost:<port>`). This blocks browser-based CSRF from other local pages. Requests with no Origin (native pywebview, curl-from-owner) are allowed only with a valid token.
- **Server-side consent gate.** `POST /api/capture/start` returns **403** unless `settings.consent_acknowledged` is true (§3).

**REST endpoints.**
| Method | Path | Body / Params | Returns |
|-------|------|---------------|---------|
| `POST` | `/api/capture/start` | `{title?}` | `{session_id, sources}` — **403 if consent not acknowledged**; errors if both sources fail |
| `POST` | `/api/capture/stop` | — | `{session_id, finalized: true}` |
| `GET` | `/api/capture/status` | — | `{recording, session_id, sources(+health), preset, effective_model, ladder_step, degraded_states, dropped_frames, ws_drops}` |
| `GET` | `/api/preflight` | — | `{cuda, cuda_version, whisper_loadable, model_cache, disk_free_gb, hf_terms_ok, cli_available, preset}` |
| `GET` | `/api/sessions` | — | `[SessionMeta]` (newest first) |
| `GET` | `/api/sessions/{id}` | — | meta + utterances (+ summary if present) |
| `GET` | `/api/sessions/{id}/utterances` | `?since_seq=N` | `[UtteranceRecord]` (WS catch-up / replay) |
| `POST` | `/api/sessions/{id}/summarize` | `{provider?}` | `{markdown}` or `{error}` |
| `POST` | `/api/sessions/{id}/rediarize` | — | `{status:"started"}` — **409 during active capture** |
| `POST` | `/api/sessions/{id}/speakers/rename` | `{old, new}` | `{updated}` — **409 during active capture** unless snapshot |
| `DELETE` | `/api/sessions/{id}` | — | `{deleted: true}` (confirmation enforced client-side) |
| `DELETE` | `/api/sessions/{id}/audio` | — | `{audio_deleted: true}` (keep transcript) |
| `POST` | `/api/sessions/{id}/recover` | — | `{recovered_utterances: n}` (offline catch-up) |
| `GET` | `/api/settings` | — | **REDACTED** settings (secrets as booleans `*_is_set`, never values) |
| `PUT` | `/api/settings` | partial | updated (redacted) settings (validated) |
| `POST` | `/api/secrets/{name}` | `{value}` | `{ok}` — write-only; stores in keychain; `name ∈ {hf_token, gemini_api_key}` |
| `DELETE` | `/api/secrets/{name}` | — | `{ok}` — clears the secret from keychain |
| `GET` | `/api/health` | — | `{ok, gpu, cuda, models_loaded}` |
| `GET` | `/` and `/static/*` | — | serves `web/` UI |

**WebSocket** `GET /ws?token=…`:
- Server → client message types:
  - `{"type":"utterance","record":<UtteranceRecord>}` — STT-first, shown immediately.
  - `{"type":"patch","seq":N,"fields":{…}}` — late translation/speaker/confidence updates (§4.5).
  - `{"type":"status", "recording":bool, "sources":{…health…}, "preset":str, "effective_model":str, "ladder_step":int, "degraded_states":[…], "note":str}` — coalescible.
  - `{"type":"rename","old":str,"new":str}`.
  - `{"type":"rediarize","state":"started|progress|done|error","detail":…}`.
  - `{"type":"summary","state":"started|done|error","markdown"?:str,"error"?:str}`.
  - `{"type":"error","code":str,"message":str}`.
- **Robustness (§4.7):** per-client bounded queues; durable messages (`utterance`/`patch`/`rename`/…:done) are never silently dropped — a lagging client is closed and recovers via `GET /api/sessions/{id}/utterances?since_seq=N`. `status` messages coalesce under load. Drops logged and exposed as `ws_drops`.
- On connect (valid token), server sends current status + the last N utterances of the active session, and the client may call the catch-up endpoint to fill any gap by `seq`.

**Lifecycle.** `start`: verify consent (else 403) → build ring buffers, raw segment writers, segmenters, transcriber (load per preset), translator/diarizer (lazy, CPU-default per preset) → start capture + STT worker + post worker → `store.create`. `stop`: signal `stop_event`, join threads (timeout), flush/close raw writers + concat to canonical WAVs, `finalize()`. Pipeline held on an app-state singleton. Server stores `self.loop = asyncio.get_running_loop()` for the bridge (§4.6/§4.7).

### 5.9 `web/` — front-end UI (progressive, degraded-mode, preflight)

**Responsibility.** Compact bar + expanded window; connect to `/ws` (with token); call REST (with token header); render live transcript with **progressive patching**; show explicit **degraded-mode states**; a **preflight/readiness** screen; settings; actions; delete/retention confirmations.

**Tech.** Dependency-light single-page app in vanilla JS + modern CSS (or a tiny locally-bundled framework — **no CDN**). No build step required for v1. Two views (compact ↔ expanded) toggle within one pywebview window (resize, not a second window). The token is read from `window.location.search` and attached to every REST call (`X-AI-Record-Token`) and the WS URL.

**pywebview host.** Frameless (`frameless=True`), always-on-top (`on_top=True`), compact default ≈ 460×160; `resizable=True`; custom drag region (`easy_drag`/draggable header); explicit close/minimize controls; Expand resizes to ≈ 900×640 and switches to the expanded layout.

**Preflight / readiness screen** (first run, and before the first record of a session). Calls `GET /api/preflight` and shows pass/warn/fail for: CUDA available + version; faster-whisper model loads; model cache present + estimated download size + **free disk space**; HF terms/token for tier-2; summarizer CLI availability; and the **auto-selected `hardware_preset`** with an explanation. The user proceeds, or fixes issues (install a CLI, free disk, add a token). On the `cpu` preset it warns clearly that real-time features are limited.

**Progressive rendering (§4.5).** On an `utterance` message, render the text row **immediately** (translation + speaker shown as pending placeholders). On a `patch` message for that `seq`, fill in the translation line and/or replace the speaker label / confidence in place — no reflow jank.

**Compact bar (default).**
- **Start/Stop** button (red "● Stop" while recording). Start is disabled until consent acknowledged (and the server also enforces it, §3/§5.8).
- **Status area** — explicit degraded-mode chips (not a single amber dot): normal/OK (green "recording"), **"recording audio only"** (ladder step 8 / STT paused), **"STT catching up"** (backlog), **"translation paused"** (translation disabled/stale), **"speaker labels offline-only"** (realtime diarization off), plus one-source-only and error states. Tooltip gives detail.
- The **2–3 most recent** transcript lines (text first; muted Vietnamese line appears when the patch arrives).
- **"Dịch"** translate toggle. **Expand** button (⤢).

**Expanded window.**
- **Header:** editable title, recording controls, degraded-state chips, search box, expand/collapse, settings gear.
- **Transcript pane:** full scrolling list; each row: timestamp · speaker (click to rename inline; shows `?` for unknown/low-confidence, with confidence on hover) · original · translation (muted, below, filled by patch). Auto-scroll unless scrolled up ("jump to latest" pill). You vs Them visually distinguished.
- **Search box:** filters/highlights by substring across original + translation + speaker.
- **Settings panel** (bound to `/api/settings`, which is **redacted**):
  - `hardware_preset` (auto / cpu / gpu_8gb / gpu_12gb / gpu_16gb_plus) with the detected VRAM shown.
  - Whisper model, compute type, latency mode.
  - Translate on/off; source-language chips; translation provider; translation device.
  - Summarizer provider (with the untrusted-input/prompt-injection note and the "safest = Gemini/Ollama" hint).
  - Diarization on/off; realtime on/off; embedder; device; thresholds.
  - **Secrets:** HF token and Gemini key shown as **"is set / not set"** with **Set** / **Clear** buttons that call the write-only `POST/DELETE /api/secrets/{name}` — the values are **never** fetched back.
  - `retention_days` with a confirmation note.
  - Legal & Consent link (reopens §3).
- **Summarize button**, **Re-diarize button** (disabled during capture), **Sessions list** (open past sessions; **Delete** and **Delete audio only** with explicit confirmation dialogs), **Recover** action for incomplete sessions, inline speaker rename.

**Aesthetic.** Clean, modern, minimal — **not "AI slop."** One restrained accent; system font stack (Segoe UI / locally-bundled Inter); generous spacing; muted secondary text; subtle borders; no gradient-spam, no emoji-spam, no purple-glow. Light/dark via `prefers-color-scheme`. No layout jank on new utterances or patches (cap/virtualize DOM nodes for long transcripts).

### 5.10 `config.py` — settings & secrets

**Responsibility.** Define, load, validate, persist settings as JSON; keep **secrets in the OS keychain**, not in the JSON; expose redaction. See §7.

**Public interface.**
```python
class Settings(BaseModel):     # pydantic
    ...  # all non-secret fields in §7 with defaults + validators
    @classmethod
    def load(cls, path: str) -> "Settings"
    def save(self, path: str) -> None            # owner-only ACL on the file
    def update(self, partial: dict) -> "Settings"
    def redacted(self) -> dict                   # secrets → *_is_set booleans, never values

class Secrets:                                   # keyring-backed, never in JSON
    def get(self, name: str) -> str | None       # name ∈ {hf_token, gemini_api_key}
    def set(self, name: str, value: str) -> None # keyring.set_password("ai-record", name, value)
    def clear(self, name: str) -> None
    def is_set(self, name: str) -> bool
```
- **File location:** `%LOCALAPPDATA%\ai-record\settings.json` (via `os.getenv("LOCALAPPDATA")`); created with defaults if absent. Unknown keys ignored with a warning; invalid values rejected (422). Set an **owner-only ACL** on `settings.json` as defense-in-depth.
- **Secrets:** `hf_token`, `gemini_api_key` live in **Windows Credential Manager via `keyring`** (service `"ai-record"`), NOT in the JSON. `GET /api/settings` returns `redacted()` (secrets as `hf_token_is_set: bool`, `gemini_api_key_is_set: bool`). Secrets are written only via `POST /api/secrets/{name}` and cleared via `DELETE`. Never log secret values.

---

## 6. Data Model

Covered inline in §5.7 (JSONL utterance schema **v2**, `meta.json` v2, session folder layout with crash-safe raw segments + `samples.idx`, `transcript.md`). Persisted artifacts per session: `meta.json`, `transcript.jsonl` (authoritative append-only + in-place patch by `seq`), `transcript.md` (rendered), `summary.md` (on demand), crash-safe `audio_<source>.NNN.wav` + `samples.idx` during capture, canonical `audio_you.wav`/`audio_them.wav` on finalize, plus backups on rewrite. Schema versioning via `schema: 2` on records and meta, with documented migration from schema 1 (§5.7). Secrets are **not** in the data model — they live in the OS keychain (§5.10).

---

## 7. Settings / Config Reference

All non-secret keys, types, defaults. Persisted in `%LOCALAPPDATA%\ai-record\settings.json`. Secrets (`hf_token`, `gemini_api_key`) are **not** here — they live in the OS keychain (§5.10) and appear in the settings API only as `*_is_set` booleans.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `consent_acknowledged` | bool | `false` | User accepted Legal & Consent. **Enforced server-side** on capture start (§3, §5.8). |
| `consent_acknowledged_at` | str/null | `null` | ISO timestamp of acknowledgement. |
| `server_port` | int | `8848` | Localhost port (auto-bumps if busy). |
| `sessions_root` | str | `"%LOCALAPPDATA%/ai-record/sessions"` | Session root. Resolved from `LOCALAPPDATA`. Repo-local only as explicit dev override. |
| `hardware_preset` | enum | `"auto"` | `auto`\|`cpu`\|`gpu_8gb`\|`gpu_12gb`\|`gpu_16gb_plus`. `auto` detects VRAM (§4.3). Reference machine → `gpu_12gb`. |
| `audio_backend` | enum | `"auto"` | `auto`\|`soundcard`\|`pyaudiowpatch`. |
| `persist_audio` | bool | `true` | Write crash-safe raw segments + canonical WAVs (needed for tier-2 + recovery). |
| `raw_segment_seconds` | int | `60` | Rolling crash-safe segment length (bounds crash loss). |
| `silent_loopback_warn_s` | int | `20` | Warn if loopback open but RMS≈0 this long while recording. |
| `silence_rms_eps` | float | `1e-4` | RMS below this counts a frame as silent (health telemetry). |
| `device_reopen_retries` | int | `5` | Reopen attempts on device change. |
| `target_sample_rate` | int | `16000` | Pipeline sample rate (do not change in v1). |
| `frame_ms` | int | `20` | VAD frame hop. |
| `vad_engine` | enum | `"silero"` | `silero`\|`webrtcvad`. |
| `vad_device` | enum | `"cpu"` | `cpu`\|`cuda` for Silero VAD. |
| `vad_aggressiveness` | int | `2` | webrtcvad only, 0–3. |
| `pre_roll_ms` | int | `300` | Audio kept before onset. |
| `speech_start_ms` | int | `150` | Sustained speech to start. |
| `silence_end_ms` | int | `600` | Trailing silence to end (latency knob). |
| `min_speech_ms` | int | `250` | Drop shorter utterances. |
| `max_utterance_seconds` | int | `15` | Force-cut long utterances. |
| `forced_cut_overlap_ms` | int | `200` | Prior-audio overlap re-included after a forced cut (§5.3). |
| `whisper_model` | enum | *(preset)* | `small`\|`medium`\|`large-v2`\|`large-v3`. Default from preset (`gpu_12gb`→`large-v3`). |
| `whisper_compute_type` | enum | *(preset)* | `float16`\|`int8_float16`\|`int8`. Default from preset (`gpu_12gb`→`int8_float16`). |
| `latency_mode` | enum | `"fast"` | `quality`(beam 5)\|`fast`(beam 1). Default `fast` on gpu_8gb/gpu_12gb. |
| `whisper_vad_filter` | bool | `true` | Secondary VAD filter in faster-whisper. |
| `force_language` | str/null | `null` | Force Whisper language, else auto. |
| `no_speech_threshold` | float | `0.6` | Hallucination guard. |
| `logprob_drop_threshold` | float | `-1.0` | Hallucination guard. |
| `min_rms` | float | `0.005` | Drop near-silent utterances. |
| `hallucination_denylist` | list[str] | (see §5.3) | Whole-utterance texts to drop. |
| `auto_downgrade_on_backpressure` | bool | **`true`** | Enable the fallback ladder (§4.4). |
| `backpressure_utt_threshold` | int | `2` | Backlog utterances → trigger ladder step 1. |
| `backpressure_lag_seconds` | int | `3` | Oldest-queued age → trigger ladder step 1. |
| `recovery_stable_seconds` | int | `30` | Backlog-clear duration before stepping back up. |
| `translate_enabled` | bool | `false` | Live translation on/off ("Dịch"). |
| `target_lang` | str | `"vi"` | Translation target. |
| `source_languages` | list[str] | `[]` | Langs to translate; empty = any non-target. |
| `translation_provider` | enum | `"nllb"` | `nllb`\|`gemini`. |
| `nllb_model` | str | `"facebook/nllb-200-distilled-600M"` | HF model id (CT2-converted int8). |
| `translation_device` | enum | *(preset)* | `cuda`\|`cpu` for NLLB. Default **`cpu`** on gpu_8gb/gpu_12gb. |
| `translate_min_duration_s` | float | `1.0` | Min utterance length to trust `lang` for translate (§5.4). |
| `translate_min_lang_prob` | float | `0.6` | Min lang confidence to trust `lang`. |
| `translate_batch_window_ms` | int | `400` | Batching window for adjacent short utterances. |
| `translate_batch_max_s` | float | `4.0` | Max per-utterance length eligible for batching. |
| `translation_max_staleness_s` | float | `8.0` | Skip translations older than this (backlog policy). |
| `diarization_enabled` | bool | `true` | Master diarization toggle. |
| `diarization_realtime` | bool | `true` | Tier-1 online clustering (may be off on `cpu` preset / by ladder). |
| `diarization_embedder` | enum | *(preset)* | `ecapa`\|`resemblyzer`. Default **`resemblyzer`** on gpu_12gb and below. |
| `diarization_device` | enum | *(preset)* | `cuda`\|`cpu`. Default **`cpu`** on gpu_12gb and below. |
| `sim_threshold_ecapa` | float | `0.75` | Cosine threshold for ECAPA. |
| `sim_threshold_resemblyzer` | float | `0.70` | Cosine threshold for Resemblyzer. |
| `centroid_update_min_conf` | float | `0.6` | Min confidence to update a centroid (§5.5). |
| `min_speaker_speech_s` | float | `3.0` | Accumulated speech before a centroid is trusted as a match target. |
| `min_embed_ms` | int | `800` | Min utterance length to embed/cluster. |
| `max_speakers` | int | `8` | Cap; overflow → `"Speaker ?"` + warning (never silent force). |
| `pyannote_model` | str | `"pyannote/speaker-diarization-3.1"` | Tier-2 pipeline. |
| `summarizer_provider` | enum | `"claude_cli"` | `claude_cli`\|`codex_cli`\|`gemini`\|`ollama`. |
| `summary_prompt` | str | (see §5.6) | Editable summarization prompt (hardened wrapper applied regardless). |
| `summary_use_translation` | bool | `true` | Feed Vietnamese text to summarizer when available. |
| `summary_max_chars` | int | `48000` | Chunking threshold (map-reduce beyond). |
| `summary_timeout_s` | int | `300` | Subprocess/API timeout. |
| `ollama_model` | str | `"llama3.1"` | Ollama model. |
| `ollama_url` | str | `"http://localhost:11434"` | Ollama endpoint. |
| `retention_days` | int | `0` | 0 = keep forever; else prune sessions older than N days (§5.7). |
| `fsync_interval_ms` | int | `1000` | Autosave durability throttle. |
| `ws_client_queue_max` | int | `256` | Per-client WS outgoing queue bound (§4.7). |
| `ws_client_slow_deadline_s` | int | `10` | Close a client whose durable queue stays full this long. |
| `theme` | enum | `"auto"` | `auto`\|`light`\|`dark`. |
| `hf_token_is_set` | bool (read-only) | — | Reflects keychain presence; secret value never returned. |
| `gemini_api_key_is_set` | bool (read-only) | — | Reflects keychain presence; secret value never returned. |
| `app_version` | str | `"2.0"` | Read-only. |

Validators: enums constrained; thresholds range-checked; `source_languages` items must be known ISO codes; explicit `whisper_model`/device overrides validated against detected VRAM with a warning; changing `hardware_preset`/`whisper_model` mid-session takes effect at next `start`.

---

## 8. Error Handling

All surface a clear UI notice via WS `error`/`status`; none crash the pipeline:

1. **No loopback device / output missing:** mic-only, `them.available=false`, degraded chip "recording — mic only", note "Recording microphone only — no system audio device."
2. **Mic blocked / missing:** loopback-only, `you.available=false`, note "Recording system audio only — microphone unavailable (check Windows mic privacy)."
3. **Both sources fail:** `start` returns error; no recording state; actionable message.
4. **Device change mid-session:** auto-reopen with retries, **new `source_epoch_id`**, epoch boundary recorded (§4.8, §5.1); note on lost/reopened.
5. **Silent loopback:** open but RMS≈0 for `silent_loopback_warn_s` → warning "No audio from your speakers?" (§5.1). Capture continues.
6. **GPU OOM (load/inference):** empty cache → ladder model rungs → CPU last resort (§5.3). Persist effective model.
7. **Whisper hallucination on silence:** VAD gating + thresholds + denylist + `min_rms` (§5.3); dropped utterances not emitted.
8. **Translation failure / unmapped language:** `translation=null` + `translation_error=true` (or skip for unmapped, log once); UI "translation failed" hint. Never blocks STT.
9. **Translation backlog:** stale translations skipped (`stale_skipped=true`), "translation paused" chip; offline batch-translate available (§5.4).
10. **Summarizer CLI missing / unavailable:** `available()` false → graceful message naming an alternative provider; no `summary.md` written.
11. **HF token missing for tier-2:** Re-diarize disabled with tooltip + link to accept model terms.
12. **pyannote/model download failure or offline:** clear error; tier-2 available once cached.
13. **Backpressure (GPU can't keep up):** ladder triggers at backlog > 2 utt / > 3 s (§4.4); degraded chips; **raw audio never dropped** (§4.6) so offline catch-up recovers everything.
14. **Crash / power loss:** crash-safe raw segments lose ≤ 1 minute of audio; JSONL loses ≤ ~1 s; on relaunch, incomplete session detected → **offline recovery** transcribes the untranscribed tail (§5.7).
15. **Port in use:** auto-bump `server_port`; report chosen port.
16. **WebSocket disconnect / slow client:** per-client bounded queues; lagging clients closed and replay durable events by `seq` via `GET /api/sessions/{id}/utterances?since_seq=N` (§4.7, §5.8).
17. **Unauthorized request (missing/bad token or bad Origin):** 401 / rejected (§5.8).
18. **Consent not acknowledged:** `POST /api/capture/start` → 403 (§3).

---

## 9. Testing Strategy

### 9.1 Unit tests (`tests/unit/`)
- **Segmenter boundaries:** synthetic 16 kHz PCM (sine bursts + silence). Assert utterance count, `min_speech_ms` drop, `silence_end_ms` boundary, `max_utterance_seconds` forced cut (with `forced_cut=True`), pre-roll inclusion, and correct `audio_start_sample`/`audio_end_sample`/`source_epoch_id` propagation. Deterministic via webrtcvad or a scripted fake VAD.
- **Timebase:** feed a fake capture with a mid-stream reopen; assert `source_epoch_id` increments, `samples.idx` records the epoch base, and sample→session-time mapping is piecewise-linear and monotonic within an epoch (§4.8).
- **Fallback ladder:** drive a fake slow transcriber; assert step 1 triggers at backlog > 2 utt / > 3 s (not 8 s), each rung applies in order, and step-back-up honors `recovery_stable_seconds` hysteresis (§4.4).
- **Translator via mocks:** lang-code mapping; gating (enabled ∧ lang≠target ∧ lang∈set); short-utterance defer (`translate_min_duration_s`/`lang_prob`); batching of adjacent same-lang/speaker; **staleness skip** past `translation_max_staleness_s`; unmapped pass-through; `GeminiTranslator.available()` with/without keychain key.
- **Diarizer clustering:** fake embedder with controlled vectors — identical cluster, distant → new speaker, **model-specific threshold** boundary, `min_embed_ms` → `"Speaker ?"`, **no centroid update** on low-confidence/short/overlap, `min_speaker_speech_s` trust gate, `max_speakers` overflow → `"Speaker ?"` + `forced_overflow=true` (no silent force), rename propagation, "You" always for mic, confidence populated.
- **Summarizer hardening:** patch `subprocess.run`; assert transcript passed **via stdin only** (never argv, `shell=False`), delimiters + system instruction present, isolated `cwd`, `CREATE_NO_WINDOW`, no-tools flags for claude/codex; prompt assembly (timestamps/labels/translation-vs-original, forced-cut stitching); chunking above `summary_max_chars`; graceful error on missing binary/timeout.
- **Store round-trip & schema:** create → append N records → read back JSONL → equality; **`patch_utterance`** updates fields visible on read; `utterances_since(seq)` catch-up; `rename_speaker` atomic (temp+`os.replace`) updates records+meta+md; finalize sorts by start; partial-trailing-line tolerance; **schema-1→2 migration** fills defaults; per-session RWLock blocks rediarize/rename during capture; crash-safe `RawSegmentWriter` produces valid per-minute WAV headers + concat.
- **Config & secrets:** load/save round-trip, defaults, validator rejection, unknown-key tolerance; `redacted()` never emits secret values; `Secrets` reads/writes via keyring (mocked); `settings.json` written with owner-only ACL.
- **Server auth:** requests without token → 401; bad `Origin` → rejected; `POST /api/capture/start` without consent → 403; `GET /api/settings` is redacted; `POST/DELETE /api/secrets/{name}` write-only.

### 9.2 Integration tests (`tests/integration/`)
- **Pipeline on a known WAV:** a `FileCaptureSource` streams a bundled dual-stream fixture (speech + silent gap) through the *real* segmenter and a small Whisper (`small`, `int8`, CPU allowed in CI). Assert: STT-first `utterance` emitted before any `patch`; non-empty transcript; files written; records well-formed at `schema 2`. Translation/diarization mocked or tiny.
- **Progressive patch:** assert a `patch` message arrives after the `utterance` and updates translation/speaker in the store.
- **Recovery:** simulate an incomplete session (no `ended_at`, raw segments beyond last transcript sample); `POST /api/sessions/{id}/recover` transcribes the tail and finalizes; assert recovered utterance count and `meta.recovered=true`.
- **Server API:** FastAPI `TestClient` (with token) — settings redaction, secrets write-only, sessions list/open/**delete**/**delete-audio**, catch-up `since_seq`, summarize with mocked provider writes `summary.md`, rename, 409 on rediarize during capture. WS receives `utterance` then `patch` for the file source.

### 9.3 Manual smoke test (user step; documented)
- One real online meeting. Verify: both sources captured; **preflight** shows the `gpu_12gb` preset; live transcript appears immediately, translation + speaker labels patch in shortly after; degraded chips behave under load; stop → summarize (hardened Claude CLI) → structured notes; Re-diarize (with HF token) relabels cleanly on the sample timeline; session folder has all artifacts under `%LOCALAPPDATA%`; delete + delete-audio work.
- **Explicitly NOT auto-testable** (deferred to §9.6 benchmark on real hardware): live WASAPI loopback across drivers, real-time RTF/latency on the RTX 4070, device-change hot-swap, actual translation/summary quality.

### 9.4 Tooling
`pytest`, `pytest-asyncio`, `numpy` (synthetic PCM), `soundfile` (WAV assertions). GPU/model-heavy tests marked `@pytest.mark.gpu`, skipped without CUDA/model.

### 9.5 Acceptance criteria (measurable)
The build is "done enough to ship a milestone" only when, on the reference RTX 4070 (`gpu_12gb`), the benchmark harness (§9.6) shows:
- **Latency:** p50 end-of-utterance → text ≤ **1.5 s**; p95 ≤ **3.0 s** (fast mode, `large-v3 int8_float16`, translation+diarization on CPU).
- **Backlog:** sustained backlog before the ladder triggers ≤ **2 utterances / 3 s**; under a 1.3× real-time speech load the ladder keeps p95 text latency ≤ **4 s** by degrading (never unbounded lag).
- **Progressive patch:** translation patch p95 ≤ **6 s** after text (or explicitly `stale_skipped`); speaker patch p95 ≤ **4 s** after text.
- **Recovery:** after a simulated crash, offline recovery reproduces **100%** of utterances present in the raw audio tail (byte-diff on canonical WAV; transcript covers the full audio span).
- **Per-preset behavior:** each preset selects the documented stack (§4.3); `cpu` preset never enables realtime diarization/live translation; overrides validated.
- **Windows audio test matrix** (all must capture + transcribe without crashing): 48 kHz stereo loopback; 44.1 kHz mono mic; device switch mid-session (epoch increments, no crash, gap recorded); silent loopback (warning fires, capture continues).

### 9.6 Benchmark harness (`tests/bench/`)
A scriptable harness that replays **dual-stream WAV fixtures** through the real pipeline (mockable STT for CI, real STT on GPU locally) and reports, **per preset** (primary `gpu_12gb`; also `cpu`-degraded, and `gpu_8gb` if a card is available): **RTF** (real-time factor), **p50/p95 end-of-utterance latency**, **queue depth over time**, **VRAM peak** (`torch.cuda.max_memory_allocated`), **dropped frames**, **ladder transitions**, and **observed cross-source drift** over a 60-minute fixture (§4.8). Output is a JSON + a short Markdown table. This harness is the gate at each milestone (§12) and validates the two deferred hardware unknowns (loopback reliability is validated in the manual matrix; RTF on the 4070 here).

---

## 10. Dependencies & Environment

### 10.1 Assumptions
- Windows 11 (WASAPI loopback is Windows-specific).
- Python 3.12.
- NVIDIA GPU with working CUDA-enabled PyTorch; faster-whisper installed and functional on GPU. **Reference: RTX 4070, 12 GB → `gpu_12gb` preset.**
- Internet on **first run only** to download models (cached thereafter).

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
soxr>=0.3.7               # streaming resample (ResampleStream)
soundfile>=0.12           # WAV read/write

# --- VAD ---
silero-vad>=5.1
webrtcvad>=2.0.10

# --- translation (NLLB via CTranslate2 int8) ---
ctranslate2>=4.3
transformers>=4.44
sentencepiece>=0.2
torch                     # provided by existing CUDA install (do not reinstall blindly)

# --- speaker embeddings (tier-1) ---
resemblyzer>=0.1.4        # default embedder (CPU, gpu_12gb and below)
speechbrain>=1.0          # ECAPA-TDNN (opt-in / gpu_16gb_plus)

# --- diarization (tier-2) ---
pyannote.audio>=3.1

# --- backend / server ---
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic>=2.7
websockets>=12

# --- secrets ---
keyring>=25              # Windows Credential Manager backend

# --- desktop shell ---
pywebview>=5.1

# --- misc ---
python-dateutil>=2.9
```
Notes:
- **Do not** clobber the working CUDA `torch`/`faster-whisper`. Install the rest with `--no-deps` where needed; ship `requirements-notes.md` warning about torch.
- Optional `google-generativeai` (Gemini) is an extra, installed only if the user enables that provider. Ollama and Claude/Codex CLIs are external executables, not pip deps.

### 10.3 Model downloads (first run, approximate)
- Whisper `large-v3` (CT2): files ~1.5 GB; `medium` ~0.8 GB; `small` ~0.5 GB.
- NLLB-200 distilled-600M (CT2 int8): ~1.2–1.5 GB files, runs on CPU by default (`gpu_12gb`).
- Resemblyzer: ~15 MB (default embedder); ECAPA-TDNN: ~80 MB (opt-in).
- pyannote 3.1 pipeline: ~30–100 MB, gated (HF token + accept terms).
The **preflight screen** (§5.9) checks cache presence + free disk before recording. Document total first-run download (~4–6 GB) and offline-capable subsequent runs.

### 10.4 VRAM budget guidance (per preset)
The presets (§4.3) are designed to fit their VRAM class with the GPU reserved for STT:
- **`gpu_12gb` (default, RTX 4070):** only Whisper `large-v3 int8_float16` resides on the GPU (~2.5–3.5 GB peak incl. CT2 working set + activations, well under 12 GB). NLLB and the speaker embedder run on **CPU**, so they do not consume VRAM. Realtime diarization is on (CPU embedder). Tier-2 pyannote runs post-meeting when STT is idle and may use the GPU freely.
- **`gpu_8gb`:** Whisper `medium int8_float16` on GPU (~1.5–2 GB); translation + embedder on CPU; realtime diarization default off.
- **`gpu_16gb_plus`:** `large-v3 fp16` beam 5 on GPU, and NLLB/ECAPA may share the GPU under the priority scheduler (§4.5).
- **`cpu`:** everything on CPU; `small int8`; no live translation/realtime diarization.
The benchmark harness reports the **actual** VRAM peak per preset (§9.6); the fallback ladder (§4.4) protects against any misestimate.

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
    __main__.py
    config.py                    # Settings (pydantic) + Secrets (keyring) + load/save + redaction
    capture.py                   # WASAPI dual-stream capture, backend contract, health, epochs
    ring_buffer.py               # RingBuffer helper
    segmenter.py                 # per-source VAD segmentation (sample-accurate)
    transcriber.py               # faster-whisper wrapper (preset-driven, STT-first)
    translator.py                # Translator interface + NLLB CT2 int8 + Gemini stub
    diarizer.py                  # RealtimeDiarizer (T1, confidence) + OfflineDiarizer (T2, sample-time)
    summarizer.py                # Summarizer interface + hardened Claude/Codex/Gemini/Ollama
    store.py                     # SessionStore + WavWriter + RawSegmentWriter + RWLock + retention
    preset.py                    # hardware preset detection + ladder logic
    pipeline.py                  # wires capture->segment->STT(emit)->post(patch)->store/broadcast
    server.py                    # FastAPI app, REST + WS, auth/consent, lifecycle
    app.py                       # starts uvicorn thread + opens pywebview window with token
    lang_maps.py                 # Whisper<->NLLB code maps, denylists
  web/
    index.html
    app.js
    styles.css
    assets/                      # bundled fonts/icons (no CDN)
  tests/
    unit/
    integration/
    bench/                       # benchmark harness (§9.6)
    fixtures/                    # small + dual-stream known WAVs
```
- **App data dir:** `%LOCALAPPDATA%\ai-record\` holds `settings.json` and `sessions\` **by default** (resolved from `os.getenv("LOCALAPPDATA")`), **not** under the repo. Repo-local storage is available only as an explicit dev override (`sessions_root`). This prevents accidental `git add` / repo sync of meeting audio, transcripts, and (formerly) secrets.

### 11.2 Entry point (`python -m ai_record` / `main.py`)
`app.py`:
1. Load `Settings`; resolve `%LOCALAPPDATA%\ai-record\`; run `apply_retention()`; detect incomplete sessions (offer recovery in UI).
2. Generate the per-launch API token.
3. Start Uvicorn (FastAPI `server.app`) in a background thread bound `127.0.0.1:<port>` (port-bump). Wait until health-ready.
4. Open a pywebview window (frameless, on-top, compact) pointing at `http://127.0.0.1:<port>?token=<token>`.
5. `webview.start()` blocks; on close, signal the server to stop capture (finalize) and shut down Uvicorn, then exit.
`main.py`: `from ai_record.app import main; main()`. `__main__.py` calls the same.

---

## 12. Milestones / Build Plan

**All v1 features (§2.1) ship**, but the *build* is sequenced into gated milestones. Each milestone is **independently runnable** and passes a **benchmark/acceptance gate** (§9.5/§9.6) before the next begins. This directly answers the review's "cut/sequence v1 scope" without dropping features.

- **M0 — Skeleton:** repo layout, `config.py` (+ `Secrets`), `preset.py` VRAM detection, FastAPI server with `/health` + `/api/preflight` + token auth + Origin check + static UI shell, pywebview window opens with token. (No audio.)

- **M1 — Core recorder (ship + benchmark):** dual WASAPI capture behind the backend contract + resample + source health + **crash-safe per-minute WAV + samples.idx** + VAD segmentation (sample-accurate) + **STT** (preset-driven, STT-first) + `store.py` (schema 2, autosave, RWLock) + **server-side consent gate** + **token auth** + **preflight screen** + compact/expanded UI showing **live transcript** + **incomplete-session recovery**. This is a genuinely useful product on its own: a crash-safe local meeting transcriber. **Gate:** acceptance latency + recovery + Windows audio matrix (§9.5) on the RTX 4070.

- **M2 — Live translation:** `translator.py` NLLB CT2 int8 (CPU on `gpu_12gb`) + gating + "Dịch" toggle + **progressive patch UI** + latency budget (defer/batch/staleness) + Gemini stub. **Gate:** translation-patch p95 ≤ 6 s or explicit stale-skip (§9.5).

- **M3 — Realtime diarization (Tier 1):** `diarizer.py` realtime (Resemblyzer CPU default) with **confidence + model-specific thresholds + "Speaker ?" unknown/overflow + no-drift centroid rules** + renameable labels + patch UI. **Gate:** clustering unit suite + no STT-latency regression.

- **M4 — Offline enrichment:** `diarizer.py` Tier-2 pyannote re-diarize (sample-time, HF token flow, disallowed during capture) + `summarizer.py` **hardened** Claude CLI default + provider selection + summary panel + **delete/retention** UI + expanded-UI polish (search, settings, anti-slop design pass). **Gate:** re-diarize relabels correctly on the sample timeline; summarizer runs stdin/no-tools/isolated-cwd; delete/retention verified.

- **Future (out of v1):** `.exe` packaging (PyInstaller), overlapping-speech separation, macOS/Linux, per-tenant model presets.

---

## 13. Risks & Open Questions

Most v1 open questions are now **resolved into the design** (presets §4.3, ladder §4.4, sample timebase §4.8, crash-safety §5.1, server-side consent/auth §5.8, hardened summarizer §5.6, keychain secrets §5.10, storage location §11). What remains are genuinely **hardware-dependent unknowns**, to be *validated* (not designed) by the benchmark harness + manual matrix (§9):

- **Loopback library reliability across drivers (hardware-dependent).** `soundcard` vs `PyAudioWPatch` behavior varies across driver/format combos; some machines may only work on one backend. Mitigation: backend contract + `audio_backend` override + robust probing + the Windows audio test matrix (§9.5). **Validate on real hardware.**
- **Real-world RTF/latency on the RTX 4070 (hardware-dependent).** Presets and the fallback ladder are designed to keep latency bounded, but the actual p50/p95 under `large-v3 int8_float16` on the 4070 is measured, not assumed. **Validate via the benchmark harness (§9.6);** the ladder guarantees graceful degradation if RTF is worse than hoped.
- **NLLB JA→VI quality (accepted tradeoff, not a bug).** Moderate; Gemini improves it but sends text to Google — a documented Settings tradeoff.
- **Overlapping speech (accepted non-goal).** Tier-1 marks overlap and abstains (`"Speaker ?"`, no centroid update); tier-2 preserves multiple candidates where pyannote reports overlap. Perfect separation remains out of scope.
- **Agentic-CLI residual prompt-injection (mitigated, residual documented).** The summarizer runs stdin-only, no-tools, isolated cwd; residual risk is at worst a misleading summary. Safest alternative (Gemini/Ollama) is one setting away; Claude CLI stays default per user preference (§5.6).
- **pywebview frameless drag/controls on Windows.** Custom chrome can be fiddly with the EdgeChromium backend; fallback to a thin native title bar if frameless drag proves unstable. Needs verification.
- **Legal exposure.** Recording without a platform indicator is legally sensitive; the consent gate (now server-enforced) + honest framing (§3) are mandatory, but the user bears compliance responsibility.

---

## Changelog (v1 → v2)

This revision integrates the `codex-spec-review-01` adversarial review. Accepted essentially all Critical/Important/Minor findings.

1. **Header/status:** downgraded "locked / implementation-ready" to "design resolved"; open questions moved into the design; only hardware-dependent unknowns remain (§ header, §13).
2. **Hardware presets + VRAM auto-detect (`hardware_preset`)** replace "default `large-v3` fp16 everywhere". `auto` detects VRAM; `gpu_12gb` is the default for the reference RTX 4070; presets documented in §4.3 and the Settings table (§7). (Critical 1)
3. **Fallback ladder** with `auto_downgrade_on_backpressure=true` by default, triggered at backlog > 2 utt / 3 s (not 8 s), with the full ordered ladder down to audio-only offline catch-up (§4.4). (Critical 2)
4. **STT-first progressive pipeline:** STT emits/persists/broadcasts immediately; translation + Tier-1 diarization are lower-priority async **patches**; new `patch` WS message; post-processing defaults to CPU and never blocks STT (§4.5, §5.8). (Critical 3, Suggestion 3)
5. **Sample-accurate timebase:** per-source sample counters + `source_epoch_id`; utterance records store `audio_start_sample`/`audio_end_sample`/`source_epoch_id`/`source_offset_sec`; tier-2 relabels in `audio_them.wav` sample time; drift documented (§4.8, §5.5). (Critical 4)
6. **WASAPI backend contract** reporting actual sample rate/channels/format/device id/block duration + byte decoding; per-source health telemetry (RMS, silent/overrun/underrun/reopen counts); silent-loopback warning (§5.1). (Critical 5)
7. **Crash-safe capture:** rolling per-minute WAV segments + `samples.idx`, incomplete-session detection, offline recovery of the untranscribed tail (§5.1, §5.7). (Critical 6)
8. **Local API protection:** per-launch token in the pywebview URL required for all REST + WS, Origin allow-listing, and **server-side consent gate** (403) on capture start (§3, §5.8). (Critical 7)
9. **Hardened summarizer:** transcript treated as untrusted; stdin-only, no `shell=True`, delimiters + system instruction, no-tools/read-only sandbox flags, isolated cwd, `CREATE_NO_WINDOW`; residual risk documented; Gemini/Ollama noted as safest, Claude CLI kept default (§5.6). (Critical 8)
10. **Diarization robustness:** model-specific thresholds, per-assignment confidence, min accumulated speech before centroid trust, explicit `"Speaker ?"` for short/low-confidence/overlap and `max_speakers` overflow, no centroid updates on low-confidence/short/overlap; `diarization_confidence`/`forced_overflow`/`is_overlap` persisted (§5.5, §7). (Important 1, 2, 3)
11. **Secrets via OS keychain (`keyring`)**; `GET /api/settings` redacted; write-only secret endpoints; owner-only ACL on `settings.json` (§5.10, §5.8, §7). (Important 7)
12. **Storage in `%LOCALAPPDATA%\ai-record\`** by default (repo-local only as dev override); first-class **delete session / delete audio-only / retention** (§5.7, §5.8, §11). (Important 10, 11)
13. **Preflight/readiness screen** (CUDA/version, model load + cache + disk, HF terms, CLI availability, auto-preset) (§5.9, `GET /api/preflight`). (Important 12)
14. **WebSocket robustness:** per-client bounded queues, coalesce/drop `status`, never drop durable events (replay by `seq` via catch-up endpoint), fixed the `call_soon_threadsafe(put_nowait)` QueueFull hazard (§4.7, §5.8). (Important 5)
15. **Store concurrency:** per-session RWLock; rediarize/rename disallowed during capture (409) or on snapshot; all full-file rewrites atomic via temp + `os.replace` (§5.7). (Important 6)
16. **Translation latency budget:** CT2 int8, translate after STT, batching, staleness skip, min-duration/confidence gating before trusting `lang` (§5.4, §7). (Important 8)
17. **Whisper forced-cut context:** `forced_cut_overlap_ms` padding, `forced_cut` persisted, adjacent forced chunks stitchable for summarization (§5.2, §5.3, §5.6). (Important 9)
18. **Acceptance criteria** (measurable latency/backlog/recovery/per-preset targets + Windows audio matrix) (§9.5). (Critical 9)
19. **Benchmark harness** (dual-stream fixtures; RTF, p95 latency, queue depth, VRAM peak, dropped frames, drift per preset) (§9.6). (Suggestion 2)
20. **Degraded-mode UX:** explicit states ("recording audio only", "STT catching up", "translation paused", "speaker labels offline-only") replace the single amber dot (§5.9). (Suggestion 4)
21. **Milestone build plan (M1–M4)**, each independently runnable and benchmark-gated, keeping all v1 features (§12). (Suggestion 1)
22. **Schema v2:** added `audio_start_sample`, `audio_end_sample`, `source_epoch_id`, `source_offset_sec`, `forced_cut`, `diarization_confidence`, `is_overlap`, `forced_overflow`, `speaker_alt`, `stale_skipped`, `effective_model`, `effective_compute_type`, `stt_latency_ms`; documented migration from schema 1 (§5.7, §6). (Minor 4)
23. **Placeholders + references fixed:** `channels=…`/`beam_size=…`/`Speaker …` replaced with concrete defaults; broken refs (`A11.5`, `A5.7`) normalized to `§N` form (throughout). (Minor 2, 3) The "mojibake" finding (Minor 1) was a **false alarm** — the file is valid UTF-8; no re-encoding performed.

*End of specification.*
