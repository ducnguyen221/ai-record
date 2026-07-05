"""Wire the real capture → pipeline for a recording session (SPEC.md §5.8 lifecycle).

Imported lazily by ``server._start_capture`` so importing ``server`` never pulls in
audio hardware libraries. Builds the pipeline, attaches a :class:`CaptureManager`
that feeds the pipeline's ring buffers + crash-safe raw writers, and starts both.
"""

from __future__ import annotations

import logging

from .config import resolve_preset

log = logging.getLogger("ai_record.capture_helpers")


def build_and_start(state, title: str) -> tuple[str, dict]:
    from .audio.capture import CaptureManager
    from .pipeline import Pipeline
    from .store import RawSegmentWriter
    from .transcriber import Transcriber
    from .server import CaptureError

    settings = state.settings
    preset = resolve_preset(settings)
    session = state.store.create(title)

    from .audio.segmenter import SourceEpoch

    epoch_states = {"you": SourceEpoch(), "them": SourceEpoch()}

    transcriber = Transcriber(settings, preset, on_status=state.submit)
    pipeline = Pipeline(
        settings, preset, transcriber, state.store, session,
        broadcast=state.submit, epoch_states=epoch_states,
    )

    raw_you = raw_them = None
    if settings.persist_audio:
        raw_you = RawSegmentWriter(session.dir, "you", settings.raw_segment_seconds, settings)
        raw_them = RawSegmentWriter(session.dir, "them", settings.raw_segment_seconds, settings)

    def on_status(source: str, event: str, detail: str) -> None:
        state.submit({"type": "status", "note": f"{source}:{event}:{detail}", "recording": True})

    capture = CaptureManager(
        ring_you=pipeline.rings["you"],
        ring_them=pipeline.rings["them"],
        raw_you=raw_you,
        raw_them=raw_them,
        settings=settings,
        on_status=on_status,
        epoch_states=epoch_states,
    )
    up = capture.start()
    if not up:
        # No source came up — do not enter recording state (SPEC.md §5.1/§8.3).
        state.store.delete_session(session.session_id)
        raise CaptureError("no audio source available (loopback + mic both failed)")

    sources = {cs.source: cs.available for cs in up}
    state.store.set_meta_fields(
        session.session_id,
        {
            "sources": sources,
            "hardware_preset": preset.name,
            "whisper_model": preset.whisper_model,
            "compute_type": preset.whisper_compute_type,
        },
    )
    pipeline.start()
    state.pipeline = pipeline
    state.capture = capture
    state.active_session_id = session.session_id
    return session.session_id, sources
