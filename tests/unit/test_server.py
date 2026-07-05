import pytest
from fastapi.testclient import TestClient

from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.server import AppState, create_app
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


def test_websocket_bad_token_closed(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()
