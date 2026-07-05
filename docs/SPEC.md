# ai-record — Software Specification

**Version:** 1.0 (implementation-ready)
**Target platform:** Windows 11, Python 3.12, NVIDIA GPU with CUDA-enabled PyTorch
**Status:** Locked design. This document is the single source of truth for a first implementation. An engineer should be able to build v1 directly from it without further design decisions.

---

## 1. Overview

**ai-record** is a *local* meeting-scribe desktop application. While the user is in an online meeting (Microsoft Teams, Zoom, Google Meet, Webex, a browser call, or any app that plays audio through the speakers), ai-record:

1. **Captures** two audio streams simultaneously on Windows:
   - The **system audio mix** via WASAPI *loopback* of the default output device — i.e. everything the speakers play, which includes remote participants. Labelled **"Them"**.
   - The **default microphone** — the local user's own voice. Labelled **"You"**.
2. **Segments** each stream independently into utterances using voice-activity detection (VAD), dropping silence.
3. **Transcribes** each finalized utterance to text in near-real-time with faster-whisper on the GPU, detecting the spoken language.
4. **Translates** foreign-language utterances to Vietnamese live (optional, toggleable), using a local NLLB-200 model by default.
5. **Diarizes speakers** in two tiers: a rough real-time online-clustering pass (for the "Them" stream) and an accurate offline `pyannote.audio` re-diarization pass on demand after the meeting.
6. **Summarizes** the transcript on demand, post-meeting, by shelling out to a local AI CLI (Claude Code by default) or other pluggable providers, producing structured Markdown notes.
7. **Persists** every session (transcript, structured records, raw audio per source, summary) to disk, autosaving each utterance so a crash never loses data.

Everything runs **on the user's machine**. No audio or transcript leaves the computer except when the user explicitly invokes a cloud-based translation or summarization provider (both off/local by default).

The UI is a small **frameless, always-on-top** window (a compact bar by default, expandable to a full transcript view), implemented as a local web app served by FastAPI and wrapped in **pywebview**.

### 1.1 Why loopback capture

ai-record does **not** use any meeting platform's official recording API. It records the operating system's audio output (loopback) plus the microphone. This is the same technique used by mainstream AI note-takers (Otter, tl;dv, Fireflies, Fathom). A direct and honest technical consequence is that **the meeting platform displays no "recording" indicator**, because from the platform's perspective nothing is being recorded — the OS is simply playing audio and ai-record is listening to the speaker output like any other audio app. See the **Legal & Consent** section (§3); this behavior must be framed to the user as a technical consequence, never marketed as an anti-detection or stealth feature.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1, in scope)

- Dual-stream WASAPI loopback + microphone capture, resampled to 16 kHz mono.
- Per-source VAD segmentation into utterances with low latency.
- Real-time GPU transcription with language detection and hallucination guards.
- Live optional translation of foreign speech → Vietnamese (local NLLB default; Gemini stub pluggable).
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
- `.exe` / installer packaging (PyInstaller etc.) — noted as future work.
- Cloud STT (Whisper stays local). Cloud is only optionally used for translation/summarization.
- Non-Windows platforms (macOS/Linux). WASAPI loopback is Windows-specific.
- Mobile / web-hosted / multi-user deployments.
- Speaker *identification* against a named voiceprint database (we only cluster into anonymous, renameable "Speaker N").
- Real-time translation of the user's own outgoing speech beyond the same pipeline treatment (no TTS back-translation).

---

## 3. Legal & Consent (READ FIRST)

> **This section MUST be surfaced in the app** (a first-run modal that the user must acknowledge, plus a permanent link in Settings). The text below is the normative content.

ai-record captures audio by recording your computer's audio output (WASAPI loopback) together with your microphone. Because it does **not** use the meeting platform's recording feature, **the meeting platform will not show a recording indicator to other participants.** This is a technical consequence of loopback capture — the app is listening to your speakers the same way any audio app does — and **not** a feature designed to hide recording from anyone.

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

The pipeline is a chain of producer/consumer stages connected by bounded queues. Each source ("You", "Them") has its own capture → ring buffer → segmenter. Finalized utterances from both sources are merged into a single work queue consumed by the transcription worker, then translation, then real-time diarization, then persistence + WebSocket broadcast.

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
              │  loopback stream  │              │  mic stream       │
              │  → resample 16k   │              │  → resample 16k   │
              │  → mono           │              │  → mono           │
              └─────────┬─────────┘              └─────────┬─────────┘
                        │ 16k mono float32 frames          │
                        ▼                                  ▼
              ┌───────────────────┐              ┌───────────────────┐
              │ RingBuffer "Them" │              │ RingBuffer "You"  │
              └─────────┬─────────┘              └─────────┬─────────┘
                        ▼                                  ▼
              ┌───────────────────┐              ┌───────────────────┐
              │ segmenter.py      │              │ segmenter.py      │
              │ VAD (Silero)      │              │ VAD (Silero)      │
              │ → utterance chunk │              │ → utterance chunk │
              └─────────┬─────────┘              └─────────┬─────────┘
                        │  Utterance{source, pcm, t0, t1}  │
                        └──────────────┬───────────────────┘
                                       ▼
                             ┌───────────────────┐
                             │  utterance_queue  │  (bounded, backpressure)
                             └─────────┬─────────┘
                                       ▼
                             ┌───────────────────┐
                             │ transcriber.py    │  faster-whisper (GPU, fp16)
                             │ text + lang       │  VAD filter (secondary)
                             └─────────┬─────────┘
                                       ▼
                             ┌───────────────────┐
                             │ translator.py     │  NLLB (if enabled & lang≠vi)
                             │ + translation     │
                             └─────────┬─────────┘
                                       ▼
                             ┌───────────────────┐
                             │ diarizer.py (T1)  │  ECAPA embed + online cluster
                             │ speaker label     │  ("You" is fixed for mic)
                             └─────────┬─────────┘
                                       ▼
                        ┌──────────────┴──────────────┐
                        ▼                              ▼
              ┌───────────────────┐        ┌───────────────────────┐
              │ store.py          │        │ server.py WebSocket   │
              │ append JSONL +    │        │ push live utterance   │
              │ transcript.md +   │        │ → UI (compact/expand) │
              │ audio_*.wav       │        └───────────────────────┘
              └───────────────────┘

   POST-MEETING (on demand):
     summarizer.py  ── reads transcript TEXT ──► summary.md
     diarizer.py T2 ── reads audio_*.wav (pyannote) ──► relabel transcript
```

### 4.3 Concurrency & backpressure model

- **Capture threads (2):** one per source. Each is a dedicated OS thread (audio callbacks / blocking record loops). They write fixed-size frames into a lock-free-ish `RingBuffer` (a `numpy` circular buffer guarded by a `threading.Lock`, or `collections.deque` of frames). Capture must never block on downstream work; if the ring buffer is full it overwrites oldest data and increments a `dropped_frames` counter (logged, surfaced as a warning if sustained).
- **Segmenter threads (2):** one per source. Each pulls frames from its ring buffer, runs VAD, and emits `Utterance` objects onto the shared bounded `utterance_queue` (`queue.Queue(maxsize=32)`).
- **Transcription worker (1 thread):** the GPU is a single serialized resource. One worker pulls from `utterance_queue` and runs faster-whisper. **Backpressure:** if the queue is full, segmenters block on `put()` — but since segments are only produced at speech boundaries and transcription is faster-than-realtime on GPU for `large-v3` with short utterances, this is rarely hit. If it is hit repeatedly (queue full > `backpressure_warn_seconds`), the app: (a) logs a warning, (b) surfaces a "falling behind" status dot, and (c) optionally auto-downgrades the model per settings (`auto_downgrade_on_backpressure`).
- **Translation + T1 diarization:** run inline on the transcription worker thread *or* on a small follow-on worker (`post_queue`, maxsize=32). Recommended: keep transcription worker lean (STT only) and run translate+diarize on a separate `post_worker` thread so GPU STT is never stalled by translation model inference. Both translation (NLLB) and diarization embeddings can share the GPU; serialize GPU access with a single `torch` device lock if VRAM is tight (see §11.5).
- **Persistence + broadcast:** the `post_worker` (or a dedicated `sink` thread) writes to `store.py` (append-only, fast) and publishes to an asyncio broadcast. Because FastAPI/WebSocket lives on the asyncio event loop and workers are threads, cross into the loop via `asyncio.run_coroutine_threadsafe(broadcast(msg), loop)` or an `asyncio.Queue` fed through `loop.call_soon_threadsafe`.
- **Bridge (threads ↔ asyncio):** the server holds a reference to the running event loop. Worker threads enqueue outgoing WS messages using `loop.call_soon_threadsafe(async_queue.put_nowait, msg)`. A single async task drains `async_queue` and fans out to all connected WS clients.
- **Ordering:** utterances may finish transcription slightly out of wall-clock order across sources (a long "Them" utterance vs a short "You" one). Each utterance carries `start`/`end` timestamps (seconds from session start). The UI orders by `start`. `store.py` appends in completion order to JSONL but each record has authoritative timestamps; the rendered `transcript.md` is sorted by `start` on finalize/close.

### 4.4 Timebase

A single monotonic session clock starts at capture start (`session_t0 = time.perf_counter()`). All utterance `start`/`end` are seconds relative to `session_t0`, derived from sample counts in each source's stream (sample_index / 16000) to keep audio and transcript aligned for tier-2 re-diarization. Capture start for both streams is recorded; a small per-stream offset (measured start delta) is stored so audio files and timestamps line up.

---

## 5. Components

Each subsection: **Responsibility · Public interface · Dependencies · Key algorithms / details.**

### 5.1 `capture.py` — audio capture

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
  - *soundcard path:* obtain loopback microphone for the default speaker via `soundcard.get_microphone(id=default_speaker.name, include_loopback=True)`; record with `.recorder(samplerate=native, channels=…, blocksize=…)`.
  - *pyaudiowpatch path:* use `get_default_wasapi_loopback()` to find the loopback device index; open an input stream on it. Open the mic as a normal WASAPI input stream.
- **Two independent streams.** Each runs in its own thread with its own recorder context manager. The loopback stream's native format is whatever the output mixer runs at (commonly 48 kHz stereo). The mic may be 44.1/48 kHz mono/stereo.
- **Downmix to mono.** Average channels (`pcm.mean(axis=1)`) if multi-channel.
- **Resample to 16 kHz.** Use a high-quality polyphase resampler. Preferred: `soxr.resample(x, in_rate, 16000)` (fast, good quality). Fallback: `scipy.signal.resample_poly` with computed up/down from `gcd(in_rate, 16000)`. Resampling is stateful across blocks: maintain per-stream filter state or use overlap; with `soxr` use a streaming `soxr.ResampleStream` to avoid block-edge artifacts.
- **Frame size.** Emit frames of a fixed hop (e.g. 20 ms = 320 samples @16 kHz) to feed VAD cleanly; internally read larger blocks from the device (e.g. 100–200 ms) and re-chunk.
- **Raw persistence.** As frames are produced (post-resample), also append them to `audio_you.wav` / `audio_them.wav` via `store.WavWriter` (16 kHz, mono, 16-bit PCM). This is required for tier-2 re-diarization (§5.5) and is always on while capturing (unless `persist_audio: false`, which disables tier-2).
- **Device-change handling.** WASAPI default-device changes (user switches headset, plugs in HDMI) invalidate a stream. Detect via: (a) recorder raising/returning an error, or (b) a periodic (every 2 s) check of the current default device id vs the one the stream was opened on. On change: stop the affected stream, attempt to reopen on the new default device (up to `device_reopen_retries`, default 5, with 500 ms backoff), emit `on_status(source, "reopened"|"lost")`. Do not touch the other stream. During a gap, VAD simply sees silence; the session continues.
- **Missing-device handling.** If loopback cannot be opened (no output device, or exclusive-mode conflict), start with only the mic and mark `them.available=false`. If the mic is missing or blocked (Windows privacy setting), start with only loopback and mark `you.available=false`. If **both** fail, `start()` returns an empty list; the server reports an error and does not enter the recording state. At least one source is required.
- **No exclusive mode.** Always open shared-mode WASAPI so we don't seize the device from the meeting app.

### 5.2 `segmenter.py` — VAD segmentation

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
- VAD is evaluated on fixed frames (Silero works on 30 ms / 512-sample windows @16 kHz; webrtcvad on 10/20/30 ms). Produce a per-frame speech probability (Silero) or boolean (webrtcvad, aggressiveness `vad_aggressiveness` 0–3, default 2).
- State machine per source:
  - **IDLE** → accumulate a short rolling pre-roll buffer (`pre_roll_ms`, default 300 ms) so we don't clip word onsets.
  - Transition to **SPEECH** when `speech` sustained for `speech_start_ms` (default 150 ms). Prepend pre-roll to the utterance.
  - In **SPEECH**, append frames. Track trailing silence.
  - End the utterance (**→ IDLE, emit**) when trailing silence exceeds `silence_end_ms` (default 600 ms) **or** the utterance reaches `max_utterance_seconds` (default 15 s — force a cut mid-speech to bound latency; the next chunk continues).
  - Discard utterances shorter than `min_speech_ms` (default 250 ms) as noise.
- **Max-length cut:** when forced, cut at the most recent low-energy frame within the last 500 ms if possible (avoid slicing mid-word); otherwise cut hard. Mark `forced_cut=true` internally (not persisted) so the transcriber can optionally overlap-pad.
- **Silero specifics.** Use the packaged VAD (`silero_vad` pip, or torch.hub). Keep the model on CPU (tiny) to leave GPU headroom for Whisper, unless `vad_device: "cuda"`. Reset internal RNN state between utterances.
- **webrtcvad fallback.** No probabilities — use a hangover counter (N consecutive speech/nonspeech frames) to emulate start/stop hysteresis.
- **Latency budget.** End-of-utterance latency ≈ `silence_end_ms` (0.6 s) + transcription time. This is the primary tunable for perceived responsiveness.
- **Two independent instances** ("you", "them") run concurrently and never share state.

### 5.3 `transcriber.py` — speech-to-text

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
- **Per-utterance transcription.** Call `model.transcribe(pcm, language=None or fixed, vad_filter=True, beam_size=…, temperature=[0.0, 0.2, 0.4], condition_on_previous_text=False)`.
  - `condition_on_previous_text=False` — critical: prevents cross-utterance hallucination drift and keeps utterances independent.
  - `vad_filter=True` — built-in Silero VAD as a **secondary** guard (segmenter already gated, but this trims residual silence inside the chunk). Configurable `whisper_vad_filter` (default true).
  - `language`: if `force_language` is set (e.g. user knows the meeting is Japanese), pass it; otherwise `None` for auto-detect. Auto-detect returns `info.language`, `info.language_probability`.
  - `beam_size`: default 5 (quality) but `beam_size=1` when in low-latency mode (`latency_mode: "fast"`).
- **Concatenate segments.** faster-whisper returns segment iterator; join `.text`, take earliest start / latest end, average `avg_logprob`, take max `no_speech_prob`.
- **Hallucination guards** (Whisper invents text like "Thank you." / "Thanks for watching" on silence/noise). Drop the transcript (return `None`) when ANY:
  - `no_speech_prob > no_speech_threshold` (default 0.6) AND `avg_logprob < logprob_drop_threshold` (default −1.0).
  - Text (after strip) matches a configurable **hallucination denylist** (case-insensitive, whole-utterance): `["thank you", "thanks for watching", "please subscribe", "つ", "ご視聴ありがとうございました", "字幕", ...]` — provide a default list in config, user-extendable.
  - Text is empty or only punctuation/whitespace.
  - Utterance audio RMS below `min_rms` (belt-and-suspenders vs VAD false-positives).
- **GPU OOM fallback.** Wrap load + transcribe in try/except for `RuntimeError`/CUDA OOM. On OOM: (1) `torch.cuda.empty_cache()`, (2) reload at the next smaller model (`large-v3`→`medium`→`small`), (3) if still failing, switch `compute_type` to `int8_float16` then `int8`. Emit a status event describing the downgrade. Persist the effective model in session metadata. Never crash the pipeline on OOM.
- **Threading.** Single transcription worker (the GPU is serialized). See §4.3.

### 5.4 `translator.py` — live translation

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
1. `translate_enabled` is true (Settings / "Dịch" toggle), AND
2. detected `lang != target_lang` (`target_lang` default `"vi"`), AND
3. `lang` ∈ `source_languages` (user-selected set of source languages to translate; empty set = "translate any non-target language").

Otherwise the utterance is passed through with `translation=None`.

**NLLB details.**
- Model: `facebook/nllb-200-distilled-600M`. Load once; keep on GPU (`device="cuda"`) if VRAM allows, else CPU (still usable for short text). `compute_type` int8 on CTranslate2 for speed.
- **Language code mapping (Whisper ISO-639-1 → NLLB FLORES code).** Maintain a dict; ship at least:
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
  Target `vi` → `vie_Latn`. If a detected `lang` has no mapping, skip translation (pass through) and log once.
- Inference: set `tokenizer.src_lang = <nllb_src>`, generate with `forced_bos_token_id = tokenizer.convert_tokens_to_ids(<nllb_tgt>)`, `max_length` scaled to input length, `num_beams` default 3.
- **Quality note (must appear in code comment + docs):** NLLB Japanese→Vietnamese quality is *moderate*; for higher-quality JA→VI (or other hard pairs), the user can switch `translation_provider` to `gemini`, which improves results at the cost of sending text to Google. This is a deliberate quality/privacy tradeoff exposed in Settings.

**GeminiTranslator (stub).** Implements the same interface. `available()` returns true only if `gemini_api_key` is set. `translate()` calls the Gemini API with a terse system prompt ("Translate the following {src} text to Vietnamese. Output only the translation."). Off by default. If selected but no key, `available()` is false → UI shows a clear "set your Gemini key" message and falls back to NLLB (or no translation).

**Error handling.** Any translation exception → return original text unchanged is **wrong** (would look like a translation). Instead return `None`/empty and mark `translation_error` on the record; the UI shows the original with a small "translation failed" hint. Never block the pipeline.

### 5.5 `diarizer.py` — speaker diarization (two tiers)

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

**Tier 1 — real-time online clustering.**
- **"You" (mic source):** always labelled `"You"`. No embedding needed.
- **"Them" (loopback):** for each utterance, extract a fixed-dim speaker embedding and assign it online.
  - **Embedding model:** ECAPA-TDNN via SpeechBrain (`speechbrain/spkrec-ecapa-voxceleb`) — preferred. Fallback: Resemblyzer (`VoiceEncoder`) — lighter, CPU-friendly. Selectable via `diarization_embedder: "ecapa"|"resemblyzer"`.
  - **Online clustering algorithm:** maintain a list of centroids `{label: (mean_embedding, count)}`.
    1. Compute embedding `e` (L2-normalized) for the utterance.
    2. If utterance shorter than `min_embed_ms` (default 800 ms), skip clustering and label `"Speaker ?"` (uncertain) — too short for a reliable embedding.
    3. Find nearest centroid by cosine similarity. If `max_sim >= sim_threshold` (default 0.70) assign that label and update the centroid as a running mean (weighted by count, capped so it stays adaptive).
    4. Else create a new label `"Speaker {n+1}"` with `e` as its centroid.
  - **Cap** the number of speakers at `max_speakers` (default 8); beyond that, force-assign to nearest to avoid runaway label creation on noisy embeddings.
  - **Renaming:** `rename()` updates the label map; new incoming utterances that match the centroid keep the new name. Renames also propagate to already-emitted records via a store update (§5.7) and a WS `rename` event.
- **Toggle / cost:** controlled by `diarization_enabled` (Settings, default **ON**) and `diarization_realtime` (default ON). When realtime diarization is off, all "Them" utterances are labelled `"Them"` (single bucket). The embedding model adds VRAM (~1 GB ECAPA on GPU) and per-utterance latency (~50–150 ms). If VRAM is tight, run the embedder on CPU (`diarization_device: "cpu"`) — slower but non-blocking to Whisper. Document this VRAM/latency tradeoff in Settings help text.

**Tier 2 — offline accurate re-diarization.**
- Triggered by the UI "Re-diarize" button → REST → runs `OfflineDiarizer.rediarize(session_dir)`.
- **Requires** the persisted audio. Re-diarize primarily on `audio_them.wav` (remote speakers) — "You" stays "You". Optionally also process a **mixed** track if desired, but v1: run pyannote on `audio_them.wav` only, keep mic as "You".
- **Model:** `pyannote/speaker-diarization-3.1` pipeline via `pyannote.audio`. `Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)`. Run on GPU (`pipeline.to(torch.device("cuda"))`).
- **HuggingFace token** (free) required; user accepts the model's gated terms once on HF and pastes the token into Settings (`hf_token`). If missing, tier-2 is **disabled** and the button shows a clear message + link to instructions. See §6 & §8.
- **Relabelling algorithm:** pyannote returns speaker-homogeneous time spans with cluster ids (SPEAKER_00, …). For each existing "Them" utterance record, compute the overlap-weighted majority pyannote speaker over `[start, end]` and assign a stable label (`"Speaker A/B/…"`). Preserve any user renames by offering a mapping step (v1: apply pyannote clusters as fresh labels, but keep a `diarization_source` field so the UI shows "re-diarized" state). Rewrite `transcript.jsonl` speaker fields (new file version) and re-render `transcript.md`; keep a backup `transcript.jsonl.pre-rediarize`.
- Long-running: runs in a background thread; report progress/completion via WS + REST status.

### 5.6 `summarizer.py` — AI summarization

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

**Input assembly.** Build a plain-text transcript from `transcript.jsonl`, one line per utterance: `"[mm:ss] <SpeakerLabel>: <text>"`, using the translated Vietnamese where translation exists (configurable `summary_use_translation`, default true) else original. Include a short header with meeting title/date/duration/languages. If the transcript is very long, chunk to fit context (`summary_max_chars`, default 48000) — map-reduce: summarize chunks then summarize the summaries. Document the chunking.

**Prompt (default).** The prompt must let the model **self-organize** into whatever sections fit the content — do not hard-force a fixed template. Ship this default (Vietnamese-first output):
```
You are an assistant that writes clear, well-structured meeting notes in Vietnamese.
Read the transcript below (speaker-labelled). Produce concise notes in Markdown.
Organize into whatever sections actually fit this meeting — for example (only if relevant):
key points, decisions, action items (with owner if stated), open questions / Q&A,
risks or warnings, and misc notes. Omit sections that don't apply. Do not invent content
not supported by the transcript. Keep it skimmable.

TRANSCRIPT:
<transcript_text>
```
Prompt text lives in config (`summary_prompt`) and is user-editable.

**Provider implementations.**
- **ClaudeCliSummarizer (default):** shell out headless: `claude -p "<prompt+transcript>"` (or pass the prompt via stdin to avoid arg-length limits — **preferred**: `claude -p` reading prompt from stdin / a temp prompt file). Capture stdout as the markdown. Use `subprocess.run([...], capture_output=True, text=True, timeout=summary_timeout_s, encoding="utf-8")`.
- **CodexCliSummarizer:** `codex exec` with the prompt (via stdin/temp file), capture stdout.
- **GeminiSummarizer:** call Gemini API with the prompt (needs `gemini_api_key`).
- **OllamaSummarizer:** POST to local Ollama (`http://localhost:11434/api/generate`, model `ollama_model` default `llama3.1`), stream/collect `response`.

**"CLI not installed" / unavailable handling.** `available()` probes: for CLI providers, check the binary exists (`shutil.which("claude")` / `"codex"`) and optionally a `--version`. If missing, `summarize()` is not called; UI shows: *"Claude CLI not found. Install it, or choose another summarizer (Codex / Gemini / Ollama) in Settings."* Same for `codex`, for a missing Ollama server (connection refused), and for a missing Gemini key. Never crash; return a clear error string that the UI renders as a notice (not saved as `summary.md`).

**Output.** On success, save markdown to `<session_dir>/summary.md` (overwrite allowed; keep a `.bak` of the previous). Return it over REST/WS so the UI shows it. Record `summary_provider` and `summarized_at` in session metadata.

**Windows subprocess notes.** Use `text=True, encoding="utf-8"`; set `creationflags=subprocess.CREATE_NO_WINDOW` so no console flashes. Prefer passing the prompt via a temp file path or stdin (arg length + quoting on Windows is fragile for long transcripts).

### 5.7 `store.py` — session storage

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
- `session_id` = folder name = `<YYYYMMDD-HHMMSS>-<slug(title)>`. Title default = "meeting"; slug = lowercased, non-alnum→`-`, trimmed, max 40 chars.
- `sessions_root` default `./sessions/` under the app data dir (see §10).

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
  "text": "会議を始めましょう。",     // original transcription
  "lang": "ja",                    // detected language (ISO-639-1)
  "lang_prob": 0.98,
  "translation": "Hãy bắt đầu cuộc họp.",  // Vietnamese, or null if not translated
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
**[14:39:12] Speaker 2 (ja):** 会議を始めましょう。
> Hãy bắt đầu cuộc họp.
```
(Translation line only when present. Timestamp shown as wall-clock derived from `created_at`/`start`.)

**Renames.** `rename_speaker` updates all matching records' `speaker` in `transcript.jsonl` (rewrite the file atomically: write to temp, `os.replace`), updates `meta.json.speakers`, and re-renders `transcript.md`. Returns count updated.

### 5.8 `server.py` — FastAPI backend

**Responsibility.** Host the HTTP API + WebSocket, own the pipeline lifecycle, serve the web UI, bridge worker threads to the event loop.

**Dependencies.** `fastapi`, `uvicorn`, `pydantic`, the components above, `config.Settings`.

**Server config.** Bind `127.0.0.1` on a fixed default port `8848` (configurable `server_port`); if occupied, try next 10 ports and report the chosen one to pywebview. CORS locked to localhost. No auth (localhost-only, single user).

**REST endpoints.**
| Method | Path | Body / Params | Returns |
|-------|------|---------------|---------|
| `POST` | `/api/capture/start` | `{title?: str}` | `{session_id, sources: {you, them}}` — starts pipeline; errors if both sources fail |
| `POST` | `/api/capture/stop` | — | `{session_id, finalized: true}` — stops capture, finalizes session |
| `GET` | `/api/capture/status` | — | `{recording, session_id, sources, dropped_frames, backpressure, model}` |
| `GET` | `/api/sessions` | — | `[SessionMeta]` (list, newest first) |
| `GET` | `/api/sessions/{id}` | — | full session: meta + utterances (+ summary if present) |
| `POST` | `/api/sessions/{id}/summarize` | `{provider?}` | `{markdown}` or `{error}` (runs summarizer) |
| `POST` | `/api/sessions/{id}/rediarize` | — | `{status:"started"}`; completion via WS |
| `POST` | `/api/sessions/{id}/speakers/rename` | `{old, new}` | `{updated: n}` |
| `GET` | `/api/settings` | — | full settings object |
| `PUT` | `/api/settings` | partial settings | updated settings (validated) |
| `GET` | `/api/health` | — | `{ok, gpu, cuda, models_loaded}` |
| `GET` | `/` and `/static/*` | — | serves `web/` UI |

**WebSocket** `GET /ws`:
- Server → client message types:
  - `{"type":"utterance", "record": <UtteranceRecord>}` — a new finalized, translated, diarized utterance.
  - `{"type":"status", "recording":bool, "sources":{…}, "backpressure":bool, "model":str, "note":str}`.
  - `{"type":"rename", "old":str, "new":str}`.
  - `{"type":"rediarize", "state":"started|progress|done|error", "detail":…}`.
  - `{"type":"summary", "state":"started|done|error", "markdown"?:str, "error"?:str}`.
  - `{"type":"error", "code":str, "message":str}` (device lost, OOM downgrade, etc.).
- Multiple clients (compact + expanded can both be open, or reconnect) supported; broadcast to all. On connect, server sends current status + optionally the last N utterances of the active session.

**Lifecycle.** `start` builds ring buffers, WAV writers, segmenters, transcriber (loads model), translator/diarizer (lazily), starts capture + worker threads, creates the session in `store`. `stop` signals `stop_event`, joins threads (with timeout), flushes/closes WAVs, `finalize()`s the session. The pipeline objects are held on an app-state singleton.

**Bridge.** Server stores `self.loop = asyncio.get_running_loop()` at startup; worker threads push WS messages via `loop.call_soon_threadsafe`. See §4.3.

### 5.9 `web/` — front-end UI (served by FastAPI, wrapped in pywebview)

**Responsibility.** Present the compact bar and expanded window; connect to `/ws`; call REST; render live transcript, translation, speaker labels; expose settings and actions.

**Tech.** Plain, dependency-light: a single-page app in vanilla JS + modern CSS (or a tiny framework like Preact/Alpine if bundled locally — **no CDN**, everything served from `web/`). No build step required for v1 (ship static files). Two logical views toggle within one page (compact ↔ expanded) so a single pywebview window resizes rather than opening a second window.

**pywebview host.** Frameless (`frameless=True`), always-on-top (`on_top=True`), small default size (compact ≈ 460×150). `resizable=True`. Custom drag region (a top strip with `-webkit-app-region: drag` semantics — for pywebview implement drag via a JS `pywebview.api` call or a draggable header using `window.moveTo`, or set `easy_drag`). Provide window controls (close, minimize) since the frame is hidden. Expand button resizes the window (≈ 900×640) and switches to the expanded layout; collapse returns to compact.

**Compact bar (default).**
- Left: **Start/Stop** button (turns red "● Stop" while recording).
- A **status dot**: grey (idle) / green (recording, healthy) / amber (falling behind / one source only) / red (error). Tooltip shows detail.
- The **2–3 most recent** transcript lines, each: `Speaker: original` and, if translated, an inline muted Vietnamese line beneath.
- A **"Dịch"** (translate) on/off toggle switch.
- An **expand** button (⤢).
- Minimal chrome; content updates live via WS.

**Expanded window.**
- **Header:** title (editable), recording controls, status dot, search box, expand/collapse, settings gear.
- **Transcript pane:** full scrolling list. Each utterance row: timestamp · speaker label (click to rename inline) · original text · translation (muted, below). Auto-scroll to bottom unless the user has scrolled up (then show a "jump to latest" pill). Source visually distinguished (You vs Them — e.g. subtle left border color).
- **Search box:** filters/highlights utterances by substring across original + translation + speaker.
- **Settings panel** (drawer/modal), fields (bound to `/api/settings`):
  - Whisper model size (select: small / medium / large-v2 / large-v3).
  - Translate on/off (mirrors "Dịch").
  - Source-language selection (multi-select chips: ja, en, zh, ko, fr, …; empty = any).
  - Translation provider (select: NLLB / Gemini).
  - Summarizer provider (select: Claude CLI / Codex CLI / Gemini / Ollama).
  - Diarization on/off; realtime diarization on/off; diarization device (GPU/CPU).
  - HF token field (for tier-2), Gemini key field (masked). Never echo secrets back in full; show `••••` with a "change" affordance.
  - Legal & Consent link (reopens §3 text).
- **Summarize button:** calls `/api/sessions/{id}/summarize`; shows spinner then renders the returned structured Markdown (in a panel with copy button). If provider unavailable, shows the graceful notice.
- **Re-diarize button:** calls `/api/sessions/{id}/rediarize`; shows progress; on done, transcript relabels live.
- **Sessions list:** open past sessions (read-only view + summarize/re-diarize).
- **Speaker rename:** inline edit on any label → `POST …/speakers/rename` → live update everywhere.

**Aesthetic.** Clean, modern, minimal — **not "AI slop."** Concretely: one restrained accent color; system font stack (Segoe UI / Inter if bundled locally); generous line-height and spacing; muted secondary text for translations/timestamps; subtle borders not heavy shadows; no gradients-for-gradient's-sake, no emoji-spam, no purple-glow. Light and dark mode via `prefers-color-scheme`. Fast, no layout jank on new utterances (virtualize or cap DOM nodes if the transcript is very long).

### 5.10 `config.py` — settings

**Responsibility.** Define, load, validate, persist all settings as JSON. See full reference in §7.

**Public interface.**
```python
class Settings(BaseModel):     # pydantic
    ...  # all fields in §7 with defaults + validators
    @classmethod
    def load(cls, path: str) -> "Settings"
    def save(self, path: str) -> None
    def update(self, partial: dict) -> "Settings"   # validate + persist
```
- File location: app data dir `settings.json` (see §10). If absent, created with defaults. Unknown keys ignored with a warning; invalid values rejected (422 on the API). Secrets (`hf_token`, `gemini_api_key`) stored in the same JSON on the local machine (single-user desktop); documented as plaintext-at-rest (v1 acceptable; note as a hardening item). Never log secret values.

---

## 6. Data Model

Covered inline in §5.7 (JSONL utterance schema, `meta.json`, session folder layout, `transcript.md` format). Summary of persisted artifacts per session: `meta.json`, `transcript.jsonl` (authoritative append-only), `transcript.md` (rendered), `summary.md` (on demand), `audio_you.wav` / `audio_them.wav` (16 kHz mono PCM16, required for tier-2), plus backups on rewrite. Schema versioning via the `schema` integer on records and meta.

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
| `vad_aggressiveness` | int | `2` | webrtcvad only, 0–3. |
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
| `hallucination_denylist` | list[str] | (see §5.3) | Whole-utterance texts to drop. |
| `auto_downgrade_on_backpressure` | bool | `false` | Auto-shrink model if falling behind. |
| `backpressure_warn_seconds` | int | `8` | Sustained backlog → warn. |
| `translate_enabled` | bool | `false` | Live translation on/off ("Dịch"). |
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
| `summary_prompt` | str | (see §5.6) | Editable summarization prompt. |
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

1. **No loopback device / output missing:** start with mic only, `them.available=false`, status dot amber, note "Recording microphone only — no system audio device." Transcript proceeds for "You".
2. **Mic blocked / missing (Windows privacy or no input device):** start with loopback only, `you.available=false`, note "Recording system audio only — microphone unavailable (check Windows mic privacy)." 
3. **Both sources fail:** `start` returns error; do not enter recording state; UI shows actionable message (check audio devices).
4. **Device change mid-session:** auto-reopen with retries (§5.1); brief gap tolerated; note on lost/reopened.
5. **GPU OOM (load or inference):** empty cache → downgrade model → downgrade compute_type (float16→int8_float16→int8) (§5.3). Emit note describing the effective config. Persist to meta.
6. **Whisper hallucination on silence:** prevented by VAD gating + `min_speech_ms` + `no_speech_prob`/`avg_logprob` thresholds + denylist + `min_rms` (§5.3). Dropped utterances are silently not emitted (optionally logged at debug).
7. **Translation failure / unmapped language:** pass through original, mark `translation_error`/`translation=null`; UI shows a subtle "translation failed" hint. Unmapped language → skip translation, log once.
8. **Summarizer CLI not installed / provider unavailable:** `available()` false → graceful message naming the missing tool and suggesting an alternative provider; no `summary.md` written.
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
- **Translator via mocks:** `NllbTranslator` with the model call mocked — assert lang-code mapping (ja→jpn_Jpan, etc.), the gating logic (only translates when enabled ∧ lang≠target ∧ lang∈source set), and unmapped-language pass-through. `GeminiTranslator.available()` with/without key.
- **Summarizer via mocks:** patch `subprocess.run` / API client. Assert prompt assembly (timestamps, speaker labels, translation-vs-original selection), chunking above `summary_max_chars`, `available()` detection of missing binary (`shutil.which` mocked), graceful error string on non-zero exit / timeout.
- **Store round-trip:** create session → append N `UtteranceRecord`s → read back JSONL → assert equality; rename_speaker updates all matching records + meta + md; finalize sorts by start; partial-trailing-line tolerance; WAV writer produces valid 16 kHz mono PCM16 (read back with soundfile, check frames).
- **Diarizer online-clustering logic:** with a **fake embedder** returning controlled vectors — assert: identical vectors cluster together, distant vectors create new speakers, `sim_threshold` boundary behavior, `min_embed_ms` short-utterance skip, `max_speakers` cap, rename propagation, "You" always for mic.
- **Config:** load/save round-trip, defaults, validator rejection of bad enums/ranges, unknown-key tolerance, secret non-logging.

### 9.2 Integration tests (`tests/integration/`)
- **Pipeline on a known WAV:** replace live capture with a `FileCaptureSource` that streams a bundled short WAV (with real speech + a silent gap) through the *real* segmenter and a *real or small* Whisper model (use `small`, `compute_type=int8`, CPU allowed in CI-lite; mark GPU-only tests to skip without CUDA). Assert: non-empty transcript, ≥1 utterance, files written (`transcript.jsonl`, `transcript.md`, `meta.json`), records well-formed. Translation + diarization can be mocked or run with tiny inputs.
- **Server API:** FastAPI `TestClient` — settings get/put validation, sessions list/open, summarize with a mocked provider returns markdown + writes `summary.md`, rename endpoint. WebSocket receives an `utterance` message when the pipeline (driven by the file source) emits one.

### 9.3 Manual smoke test (user step; documented, not automatable here)
- Run one real online meeting. Verify: both "You" and "Them" audio captured; live transcript + translation appear with low latency; speaker labels look sane and are renameable; stop → summarize (Claude CLI) → structured notes; Re-diarize (with HF token) relabels cleanly; session folder contains all artifacts.
- **Explicitly NOT auto-testable here** and called out in the test docs: (a) live WASAPI loopback capture on real hardware, (b) real-time GPU performance / latency under `large-v3`, (c) device-change hot-swap, (d) actual translation/summary quality. These require the user's machine + a real meeting.

### 9.4 Tooling
`pytest`, `pytest-asyncio` (WS/async), `numpy` for synthetic PCM, `soundfile` for WAV assertions. GPU/model-heavy tests marked `@pytest.mark.gpu` and skipped when CUDA/model absent.

---

## 10. Dependencies & Environment

### 10.1 Assumptions
- Windows 11 (WASAPI loopback is **Windows-specific** — the app does not run on macOS/Linux in v1).
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
- NLLB-200 distilled-600M: ~2.5 GB files, ~1.5–2 GB VRAM (or CPU).
- ECAPA-TDNN (SpeechBrain): ~80 MB, ~1 GB VRAM on GPU.
- pyannote 3.1 pipeline: ~30–100 MB, gated (needs HF token + accept terms).
Document total first-run download (~5–7 GB) and that subsequent runs are offline-capable.

### 10.4 VRAM budget guidance
Rough concurrent GPU residency with defaults: Whisper large-v3 (~3 GB) + NLLB (~1.5 GB) + ECAPA (~1 GB) ≈ 5.5 GB. On <8 GB cards, recommend either `whisper_model=medium`, `translation_device=cpu`, or `diarization_device=cpu`. Provide these as documented Settings knobs (§7). A single `torch` device lock serializes GPU submissions to avoid fragmentation spikes.

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

- **M0 — Skeleton:** repo layout, `config.py`, FastAPI server with `/health` + static UI shell, pywebview window opens. (No audio.)
- **M1 — Capture:** `capture.py` dual WASAPI streams + resample + ring buffers + WAV persistence; status/device-missing handling; a debug endpoint showing RMS levels. Manual verify audio flows.
- **M2 — Segment + Transcribe:** `segmenter.py` + `transcriber.py`; live utterances pushed over WS; compact bar shows transcript. Hallucination guards. This is the first end-to-end "it transcribes a meeting" milestone.
- **M3 — Storage + Sessions:** `store.py` autosave JSONL/MD/WAV, sessions list/open, `transcript.md` finalize. Crash-safety verified.
- **M4 — Translation:** `translator.py` NLLB + gating + "Dịch" toggle + inline translation in UI + Gemini stub.
- **M5 — Diarization T1:** real-time embedding + online clustering + renameable labels.
- **M6 — Summarize:** `summarizer.py` Claude CLI default + provider selection + summary panel + graceful missing-CLI.
- **M7 — Diarization T2:** pyannote re-diarize button + HF token flow + transcript relabel + backups.
- **M8 — Expanded UI + polish:** full transcript view, search, settings panel, theme, design pass (anti-slop), error surfacing.
- **M9 — Tests + docs:** unit + integration suites, README, first-run consent modal, manual smoke test checklist.
- **Future (out of v1):** `.exe` packaging (PyInstaller), overlapping-speech separation, app-data-dir migration, per-tenant model presets.

---

## 13. Risks & Open Questions

- **Loopback library reliability.** `soundcard` loopback on Windows can be finicky across driver/format combos; `pyaudiowpatch` is the fallback but has its own device-index quirks. Risk: some machines only work on one backend. Mitigation: `audio_backend` override + robust device probing. Needs real-hardware validation (not auto-testable).
- **Real-time performance under large-v3.** On modest GPUs, `large-v3` per-utterance latency may exceed comfort; backpressure/auto-downgrade helps but the default may need to be `medium` for some users. Open: pick default per detected VRAM at first run?
- **Concurrent GPU residency.** Whisper + NLLB + ECAPA + (occasionally) pyannote can exceed 8 GB VRAM. Mitigated by CPU-offload knobs, but the *default* config assumes ~8–12 GB. Open: dynamic device placement based on detected VRAM.
- **NLLB JA→VI quality.** Explicitly moderate; Gemini improves it but breaks the "fully local" promise. This is a documented tradeoff, not a bug.
- **Diarization accuracy on short utterances / overlapping speech.** Tier-1 online clustering will mislabel short/overlapping segments; tier-2 pyannote is the corrective, but overlapping speech remains a known v1 limitation (non-goal).
- **Timestamp alignment for tier-2.** Utterance timestamps must map accurately onto `audio_them.wav` sample positions for pyannote overlap-majority relabelling. Any drift between capture start and the session clock, or resampler latency, could misalign. Mitigation: derive timestamps from cumulative sample counts of the persisted stream, not wall-clock.
- **Secrets at rest.** `hf_token` / `gemini_api_key` stored plaintext in `settings.json` (single-user desktop). Acceptable v1; hardening (OS keyring) noted as future.
- **CLI summarizer variability.** `claude -p` / `codex exec` output format and flags can change; long transcripts via arg vs stdin. Mitigation: prefer stdin/temp-file, timeout, and treat output as opaque markdown.
- **Legal exposure.** The core function (recording without a platform indicator) is legally sensitive; the consent gate + honest framing (§3) are mandatory, but ultimately the user bears compliance responsibility.
- **pywebview frameless drag/controls on Windows.** Custom window chrome (drag region, always-on-top toggling) can be fiddly with the EdgeChromium backend. Needs verification; fallback to a thin native title bar if frameless drag proves unstable.

---

*End of specification.*
