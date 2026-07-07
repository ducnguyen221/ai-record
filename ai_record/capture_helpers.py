"""Wire the real capture → pipeline for a recording session (SPEC.md §5.8 lifecycle).

Imported lazily by ``server._start_capture`` so importing ``server`` never pulls in
audio hardware libraries. Builds the pipeline, attaches a :class:`CaptureManager`
that feeds the pipeline's ring buffers + crash-safe raw writers, and starts both.
"""

from __future__ import annotations

import logging

from .config import resolve_preset

log = logging.getLogger("ai_record.capture_helpers")

# Sentinel resolved lazily at runtime so importing this module never pulls in the
# (heavy, ffmpeg-backed) video stack. Tests monkeypatch this attribute with a fake
# manager class; production leaves it ``None`` and the real class is imported on demand.
VideoCaptureManager = None


def _import_video_manager():
    """Import the real VideoCaptureManager lazily (kept out of module import path)."""
    from .capture_video import VideoCaptureManager as _VCM

    return _VCM


def build_and_start(
    state,
    title: str,
    mode: str = "meeting",
    sources: list[str] | None = None,
    devices: dict[str, str | None] | None = None,
    *,
    ephemeral: bool = False,
    video: dict | None = None,
) -> tuple[str, dict]:
    from .audio.capture import CaptureManager
    from .pipeline import Pipeline
    from .store import RawSegmentWriter
    from .transcriber import Transcriber
    from .server import CaptureError

    settings = state.settings
    preset = resolve_preset(settings)
    # Ephemeral ("Không lưu"): the session lives only in memory — no directory,
    # no transcript/WAV/summary files anywhere under the sessions root.
    session = state.store.create(title, mode=mode, persist=not ephemeral)

    from .audio.segmenter import SourceEpoch

    epoch_states = {"you": SourceEpoch(), "them": SourceEpoch()}

    transcriber = Transcriber(settings, preset, on_status=state.submit)

    # M2/M3 post-processing (lazy, CPU by default per preset; SPEC.md §4.5).
    translator = None
    if settings.translate_enabled:
        from .translator import make_translator

        translator = make_translator(settings, preset, state.secrets)
    diarizer = None
    if settings.diarization_enabled and settings.diarization_realtime and preset.diarization_realtime:
        from .diarizer import make_realtime_diarizer

        diarizer = make_realtime_diarizer(settings, preset)

    pipeline = Pipeline(
        settings, preset, transcriber, state.store, session,
        broadcast=state.submit, epoch_states=epoch_states,
        translator=translator, diarizer=diarizer,
    )

    enabled = tuple(s for s in ("them", "you") if not sources or s in sources) or ("them", "you")

    raw_you = raw_them = None
    if settings.persist_audio and not ephemeral:
        if "you" in enabled:
            raw_you = RawSegmentWriter(session.dir, "you", settings.raw_segment_seconds, settings)
        if "them" in enabled:
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
        enabled_sources=enabled,
        devices=devices,
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
    state.active_ephemeral = bool(ephemeral)

    # -- video capture (opt-in, best-effort) -------------------------------- #
    # Started ONLY after audio is up and ONLY when not ephemeral. A video-start
    # failure NEVER fails the audio session — it is surfaced via ``state.video_errors``.
    state.video = None
    state.video_errors = []
    state.video_skipped = None
    if video and ephemeral:
        # Ephemeral ("Không lưu"): nothing is written to disk, so no video is recorded.
        state.video_skipped = "ephemeral"
    elif video:
        try:
            import subprocess as _sp

            manager_cls = VideoCaptureManager or _import_video_manager()
            manager = manager_cls(session.dir, video, settings, spawn=_sp.Popen)
            result = manager.start() or {}
            state.video = manager
            state.video_errors = list(result.get("errors") or [])
            # Record the chosen config into meta (round-trips via SessionMeta.video).
            state.store.set_meta_fields(
                session.session_id,
                {
                    "video": {
                        "screen": video.get("screen"),
                        "camera": video.get("camera"),
                        "encoder": settings.video_encoder,
                        "container": settings.video_container,
                        "screen_fps": settings.video_screen_fps,
                        "camera_fps": settings.video_camera_fps,
                        "capture_cursor": settings.video_capture_cursor,
                    }
                },
            )
        except Exception as exc:  # construction/import/start failure — never fail audio
            log.warning("video capture failed to start: %s", exc)
            state.video = None
            state.video_errors = [str(exc)]

    return session.session_id, sources
