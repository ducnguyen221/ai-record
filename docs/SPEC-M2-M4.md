# ai-record — Spec Addendum for M2–M4 (+ user emphases)

Extends `SPEC.md` v2. Where this conflicts, THIS wins for M2–M4 scope. Pins the API contract so the backend track and the UI track build to the same target.

---

## E1. Standalone / Dictation capture (user emphasis #1) — MUST

Capture is **not** meeting-dependent. When recording is ON, ai-record captures **both** the microphone (system audio *input*) and the WASAPI loopback (system audio *output*) **regardless of whether any meeting app (Teams/Zoom/…) is running**.

- Primary non-meeting use case: **voice dictation** — the user speaks long-form (e.g. explaining a lengthy requirement) and it is transcribed and saved as text for later. This must work with mic alone.
- `them` (loopback) is optional; a **mic-only** session is first-class. `you` (mic) is optional too; a loopback-only session is valid. At least one source required (already in M1).
- Add a session **`mode`** hint in `meta.json`: `"meeting" | "dictation"` (default `"meeting"`; `"dictation"` when the user starts a mic-only quick session). Purely informational; does not change the pipeline.
- UI must make clear the app records standalone; provide a **source choice** at start: *Both (meeting)* / *Mic only (dictation)* / *System only*. Do not imply a meeting is required anywhere.

## E2. Summarization scenarios (user emphasis #2) — MUST

`summarizer.summarize()` gains a **`scenario`** parameter. Each scenario is a named, config-editable prompt template. Ship these 5; default = **`reformat`**.

1. **`reformat` (DEFAULT) — lossless structuring.** Do **NOT** change, add, remove, translate, paraphrase, or reorder the wording of any utterance. ONLY: (a) group related/consecutive utterances into thematic sections, (b) add Markdown structure — `##` topic headings, bullet lists, preserved `Speaker + [timestamp]` labels. Every original utterance's text must still appear **verbatim** in the output.
   - **Integrity guard (required):** after the LLM returns, verify each original utterance `text` is present as a substring (normalized whitespace) in the output. If any is missing → the reformat is rejected and the app falls back to a **deterministic** reformatter (group by speaker + time-gap paragraphs, add date/section headers, no text alteration) and flags `reformat_fallback=true`. This guarantees "no content changed" even if the model misbehaves.
2. **`minutes` — meeting minutes / summary.** AI reads & understands the whole transcript, groups by context, and writes concise **meeting minutes**: overview, key discussion points, decisions, action items (with owner if stated), open questions/risks. Lossy summary, Vietnamese-first.
3. **`study_notes` — NotebookLM-style study notes.** Restructure into learning material: key concepts & definitions, a short Q&A / flashcard list, "things to remember", grouped by topic. For self-study.
4. **`action_tracker` — tasks & decisions only (creative extra).** Extract just a checklist of action items, decisions, owners, and deadlines/follow-ups; nothing else.
5. **`article` — narrative rewrite (creative extra).** Rewrite the discussion into a clean, readable article/blog post explaining what was covered, in flowing prose.

- All scenario prompts live in config (`summary_scenarios: {name: prompt}`), user-editable. Output always Markdown → `summary.md`; `meta.json` records `summary_scenario` + `summary_provider` + `summarized_at`.
- Providers unchanged (Claude CLI default, hardened per SPEC §5.6). For `reformat`, the deterministic fallback needs no LLM at all.

## E3. Export / download (user emphasis #3) — MUST

Downloadable, **agent-readable** exports of transcript and summary.

- **`.md` (rich, agent-optimized):** YAML front-matter (session_id, title, date, duration, sources, languages, model) + a clean body: `## Transcript` with one block per utterance `**[hh:mm:ss] Speaker (lang):** text` and translation as a `> ` quote line when present. Consistent, parseable structure so an LLM/agent can ingest it directly.
- **`.txt` (plain):** same information, no Markdown markup, `[hh:mm:ss] Speaker: text` lines; translation on the next indented line.
- Export **transcript** and **summary** independently, and a **combined** transcript+summary `.md`.
- Files served with `Content-Disposition: attachment` and a sensible filename (`<session_id>-transcript.md`, etc.).

---

## E4. API contract (PINNED — both tracks build to this)

All endpoints require the per-launch token + Origin check (as M1). `{sid}` is validated by the M1 confinement rules.

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| `PUT` | `/api/settings` | partial settings (redacted secrets) | updated redacted settings |
| `POST` | `/api/capture/start` | `{title?, mode?: "meeting"\|"dictation", sources?: ["you","them"]}` | `{session_id, sources}` |
| `POST` | `/api/sessions/{sid}/summarize` | `{scenario?: one of the 5, provider?}` | `{markdown, scenario, provider, reformat_fallback?}` + saves `summary.md` |
| `GET` | `/api/sessions/{sid}/summary` | — | `{markdown, scenario, summarized_at}` or 404 |
| `GET` | `/api/sessions/{sid}/export` | `?what=transcript\|summary\|combined&fmt=md\|txt` | file download (attachment) |
| `POST` | `/api/sessions/{sid}/rediarize` | — | `{status:"started"}` then progress via WS; relabels transcript (SPEC §5.5 tier-2) |
| `GET` | `/api/sessions/{sid}/rediarize/status` | — | `{state, progress}` |
| `POST` | `/api/sessions/{sid}/speakers/rename` | `{old, new}` | `{updated_count}` |
| `GET` | `/api/languages` | — | supported source languages for translation UI |

**WebSocket** (additions to M1): after STT emits an utterance, late results arrive as **patch** messages the UI must apply by `seq`:
```json
{"type":"patch","seq":123,"translation":"…","translation_provider":"nllb","speaker":"Speaker 2","diarization_confidence":0.82}
```
`translation` and `speaker` may arrive in separate patches. Status/degraded-mode messages unchanged.

**Translation settings** (drive M2): `translate_enabled` (bool), `target_lang` (default `"vi"`), `source_languages` (list; empty = any non-target). Translate an utterance only when enabled AND `lang != target` AND (`source_languages` empty OR `lang ∈ source_languages`) AND lang detection confident enough (SPEC §5.4 latency/staleness budget applies).

---

## E5. Milestone mapping (unchanged features, now concrete)

- **M2** — `translator.py` (NLLB CT2 int8 on CPU per `gpu_12gb` preset) wired into the STT-first pipeline as an async patch; `patch` WS; translate settings + `/api/languages`; UI inline translation + Dịch toggle + source-language picker.
- **M3** — `diarizer.py` tier-1 realtime (Resemblyzer CPU default) with confidence/unknown per SPEC §5.5; speaker patch + rename endpoint; UI speaker labels + rename.
- **M4** — `diarizer.py` tier-2 offline (pyannote) + `/rediarize`; `summarizer.py` with the 5 scenarios + integrity guard + deterministic reformat fallback; export module + endpoints; delete/retention (SPEC §5.7); UI summarize panel (scenario+provider), download buttons, re-diarize button.

Each module keeps SPEC's interfaces. Tests must stay green on CPU with no GPU/audio/model/HF-token (mock heavy deps; for pyannote/summarizer providers, inject fakes).
