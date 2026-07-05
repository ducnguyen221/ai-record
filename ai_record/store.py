"""Durable, crash-safe session storage (SPEC.md §5.7, schema 2).

Uses the stdlib ``wave`` module for all WAV I/O (16 kHz mono PCM16) so the module
is import-safe with no third-party audio deps. Persistence is append-only for
utterances (fast) with atomic temp+``os.replace`` rewrites for renames/patches, a
per-session reader/writer lock, crash-safe rolling per-minute raw segments, and
incomplete-session recovery.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

log = logging.getLogger("ai_record.store")

SAMPLE_RATE = 16000
SCHEMA = 2


# --------------------------------------------------------------------------- #
# WAV helpers (stdlib wave, PCM16 mono)
# --------------------------------------------------------------------------- #
def _float_to_pcm16(pcm: np.ndarray) -> bytes:
    clipped = np.clip(np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _pcm16_to_float(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def read_wav_mono16k(path: str | os.PathLike[str]) -> np.ndarray:
    """Read a mono 16 kHz PCM16 WAV into float32. Returns empty array if absent."""
    p = Path(path)
    if not p.exists():
        return np.empty(0, dtype=np.float32)
    with contextlib.closing(wave.open(str(p), "rb")) as wf:
        frames = wf.readframes(wf.getnframes())
    return _pcm16_to_float(frames)


class WavWriter:
    """Streaming PCM16 mono WAV writer with a valid header on close (SPEC.md §5.7)."""

    def __init__(self, path: str | os.PathLike[str], samplerate: int = SAMPLE_RATE, channels: int = 1) -> None:
        self.path = str(path)
        self._wf = wave.open(self.path, "wb")
        self._wf.setnchannels(channels)
        self._wf.setsampwidth(2)
        self._wf.setframerate(samplerate)
        self._closed = False

    def write(self, pcm: np.ndarray) -> None:
        if self._closed:
            raise ValueError("write after close")
        self._wf.writeframes(_float_to_pcm16(pcm))

    def close(self) -> None:
        if not self._closed:
            self._wf.close()
            self._closed = True


class RawSegmentWriter:
    """Crash-safe rolling per-minute WAV segments + samples.idx sidecar (SPEC.md §5.1)."""

    def __init__(self, session_dir: str | os.PathLike[str], source: str, seconds: int = 60) -> None:
        self.dir = Path(session_dir)
        self.source = source
        self.seg_samples = max(1, int(seconds * SAMPLE_RATE))
        self._idx_path = self.dir / "samples.idx"
        self._seg_index = 0
        self._seg_written = 0
        self._writer: WavWriter | None = None
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    def _seg_path(self, i: int) -> Path:
        return self.dir / f"audio_{self.source}.{i:03d}.wav"

    def _append_idx(self, entry: dict[str, Any]) -> None:
        with self._idx_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()

    def mark_epoch(self, epoch_id: int, wall_iso: str, cum_sample: int) -> None:
        self._append_idx(
            {"kind": "epoch", "source": self.source, "epoch_id": epoch_id,
             "wall_open": wall_iso, "cum_sample": cum_sample}
        )

    def _open_segment(self, cum_sample: int) -> None:
        path = self._seg_path(self._seg_index)
        self._writer = WavWriter(path)
        self._seg_written = 0
        self._append_idx(
            {"kind": "segment", "source": self.source, "segment": self._seg_index,
             "start_cum_sample": cum_sample, "wall": _now_iso()}
        )

    def write(self, pcm: np.ndarray, cum_sample: int, epoch_id: int) -> None:
        data = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1)
        with self._lock:
            if self._writer is None:
                self._open_segment(cum_sample)
            offset = 0
            while offset < data.size:
                room = self.seg_samples - self._seg_written
                chunk = data[offset:offset + room]
                assert self._writer is not None
                self._writer.write(chunk)
                self._seg_written += chunk.size
                offset += chunk.size
                if self._seg_written >= self.seg_samples:
                    self._writer.close()
                    self._seg_index += 1
                    self._open_segment(cum_sample + offset)

    def close_and_concat(self) -> str:
        """Close the current segment and concatenate all segments → canonical WAV."""
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
            canonical = self.dir / f"audio_{self.source}.wav"
            writer = WavWriter(canonical)
            for i in range(self._seg_index + 1):
                seg = self._seg_path(i)
                if seg.exists():
                    writer.write(read_wav_mono16k(seg))
            writer.close()
            return str(canonical)


def concat_segments(session_dir: str | os.PathLike[str], source: str) -> np.ndarray:
    """Read + concatenate all per-minute segments for a source (recovery helper)."""
    d = Path(session_dir)
    parts: list[np.ndarray] = []
    i = 0
    while True:
        seg = d / f"audio_{source}.{i:03d}.wav"
        if not seg.exists():
            break
        parts.append(read_wav_mono16k(seg))
        i += 1
    if not parts:
        return np.empty(0, dtype=np.float32)
    return np.concatenate(parts)


# --------------------------------------------------------------------------- #
# Records (schema 2)
# --------------------------------------------------------------------------- #
@dataclass
class UtteranceRecord:
    """One transcript line (SPEC.md §5.7 JSONL schema 2)."""

    id: str
    session_id: str
    seq: int
    source: str
    speaker: str
    start: float
    end: float
    duration: float
    text: str
    lang: str
    lang_prob: float
    audio_start_sample: int | None
    audio_end_sample: int | None
    source_epoch_id: int
    source_offset_sec: float
    forced_cut: bool
    no_speech_prob: float
    avg_logprob: float
    effective_model: str
    effective_compute_type: str
    stt_latency_ms: int | None
    created_at: str
    # patchable later (M2–M4) — present but null in M1
    speaker_alt: str | None = None
    translation: str | None = None
    translation_provider: str | None = None
    translation_error: bool = False
    stale_skipped: bool = False
    diarization_source: str = "none"
    diarization_confidence: float | None = None
    is_overlap: bool = False
    forced_overflow: bool = False
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, meta: dict[str, Any] | None = None) -> "UtteranceRecord":
        """Build a record, upconverting schema-1 rows with safe defaults (SPEC.md §5.7)."""
        meta = meta or {}
        d = dict(data)
        if d.get("schema", 1) < 2:
            d.setdefault("audio_start_sample", None)
            d.setdefault("audio_end_sample", None)
            d.setdefault("source_epoch_id", 0)
            d.setdefault("source_offset_sec", 0.0)
            d.setdefault("forced_cut", False)
            d.setdefault("is_overlap", False)
            d.setdefault("forced_overflow", False)
            d.setdefault("stale_skipped", False)
            d.setdefault("speaker_alt", None)
            d.setdefault("diarization_confidence", None)
            d.setdefault("effective_model", meta.get("whisper_model", ""))
            d.setdefault("effective_compute_type", meta.get("compute_type", ""))
            d.setdefault("stt_latency_ms", None)
            d.setdefault("translation_error", bool(d.get("translation_error", False)))
            d["schema"] = SCHEMA
        fdefs = cls.__dataclass_fields__  # type: ignore[attr-defined]
        clean = {k: v for k, v in d.items() if k in fdefs}
        import dataclasses as _dc

        for name, fdef in fdefs.items():
            if name in clean:
                continue
            if fdef.default is not _dc.MISSING:
                clean[name] = fdef.default
            elif fdef.default_factory is not _dc.MISSING:  # type: ignore[misc]
                clean[name] = fdef.default_factory()  # type: ignore[misc]
            else:
                clean[name] = None
        return cls(**clean)


@dataclass
class SessionMeta:
    session_id: str
    title: str
    created_at: str
    ended_at: str | None = None
    duration_sec: int | None = None
    sources: dict[str, bool] = field(default_factory=dict)
    hardware_preset: str = ""
    whisper_model: str = ""
    compute_type: str = ""
    translate_enabled: bool = False
    target_lang: str = "vi"
    source_languages: list[str] = field(default_factory=list)
    translation_provider: str = "nllb"
    diarization_enabled: bool = True
    diarization_realtime: bool = True
    speakers: dict[str, str] = field(default_factory=dict)
    summary_provider: str = "claude_cli"
    summarized_at: str | None = None
    rediarized_at: str | None = None
    recovered: bool = False
    app_version: str = "2.0"
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMeta":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class SessionData:
    meta: SessionMeta
    utterances: list[UtteranceRecord]
    summary: str | None = None


@dataclass
class Session:
    session_id: str
    dir: str
    meta: SessionMeta


# --------------------------------------------------------------------------- #
# Reader/writer lock (SPEC.md §5.7)
# --------------------------------------------------------------------------- #
class RWLock:
    """A small writer-preferring reader/writer lock."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.contextmanager
    def read(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting_writers > 0:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write(self) -> Iterator[None]:
        with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers > 0:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "meeting").lower()).strip("-")
    return (slug or "meeting")[:max_len]


def _fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_md_line(rec: UtteranceRecord) -> str:
    head = f"**[{_fmt_ts(rec.start)}] {rec.speaker} ({rec.lang}):** {rec.text}"
    if rec.translation:
        return head + f"\n> {rec.translation}\n"
    return head + "\n"


# --------------------------------------------------------------------------- #
# SessionStore
# --------------------------------------------------------------------------- #
class SessionStore:
    """Create/append/patch/list/load/finalize/recover sessions (SPEC.md §5.7)."""

    def __init__(self, sessions_root: str | os.PathLike[str], settings: Any | None = None) -> None:
        self.root = Path(sessions_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._locks: dict[str, RWLock] = {}
        self._locks_guard = threading.Lock()
        self._seq: dict[str, int] = {}
        self._fsync_last: dict[str, float] = {}

    # -- locks ------------------------------------------------------------- #
    def _lock(self, session_id: str) -> RWLock:
        with self._locks_guard:
            if session_id not in self._locks:
                self._locks[session_id] = RWLock()
            return self._locks[session_id]

    def _dir(self, session_id: str) -> Path:
        return self.root / session_id

    def _jsonl(self, session_id: str) -> Path:
        return self._dir(session_id) / "transcript.jsonl"

    def _md(self, session_id: str) -> Path:
        return self._dir(session_id) / "transcript.md"

    def _meta_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "meta.json"

    # -- create ------------------------------------------------------------ #
    def create(self, title: str = "meeting") -> Session:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_id = f"{stamp}-{slugify(title)}"
        d = self._dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        s = self.settings
        meta = SessionMeta(
            session_id=session_id,
            title=title or "meeting",
            created_at=_now_iso(),
            sources={},
            hardware_preset=getattr(s, "hardware_preset", "") if s else "",
            translate_enabled=getattr(s, "translate_enabled", False) if s else False,
            target_lang=getattr(s, "target_lang", "vi") if s else "vi",
            source_languages=list(getattr(s, "source_languages", []) or []) if s else [],
            translation_provider=getattr(s, "translation_provider", "nllb") if s else "nllb",
            diarization_enabled=getattr(s, "diarization_enabled", True) if s else True,
            diarization_realtime=getattr(s, "diarization_realtime", True) if s else True,
            summary_provider=getattr(s, "summarizer_provider", "claude_cli") if s else "claude_cli",
        )
        self._write_meta(meta)
        self._jsonl(session_id).touch()
        self._md(session_id).write_text(f"# {meta.title}\n\n", encoding="utf-8")
        self._seq[session_id] = 0
        return Session(session_id=session_id, dir=str(d), meta=meta)

    def set_meta_fields(self, session_id: str, fields: dict[str, Any]) -> None:
        with self._lock(session_id).write():
            meta = self._read_meta(session_id)
            data = meta.to_dict()
            data.update(fields)
            self._write_meta(SessionMeta.from_dict(data))

    # -- append ------------------------------------------------------------ #
    def next_seq(self, session_id: str) -> int:
        cur = self._seq.get(session_id)
        if cur is None:
            cur = self._max_seq(session_id)
        cur += 1
        self._seq[session_id] = cur
        return cur

    def _max_seq(self, session_id: str) -> int:
        mx = 0
        for rec in self._iter_records(session_id):
            mx = max(mx, rec.seq)
        return mx

    def append_utterance(self, rec: UtteranceRecord) -> None:
        sid = rec.session_id
        with self._lock(sid).write():
            with self._jsonl(sid).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                self._maybe_fsync(sid, fh)
            with self._md(sid).open("a", encoding="utf-8") as fh:
                fh.write(_render_md_line(rec))
            self._seq[sid] = max(self._seq.get(sid, 0), rec.seq)

    def _maybe_fsync(self, session_id: str, fh) -> None:
        import time as _time

        interval = (getattr(self.settings, "fsync_interval_ms", 1000) if self.settings else 1000) / 1000.0
        now = _time.monotonic()
        last = self._fsync_last.get(session_id, 0.0)
        if now - last >= interval:
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
            self._fsync_last[session_id] = now

    # -- patch ------------------------------------------------------------- #
    def patch_utterance(self, session_id: str, seq: int, fields: dict[str, Any]) -> None:
        with self._lock(session_id).write():
            records = list(self._iter_records(session_id))
            changed = False
            for rec in records:
                if rec.seq == seq:
                    for k, v in fields.items():
                        if hasattr(rec, k):
                            setattr(rec, k, v)
                    changed = True
                    break
            if changed:
                self._rewrite_all(session_id, records)

    def utterances_since(self, session_id: str, since_seq: int) -> list[UtteranceRecord]:
        with self._lock(session_id).read():
            return [r for r in self._iter_records(session_id) if r.seq > since_seq]

    # -- rename ------------------------------------------------------------ #
    def rename_speaker(self, session_id: str, old: str, new: str) -> int:
        with self._lock(session_id).write():
            records = list(self._iter_records(session_id))
            count = 0
            for rec in records:
                if rec.speaker == old:
                    rec.speaker = new
                    count += 1
            if count:
                self._rewrite_all(session_id, records)
                meta = self._read_meta(session_id)
                meta.speakers[old] = new
                self._write_meta(meta)
            return count

    # -- summary ----------------------------------------------------------- #
    def write_summary(self, session_id: str, markdown: str) -> None:
        with self._lock(session_id).write():
            path = self._dir(session_id) / "summary.md"
            if path.exists():
                path.replace(path.with_suffix(".md.bak"))
            _atomic_write(path, markdown)
            meta = self._read_meta(session_id)
            meta.summarized_at = _now_iso()
            self._write_meta(meta)

    def rewrite_after_rediarize(self, session_id: str, new_labels: dict[int, str]) -> None:
        """Apply offline (tier-2) speaker labels by seq (M4 hook; backup kept)."""
        with self._lock(session_id).write():
            jsonl = self._jsonl(session_id)
            if jsonl.exists():
                backup = jsonl.with_suffix(".jsonl.pre-rediarize")
                backup.write_bytes(jsonl.read_bytes())
            records = list(self._iter_records(session_id))
            for rec in records:
                if rec.seq in new_labels:
                    rec.speaker = new_labels[rec.seq]
                    rec.diarization_source = "offline"
            self._rewrite_all(session_id, records)
            meta = self._read_meta(session_id)
            meta.rediarized_at = _now_iso()
            self._write_meta(meta)

    # -- read -------------------------------------------------------------- #
    def _iter_records(self, session_id: str) -> Iterator[UtteranceRecord]:
        path = self._jsonl(session_id)
        if not path.exists():
            return
        meta_data = self._safe_meta_dict(session_id)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed jsonl line in %s", session_id)
                continue  # tolerate a partial trailing line after a crash
            yield UtteranceRecord.from_dict(data, meta=meta_data)

    def load_session(self, session_id: str) -> SessionData:
        with self._lock(session_id).read():
            meta = self._read_meta(session_id)
            records = list(self._iter_records(session_id))
            summary_path = self._dir(session_id) / "summary.md"
            summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else None
        return SessionData(meta=meta, utterances=records, summary=summary)

    def list_sessions(self) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for d in self.root.iterdir() if self.root.exists() else []:
            if d.is_dir() and (d / "meta.json").exists():
                with contextlib.suppress(Exception):
                    metas.append(self._read_meta(d.name))
        metas.sort(key=lambda m: m.created_at, reverse=True)
        return metas

    # -- delete / retention ------------------------------------------------ #
    def delete_session(self, session_id: str) -> None:
        import shutil

        with self._lock(session_id).write():
            d = self._dir(session_id)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    def delete_audio_only(self, session_id: str) -> None:
        with self._lock(session_id).write():
            d = self._dir(session_id)
            for wav in list(d.glob("*.wav")):
                with contextlib.suppress(OSError):
                    wav.unlink()
            idx = d / "samples.idx"
            if idx.exists():
                with contextlib.suppress(OSError):
                    idx.unlink()
            meta = self._read_meta(session_id)
            meta.sources = {k: False for k in meta.sources}
            self._write_meta(meta)

    def apply_retention(self) -> int:
        days = getattr(self.settings, "retention_days", 0) if self.settings else 0
        if not days or days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        pruned = 0
        for meta in self.list_sessions():
            try:
                created = datetime.fromisoformat(meta.created_at).timestamp()
            except ValueError:
                continue
            if created < cutoff:
                self.delete_session(meta.session_id)
                pruned += 1
        return pruned

    # -- finalize / recovery ---------------------------------------------- #
    def finalize(self, session_id: str) -> None:
        with self._lock(session_id).write():
            records = sorted(self._iter_records(session_id), key=lambda r: r.start)
            # Re-render transcript.md sorted by start.
            meta = self._read_meta(session_id)
            lines = [f"# {meta.title}\n\n"] + [_render_md_line(r) for r in records]
            _atomic_write(self._md(session_id), "".join(lines))
            # Concat raw segments → canonical WAVs (if segments present).
            d = self._dir(session_id)
            for source in ("you", "them"):
                if list(d.glob(f"audio_{source}.[0-9][0-9][0-9].wav")):
                    RawSegmentWriter(d, source).close_and_concat()
            if meta.ended_at is None:
                meta.ended_at = _now_iso()
                try:
                    start = datetime.fromisoformat(meta.created_at)
                    end = datetime.fromisoformat(meta.ended_at)
                    meta.duration_sec = int((end - start).total_seconds())
                except ValueError:
                    meta.duration_sec = None
            self._write_meta(meta)

    def detect_incomplete(self) -> list[SessionMeta]:
        return [m for m in self.list_sessions() if m.ended_at is None]

    def recover_offline(self, session_id: str, transcriber, vad=None) -> int:
        """Transcribe the untranscribed audio tail of an incomplete session (SPEC.md §5.7).

        For each source: concat raw segments (or use canonical WAV), find the last
        transcribed ``audio_end_sample``, segment the tail with a VAD, transcribe it,
        and append the recovered utterances. Then finalize.
        """
        from .audio.segmenter import Segmenter
        from .audio.vad import FakeVad, make_vad

        settings = self.settings
        d = self._dir(session_id)
        existing = list(self._iter_records(session_id))
        last_end: dict[str, int] = {"you": 0, "them": 0}
        for rec in existing:
            if rec.audio_end_sample is not None:
                last_end[rec.source] = max(last_end.get(rec.source, 0), rec.audio_end_sample)

        recovered = 0
        for source in ("you", "them"):
            audio = concat_segments(d, source)
            if audio.size == 0:
                canonical = d / f"audio_{source}.wav"
                audio = read_wav_mono16k(canonical)
            if audio.size == 0:
                continue
            start = min(last_end.get(source, 0), audio.size)
            tail = audio[start:]
            if tail.size == 0:
                continue
            source_vad = vad if vad is not None else (make_vad(settings) if settings else FakeVad())
            seg = Segmenter(source, settings, source_vad)
            for utt in seg.run_array(tail, start_sample=start):
                tr = transcriber.transcribe(utt)
                if tr is None:
                    continue
                self.append_utterance(self._record_from(session_id, source, utt, tr))
                recovered += 1

        self.set_meta_fields(session_id, {"recovered": True})
        self.finalize(session_id)
        return recovered

    def _record_from(self, session_id: str, source: str, utt, tr) -> UtteranceRecord:
        seq = self.next_seq(session_id)
        speaker = "You" if source == "you" else "Them"
        return UtteranceRecord(
            id=f"u_{seq:06d}",
            session_id=session_id,
            seq=seq,
            source=source,
            speaker=speaker,
            start=utt.start,
            end=utt.end,
            duration=utt.end - utt.start,
            text=tr.text,
            lang=tr.lang,
            lang_prob=tr.lang_prob,
            audio_start_sample=utt.audio_start_sample,
            audio_end_sample=utt.audio_end_sample,
            source_epoch_id=utt.source_epoch_id,
            source_offset_sec=utt.source_offset_sec,
            forced_cut=utt.forced_cut,
            no_speech_prob=tr.no_speech_prob,
            avg_logprob=tr.avg_logprob,
            effective_model=tr.effective_model,
            effective_compute_type=tr.effective_compute_type,
            stt_latency_ms=tr.stt_latency_ms,
            created_at=_now_iso(),
            diarization_source="realtime" if source == "you" else "none",
        )

    # -- meta io ----------------------------------------------------------- #
    def _write_meta(self, meta: SessionMeta) -> None:
        _atomic_write(
            self._meta_path(meta.session_id),
            json.dumps(meta.to_dict(), indent=2, ensure_ascii=False),
        )

    def _safe_meta_dict(self, session_id: str) -> dict[str, Any]:
        path = self._meta_path(session_id)
        if not path.exists():
            return {}
        with contextlib.suppress(Exception):
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def _read_meta(self, session_id: str) -> SessionMeta:
        data = self._safe_meta_dict(session_id)
        if not data:
            return SessionMeta(session_id=session_id, title=session_id, created_at=_now_iso())
        return SessionMeta.from_dict(data)

    def _rewrite_all(self, session_id: str, records: list[UtteranceRecord]) -> None:
        tmp_lines = "".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in records)
        _atomic_write(self._jsonl(session_id), tmp_lines)
        meta = self._read_meta(session_id)
        md = [f"# {meta.title}\n\n"] + [_render_md_line(r) for r in sorted(records, key=lambda r: r.start)]
        _atomic_write(self._md(session_id), "".join(md))


def _atomic_write(path: Path, text: str) -> None:
    """Atomic write via temp + os.replace in the same directory (SPEC.md §5.7)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
