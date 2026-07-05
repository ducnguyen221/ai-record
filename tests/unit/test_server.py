import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.server import AppState, _Client, create_app
from ai_record.store import SessionStore

TOKEN = "test-token-123"
H = {"X-AI-Record-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    settings = Settings(sessions_root=str(tmp_path / "s"), consent_acknowledged=False)
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN, port=8848)
    with TestClient(create_app(state)) as c:
        c.ai_state = state
        c.ai_store = store
        yield c


def test_open_folder_root(client, monkeypatch):
    calls = []
    monkeypatch.setattr("ai_record.server._reveal", lambda p: calls.append(str(p)))
    r = client.post("/api/open-folder", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["path"] == str(client.ai_store.root)
    assert calls == [str(client.ai_store.root)]


def test_open_folder_requires_token(client):
    assert client.post("/api/open-folder").status_code == 401


def test_open_session_folder_ok(client, monkeypatch):
    calls = []
    monkeypatch.setattr("ai_record.server._reveal", lambda p: calls.append(str(p)))
    sess = client.ai_store.create("demo")
    r = client.post(f"/api/sessions/{sess.session_id}/open-folder", headers=H)
    assert r.status_code == 200
    assert calls and calls[0].endswith(sess.session_id)


def test_open_session_folder_traversal_blocked(client, monkeypatch):
    def _boom(_p):
        raise AssertionError("_reveal must not run for a traversal id")
    monkeypatch.setattr("ai_record.server._reveal", _boom)
    for sid in ["..%5C..%5Cdocs", "..%2f..%2fx", "C:%5CWindows"]:
        r = client.post(f"/api/sessions/{sid}/open-folder", headers=H)
        assert r.status_code in (404, 422), (sid, r.status_code)


def test_missing_token_401(client):
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers={"X-AI-Record-Token": "wrong"}).status_code == 401


def test_bad_origin_rejected(client):
    r = client.get("/api/settings", headers={**H, "Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_good_request_and_redaction(client):
    r = client.get("/api/settings", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "hf_token" not in body
    assert body["hf_token_is_set"] is False
    assert "gemini_api_key" not in body


def test_consent_gate_403(client):
    r = client.post("/api/capture/start", headers=H, json={"title": "x"})
    assert r.status_code == 403


def test_secrets_write_only(client):
    assert client.post("/api/secrets/hf_token", headers=H, json={"value": "abc"}).status_code == 200
    body = client.get("/api/settings", headers=H).json()
    assert body["hf_token_is_set"] is True
    # no endpoint returns the value
    assert client.post("/api/secrets/unknown", headers=H, json={"value": "x"}).status_code == 404
    client.ai_state.secrets.clear("hf_token")


def test_settings_update_and_validation(client):
    r = client.put("/api/settings", headers=H, json={"consent_acknowledged": True})
    assert r.status_code == 200
    assert r.json()["consent_acknowledged"] is True
    bad = client.put("/api/settings", headers=H, json={"hardware_preset": "nope"})
    assert bad.status_code == 422


def test_catchup_since_seq(client):
    store: SessionStore = client.ai_store
    sess = store.create("cu")
    from tests.unit.test_store import _rec

    for i in range(3):
        store.append_utterance(_rec(store, sess.session_id, text=f"t{i}", start=float(i)))
    r = client.get(f"/api/sessions/{sess.session_id}/utterances?since_seq=1", headers=H)
    assert r.status_code == 200
    seqs = [u["seq"] for u in r.json()]
    assert seqs == [2, 3]


def test_websocket_status_on_connect(client):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"


def test_websocket_bad_token_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()
    assert ei.value.code == 4401


def test_websocket_missing_token_closed_4401(client):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert ei.value.code == 4401


def test_websocket_bad_origin_closed_4403(client):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            f"/ws?token={TOKEN}", headers={"Origin": "http://evil.example.com"}
        ) as ws:
            ws.receive_json()
    assert ei.value.code == 4403


def _fake_client(maxsize):
    class _WS:
        async def close(self, code=1000):
            self.closed_code = code

    return _Client(_WS(), maxsize)


def test_ws_status_coalesced_not_dropped_forever(client):
    """Non-durable STATUS messages coalesce (newest wins) when the queue is full."""
    state = client.ai_state
    state.loop = asyncio.new_event_loop()
    c = _fake_client(maxsize=1)
    state.clients.add(c)
    state._fanout({"type": "status", "note": "old"})
    state._fanout({"type": "status", "note": "new"})  # coalesces, no unbounded growth
    assert c.queue.qsize() == 1
    assert c.queue.get_nowait()["note"] == "new"
    state.clients.discard(c)
    state.loop.close()


def test_ws_durable_marks_lagging_and_catchup_via_since_seq(client):
    """A full queue never silently loses durable utterances; they replay via since_seq."""
    store: SessionStore = client.ai_store
    sess = store.create("cu")
    from tests.unit.test_store import _rec

    state = client.ai_state
    state.loop = asyncio.new_event_loop()
    c = _fake_client(maxsize=1)
    state.clients.add(c)

    # Persist utterances AND fan them out to a client whose queue is already full.
    c.queue.put_nowait({"type": "status"})  # fill the single slot
    for i in range(3):
        store.append_utterance(_rec(store, sess.session_id, text=f"t{i}", start=float(i)))
        state._fanout({"type": "utterance", "record": {"seq": i + 1}})

    assert c.lagging is True
    assert state.ws_drops == 3  # counted, not silently ignored
    # The durable events are recoverable from the store via the catch-up endpoint.
    r = client.get(f"/api/sessions/{sess.session_id}/utterances?since_seq=0", headers=H)
    assert [u["seq"] for u in r.json()] == [1, 2, 3]
    state.clients.discard(c)
    state.loop.close()


def test_ws_durable_client_evicted_past_deadline(client):
    """A client lagging beyond ws_client_slow_deadline_s is evicted (reconnect+replay)."""
    state = client.ai_state
    state.loop = asyncio.new_event_loop()
    state.settings = state.settings.update({"ws_client_slow_deadline_s": 0})
    c = _fake_client(maxsize=1)
    c.queue.put_nowait({"type": "status"})  # full
    state.clients.add(c)
    state._fanout({"type": "utterance", "record": {"seq": 1}})  # deadline 0 → evict now
    assert c not in state.clients
    state.loop.run_until_complete(asyncio.sleep(0))  # drain the scheduled close task
    assert getattr(c.ws, "closed_code", None) == 4402
    state.loop.close()


def test_models_catalog_requires_token(client):
    assert client.get("/api/models/catalog").status_code == 401


def test_models_catalog_returns_default_and_current(client, monkeypatch):
    import ai_record.models as models

    # Force the "ollama installed, two models pulled" path via the mockable helpers.
    monkeypatch.setattr(models, "ollama_available", lambda: True)
    monkeypatch.setattr(models, "list_installed_models", lambda: ["qwen2.5:7b", "llama3.1:8b"])
    r = client.get("/api/models/catalog", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "qwen2.5:7b"
    assert body["current"] == client.ai_state.settings.ollama_model
    assert body["ollama_available"] is True
    assert body["installed"] == ["qwen2.5:7b", "llama3.1:8b"]
    tags = {m["tag"] for m in body["models"]}
    assert "qwen2.5:7b" in tags


def test_models_catalog_ollama_absent(client, monkeypatch):
    import ai_record.models as models

    monkeypatch.setattr(models, "ollama_available", lambda: False)

    def _boom():
        raise AssertionError("list_installed_models must not run when ollama is absent")

    monkeypatch.setattr(models, "list_installed_models", _boom)
    r = client.get("/api/models/catalog", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ollama_available"] is False
    assert body["installed"] == []


def test_traversal_session_id_404(client):
    """A backslash-traversal session id returns 404, never escapes the root."""
    r = client.get("/api/sessions/..%5Cdocs", headers=H)
    assert r.status_code == 404
    r2 = client.delete("/api/sessions/..%5Cdocs", headers=H)
    assert r2.status_code == 404


# --------------------------------------------------------------------------- #
# Auto AI-summary at session end (output_formats includes "summary")
# --------------------------------------------------------------------------- #
def _seed_session(store, text="hello auto summary"):
    from tests.unit.test_store import _rec

    sess = store.create("autosum")
    store.append_utterance(_rec(store, sess.session_id, text=text))
    return sess.session_id


def test_stop_capture_auto_summary_when_selected(client, monkeypatch):
    import ai_record.summarizer as summarizer_mod
    from ai_record.server import _stop_capture
    from ai_record.summarizer import SummaryResult

    state = client.ai_state
    state.settings = state.settings.update({"output_formats": ["md", "summary"]})
    sid = _seed_session(state.store)
    state.active_session_id = sid

    calls = {}

    def fake_build(data, scenario, provider, settings, secrets, **kw):
        calls["scenario"] = scenario
        calls["provider"] = provider
        return SummaryResult(markdown="# Auto\n\nnotes", scenario=scenario, provider=provider)

    monkeypatch.setattr(summarizer_mod, "build_summary", fake_build)

    out = _stop_capture(state)
    assert out == sid
    t = state._summary_threads.get(sid)
    assert t is not None
    t.join(timeout=5)
    assert not t.is_alive()

    summ = state.store._dir(sid) / "summary.md"
    assert summ.exists()
    assert "Auto" in summ.read_text(encoding="utf-8")
    # default scenario is "minutes" when settings has no summarizer_scenario.
    assert calls["scenario"] == "minutes"


def test_stop_capture_no_summary_when_not_selected(client, monkeypatch):
    import ai_record.summarizer as summarizer_mod
    from ai_record.server import _stop_capture

    state = client.ai_state
    state.settings = state.settings.update({"output_formats": ["md"]})
    sid = _seed_session(state.store)
    state.active_session_id = sid

    def boom(*a, **k):
        raise AssertionError("build_summary must not run without 'summary' selected")

    monkeypatch.setattr(summarizer_mod, "build_summary", boom)

    out = _stop_capture(state)
    assert out == sid
    assert sid not in state._summary_threads
    assert not (state.store._dir(sid) / "summary.md").exists()
