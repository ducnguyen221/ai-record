"""FastAPI backend: token+Origin auth, consent gate, REST + WebSocket (SPEC.md §5.8).

The app is built by :func:`create_app` around an :class:`AppState` so tests can inject
a known token, a temp store, and an in-memory :class:`Secrets`. All REST endpoints and
the WebSocket require the per-launch token; a bad ``Origin`` is rejected; capture start
is gated 403 on consent. Worker threads bridge to the event loop via
:meth:`AppState.submit` with per-client bounded queues (SPEC.md §4.7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as _secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Secrets, Settings, resolve_preset, resolve_sessions_root, SECRET_NAMES
from .preflight import run_preflight
from .store import InvalidSessionId, SessionStore

log = logging.getLogger("ai_record.server")

WEB_DIR = Path(__file__).parent / "web"

# Durable message types never silently dropped (SPEC.md §4.7).
_DURABLE = {"utterance", "patch", "rename", "rediarize", "summary"}


class _Client:
    """One connected WebSocket with a bounded outgoing queue."""

    def __init__(self, ws: WebSocket, maxsize: int) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.lagging = False
        self.lagging_since: float | None = None


class AppState:
    """Holds server-wide state and owns the capture pipeline lifecycle."""

    def __init__(
        self,
        settings: Settings,
        store: SessionStore | None = None,
        secrets: Secrets | None = None,
        token: str | None = None,
        port: int = 8848,
    ) -> None:
        self.settings = settings
        self.secrets = secrets or Secrets()
        self.store = store or SessionStore(resolve_sessions_root(settings), settings)
        self.token = token or _secrets.token_urlsafe(32)
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[_Client] = set()
        self.ws_drops = 0
        self.pipeline = None
        self.capture = None
        self.active_session_id: str | None = None
        # Offline re-diarization progress, keyed by session id (SPEC.md §5.5 tier 2).
        self.rediarize_state: dict[str, dict] = {}
        self._rediarize_threads: dict[str, Any] = {}

    # -- auth ------------------------------------------------------------- #
    def allowed_origins(self) -> set[str]:
        return {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
            "null",  # pywebview / file origin
        }

    def check_origin(self, origin: str | None) -> bool:
        if not origin:
            return True  # native pywebview / curl-from-owner (still needs token)
        return origin in self.allowed_origins()

    # -- broadcast bridge (threads → loop) -------------------------------- #
    def submit(self, msg: dict) -> None:
        """Thread-safe enqueue from a worker thread (SPEC.md §4.6/§4.7)."""
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._fanout, msg)
        except RuntimeError:  # loop closed
            pass

    def _fanout(self, msg: dict) -> None:
        mtype = msg.get("type", "")
        durable = mtype in _DURABLE
        now = time.monotonic()
        deadline = self.settings.ws_client_slow_deadline_s
        for client in list(self.clients):
            if durable:
                try:
                    client.queue.put_nowait(msg)
                    client.lagging = False
                    client.lagging_since = None
                except asyncio.QueueFull:
                    # NEVER silently drop a durable event. The utterance is already
                    # persisted (jsonl keyed by seq); mark the client lagging and, if
                    # it stays behind past the deadline, close it so it reconnects and
                    # replays via the since_seq catch-up endpoint (SPEC.md §4.7).
                    self.ws_drops += 1
                    client.lagging = True
                    if client.lagging_since is None:
                        client.lagging_since = now
                    if now - client.lagging_since >= deadline:
                        self._close_client(client)
            else:
                # STATUS (non-durable): coalesce — drop the oldest, keep the newest.
                try:
                    client.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    with _suppress():
                        client.queue.get_nowait()
                    with _suppress():
                        client.queue.put_nowait(msg)

    def _close_client(self, client: "_Client") -> None:
        """Evict a persistently-lagging client (it will reconnect + replay by seq)."""
        self.clients.discard(client)
        if self.loop is None:
            return

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await client.ws.close(code=4402)

        with contextlib.suppress(Exception):
            self.loop.create_task(_close())


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


# --------------------------------------------------------------------------- #
def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="ai-record", version=state.settings.app_version)

    async def auth(request: Request) -> None:
        token = request.headers.get("X-AI-Record-Token") or request.query_params.get("token")
        if token != state.token:
            raise HTTPException(status_code=401, detail="missing or invalid token")
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin is not None and not state.check_origin(_origin_of(origin)):
            raise HTTPException(status_code=403, detail="origin not allowed")

    dep = [Depends(auth)]

    @app.exception_handler(InvalidSessionId)
    async def _bad_session_id(request: Request, exc: InvalidSessionId) -> JSONResponse:
        # A rejected/traversal session_id is indistinguishable from "not found".
        return JSONResponse(status_code=404, content={"detail": "session not found"})

    @app.on_event("startup")
    async def _capture_loop() -> None:
        state.loop = asyncio.get_running_loop()

    # -- static UI -------------------------------------------------------- #
    if (WEB_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index() -> Any:
        idx = WEB_DIR / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return JSONResponse({"app": "ai-record", "note": "UI not built"})

    @app.get("/styles.css")
    async def styles() -> Any:
        return _serve(WEB_DIR / "styles.css")

    @app.get("/app.js")
    async def appjs() -> Any:
        return _serve(WEB_DIR / "app.js")

    # -- health / preflight ---------------------------------------------- #
    @app.get("/api/health", dependencies=dep)
    async def health() -> dict:
        cuda = run_preflight(state.settings, state.secrets)["cuda"]
        return {"ok": True, "gpu": cuda, "cuda": cuda, "models_loaded": state.pipeline is not None}

    @app.get("/api/preflight", dependencies=dep)
    async def preflight() -> dict:
        return run_preflight(state.settings, state.secrets)

    # -- capture lifecycle ------------------------------------------------ #
    @app.post("/api/capture/start", dependencies=dep)
    async def start(body: dict | None = None) -> dict:
        if not state.settings.consent_acknowledged:
            raise HTTPException(status_code=403, detail="consent not acknowledged")
        if state.pipeline is not None:
            raise HTTPException(status_code=409, detail="already recording")
        body = body or {}
        title = body.get("title") or "meeting"
        mode = body.get("mode") or "meeting"
        if mode not in ("meeting", "dictation"):
            mode = "meeting"
        sources = body.get("sources")
        if sources is not None:
            sources = [s for s in sources if s in ("you", "them")]
            if not sources:
                raise HTTPException(status_code=422, detail="sources must include 'you' and/or 'them'")
        try:
            session_id, opened = _start_capture(state, title, mode, sources)
        except CaptureError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return {"session_id": session_id, "sources": opened}

    @app.post("/api/capture/stop", dependencies=dep)
    async def stop() -> dict:
        sid = _stop_capture(state)
        return {"session_id": sid, "finalized": True}

    @app.get("/api/capture/status", dependencies=dep)
    async def status() -> dict:
        return _status(state)

    # -- sessions --------------------------------------------------------- #
    @app.get("/api/sessions", dependencies=dep)
    async def sessions() -> list[dict]:
        return [m.to_dict() for m in state.store.list_sessions()]

    @app.get("/api/sessions/{sid}", dependencies=dep)
    async def session(sid: str) -> dict:
        data = state.store.load_session(sid)
        return {
            "meta": data.meta.to_dict(),
            "utterances": [u.to_dict() for u in data.utterances],
            "summary": data.summary,
        }

    @app.get("/api/sessions/{sid}/utterances", dependencies=dep)
    async def utterances(sid: str, since_seq: int = 0) -> list[dict]:
        return [u.to_dict() for u in state.store.utterances_since(sid, since_seq)]

    @app.get("/api/languages", dependencies=dep)
    async def languages() -> dict:
        from .lang_maps import supported_source_languages

        return {"languages": supported_source_languages(), "target": state.settings.target_lang}

    @app.post("/api/sessions/{sid}/speakers/rename", dependencies=dep)
    async def rename(sid: str, body: dict) -> dict:
        if state.active_session_id == sid:
            raise HTTPException(status_code=409, detail="cannot rename during active capture")
        updated = state.store.rename_speaker(sid, body["old"], body["new"])
        state.submit({"type": "rename", "old": body["old"], "new": body["new"]})
        return {"updated_count": updated}

    @app.post("/api/sessions/{sid}/rediarize", dependencies=dep)
    async def rediarize(sid: str) -> dict:
        if state.active_session_id == sid:
            raise HTTPException(status_code=409, detail="cannot re-diarize during active capture")
        # Validate the session id + ensure it exists before spawning the worker.
        state.store.load_session(sid)
        running = state.rediarize_state.get(sid, {}).get("state")
        if running in ("started", "progress"):
            raise HTTPException(status_code=409, detail="re-diarization already running")
        _start_rediarize(state, sid)
        return {"status": "started"}

    @app.get("/api/sessions/{sid}/rediarize/status", dependencies=dep)
    async def rediarize_status(sid: str) -> dict:
        return state.rediarize_state.get(sid, {"state": "idle", "progress": 0.0})

    @app.post("/api/sessions/{sid}/summarize", dependencies=dep)
    async def summarize(sid: str, body: dict | None = None) -> dict:
        from .summarizer import SummarizerError, SummarizerUnavailable, build_summary

        body = body or {}
        scenario = body.get("scenario") or "reformat"
        provider = body.get("provider") or state.settings.summarizer_provider
        data = state.store.load_session(sid)
        try:
            result = build_summary(data, scenario, provider, state.settings, state.secrets)
        except SummarizerUnavailable as exc:
            # Provider not installed / no key: 503, not a 200-with-{error} (review I2).
            return JSONResponse(status_code=503, content={"error": str(exc)})
        except SummarizerError as exc:
            # Provider ran but failed (non-zero exit / timeout / empty): 502, not 500 (I1).
            return JSONResponse(status_code=502, content={"error": str(exc)})
        except ValueError as exc:  # unknown scenario
            raise HTTPException(status_code=422, detail=str(exc))
        state.store.write_summary(sid, result.markdown, scenario=result.scenario, provider=result.provider)
        state.submit({"type": "summary", "state": "done", "markdown": result.markdown})
        return {
            "markdown": result.markdown,
            "scenario": result.scenario,
            "provider": result.provider,
            "reformat_fallback": result.reformat_fallback,
        }

    @app.get("/api/sessions/{sid}/summary", dependencies=dep)
    async def get_summary(sid: str) -> dict:
        data = state.store.load_session(sid)
        if not data.summary:
            raise HTTPException(status_code=404, detail="no summary")
        return {
            "markdown": data.summary,
            "scenario": data.meta.summary_scenario,
            "summarized_at": data.meta.summarized_at,
        }

    @app.get("/api/sessions/{sid}/export", dependencies=dep)
    async def export(sid: str, what: str = "transcript", fmt: str = "md") -> Any:
        from .export import render_export

        data = state.store.load_session(sid)
        try:
            filename, content, media_type = render_export(data, what, fmt)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        from fastapi.responses import Response

        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/sessions/{sid}/recover", dependencies=dep)
    async def recover(sid: str) -> dict:
        from .transcriber import Transcriber

        preset = resolve_preset(state.settings)
        transcriber = Transcriber(state.settings, preset)
        n = state.store.recover_offline(sid, transcriber)
        return {"recovered_utterances": n}

    @app.delete("/api/sessions/{sid}", dependencies=dep)
    async def delete(sid: str) -> dict:
        state.store.delete_session(sid)
        return {"deleted": True}

    @app.delete("/api/sessions/{sid}/audio", dependencies=dep)
    async def delete_audio(sid: str) -> dict:
        state.store.delete_audio_only(sid)
        return {"audio_deleted": True}

    # -- settings / secrets ---------------------------------------------- #
    @app.get("/api/settings", dependencies=dep)
    async def get_settings() -> dict:
        return state.settings.redacted(state.secrets)

    @app.put("/api/settings", dependencies=dep)
    async def put_settings(partial: dict) -> dict:
        try:
            new = state.settings.update(partial)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        new.save()
        state.settings = new
        return new.redacted(state.secrets)

    @app.post("/api/secrets/{name}", dependencies=dep)
    async def set_secret(name: str, body: dict) -> dict:
        if name not in SECRET_NAMES:
            raise HTTPException(status_code=404, detail="unknown secret")
        state.secrets.set(name, body["value"])
        return {"ok": True}

    @app.delete("/api/secrets/{name}", dependencies=dep)
    async def clear_secret(name: str) -> dict:
        if name not in SECRET_NAMES:
            raise HTTPException(status_code=404, detail="unknown secret")
        state.secrets.clear(name)
        return {"ok": True}

    # -- WebSocket -------------------------------------------------------- #
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        if token != state.token:
            await websocket.close(code=4401)
            return
        origin = websocket.headers.get("origin")
        if origin is not None and not state.check_origin(_origin_of(origin)):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        client = _Client(websocket, state.settings.ws_client_queue_max)
        state.clients.add(client)
        await _send(websocket, _status(state) | {"type": "status", "note": ""})
        await _send_recent(state, websocket)
        sender = asyncio.create_task(_sender(client))
        try:
            while True:
                raw = await websocket.receive_json()
                if isinstance(raw, dict) and raw.get("type") == "get_status":
                    await _send(websocket, _status(state) | {"type": "status", "note": ""})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            log.debug("ws error: %s", exc)
        finally:
            sender.cancel()
            state.clients.discard(client)

    return app


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class CaptureError(RuntimeError):
    pass


def _origin_of(value: str) -> str:
    """Reduce an Origin/Referer to scheme://host:port."""
    if value == "null":
        return "null"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:  # pragma: no cover
        pass
    return value


def _serve(path: Path) -> Any:
    if path.exists():
        return FileResponse(str(path))
    raise HTTPException(status_code=404, detail="not found")


async def _send(ws: WebSocket, msg: dict) -> None:
    with _suppress():
        await ws.send_json(msg)


async def _sender(client: _Client) -> None:
    try:
        while True:
            msg = await client.queue.get()
            await client.ws.send_json(msg)
    except Exception:  # pragma: no cover - disconnect
        return


async def _send_recent(state: AppState, ws: WebSocket, n: int = 20) -> None:
    if not state.active_session_id:
        return
    with _suppress():
        recs = state.store.utterances_since(state.active_session_id, 0)
        for rec in recs[-n:]:
            await _send(ws, {"type": "utterance", "record": rec.to_dict()})


def _status(state: AppState) -> dict:
    base = {
        "recording": state.pipeline is not None,
        "session_id": state.active_session_id,
        "preset": resolve_preset(state.settings).name,
        "effective_model": "",
        "ladder_step": 0,
        "degraded_states": [],
        "dropped_frames": 0,
        "ws_drops": state.ws_drops,
        "sources": {},
    }
    if state.pipeline is not None:
        base.update(state.pipeline.status())
    if state.capture is not None:
        health = {cs.source: cs.health.to_dict() for cs in state.capture.sources_status()}
        base["sources"] = health
        avail = {cs.source: cs.available for cs in state.capture.sources_status()}
        degraded = list(base.get("degraded_states", []))
        if avail.get("them") and not avail.get("you"):
            degraded.append("them_only")
        elif avail.get("you") and not avail.get("them"):
            degraded.append("mic_only")
        base["degraded_states"] = degraded
    return base


def _start_capture(
    state: AppState, title: str, mode: str = "meeting", sources: list[str] | None = None
) -> tuple[str, dict]:
    """Build the pipeline + capture manager and start recording (real hardware path)."""
    from .capture_helpers import build_and_start

    return build_and_start(state, title, mode, sources)


def _start_rediarize(state: AppState, sid: str) -> None:
    """Run tier-2 offline re-diarization in a background thread (SPEC.md §5.5)."""
    import threading

    from .diarizer import make_offline_diarizer, relabel_them_utterances

    state.rediarize_state[sid] = {"state": "started", "progress": 0.0}
    state.submit({"type": "rediarize", "state": "started", "detail": sid})

    def _worker() -> None:
        try:
            diarizer = make_offline_diarizer(state.settings, state.secrets)
            ok, why = diarizer.available()
            if not ok:
                state.rediarize_state[sid] = {"state": "error", "progress": 0.0, "error": why}
                state.submit({"type": "rediarize", "state": "error", "detail": why})
                return
            session_dir = state.store._dir(sid)
            state.rediarize_state[sid] = {"state": "progress", "progress": 0.3}
            state.submit({"type": "rediarize", "state": "progress", "detail": 0.3})
            spans = diarizer.rediarize(str(session_dir))
            records = state.store.load_session(sid).utterances
            new_labels = relabel_them_utterances(records, spans)
            state.store.rewrite_after_rediarize(sid, new_labels)
            state.rediarize_state[sid] = {"state": "done", "progress": 1.0, "updated_count": len(new_labels)}
            state.submit({"type": "rediarize", "state": "done", "detail": len(new_labels)})
        except Exception as exc:  # HfTokenRequired, FileNotFound, model errors
            log.warning("rediarize failed for %s: %s", sid, exc)
            state.rediarize_state[sid] = {"state": "error", "progress": 0.0, "error": str(exc)}
            state.submit({"type": "rediarize", "state": "error", "detail": str(exc)})

    t = threading.Thread(target=_worker, name=f"rediarize-{sid}", daemon=True)
    state._rediarize_threads[sid] = t
    t.start()


def _stop_capture(state: AppState) -> str | None:
    sid = state.active_session_id
    if state.capture is not None:
        with _suppress():
            state.capture.stop()
    if state.pipeline is not None:
        with _suppress():
            state.pipeline.stop()
    if sid is not None:
        with _suppress():
            state.store.finalize(sid)
    state.pipeline = None
    state.capture = None
    state.active_session_id = None
    return sid
