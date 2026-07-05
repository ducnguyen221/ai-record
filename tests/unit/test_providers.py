"""Per-machine AI-provider connection: status, sign-in launcher, and test-connection.

Nothing here touches the real system: ``shutil.which``, the home dir, the Ollama
probe, the terminal spawn, and provider construction are all monkeypatched. No real
CLI runs and no terminal window is ever opened.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ai_record.providers as providers
from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.server import AppState, create_app
from ai_record.store import SessionStore

TOKEN = "test-token-123"
H = {"X-AI-Record-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    settings = Settings(sessions_root=str(tmp_path / "s"), hardware_preset="cpu")
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN, port=8848)
    with TestClient(create_app(state)) as c:
        c.ai_state = state
        yield c


# --------------------------------------------------------------------------- #
# provider_status
# --------------------------------------------------------------------------- #
def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_status_cli_installed_and_signed_in(monkeypatch, tmp_path, settings):
    """claude_cli installed + a login file present → signed_in True, ready True."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")  # existence only
    monkeypatch.setattr(providers, "_home", lambda: home)
    monkeypatch.setattr(providers, "_which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))

    rows = _by_name(providers.provider_status(settings, Secrets()))
    claude = rows["claude_cli"]
    assert claude["kind"] == "cli"
    assert claude["installed"] is True
    assert claude["signed_in"] is True
    assert claude["ready"] is True


def test_status_cli_installed_unknown_signin(monkeypatch, tmp_path, settings):
    """Installed but no login file → signed_in None (UNKNOWN), ready still True."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(providers, "_home", lambda: home)
    monkeypatch.setattr(providers, "_which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))

    rows = _by_name(providers.provider_status(settings, Secrets()))
    codex = rows["codex_cli"]
    assert codex["installed"] is True
    assert codex["signed_in"] is None
    assert codex["ready"] is True  # unknown != logged-out


def test_status_cli_not_installed(monkeypatch, tmp_path, settings):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(providers, "_home", lambda: home)
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))

    rows = _by_name(providers.provider_status(settings, Secrets()))
    claude = rows["claude_cli"]
    assert claude["installed"] is False
    assert claude["signed_in"] is None
    assert claude["ready"] is False


def test_status_gemini_reflects_key(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(providers, "_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))

    class FakeSecrets:
        def __init__(self, val):
            self._val = val

        def is_set(self, name):
            return bool(self._val)

    unset = _by_name(providers.provider_status(settings, FakeSecrets(None)))["gemini"]
    assert unset["kind"] == "api"
    assert unset["installed"] is True
    assert unset["signed_in"] is False
    assert unset["ready"] is False

    setrow = _by_name(providers.provider_status(settings, FakeSecrets("k")))["gemini"]
    assert setrow["signed_in"] is True
    assert setrow["ready"] is True


def test_status_ollama_reachable_and_model_pulled(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(providers, "_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(
        providers, "_ollama_probe", lambda url, timeout=2.0: (True, [settings.ollama_model])
    )
    row = _by_name(providers.provider_status(settings, Secrets()))["ollama"]
    assert row["kind"] == "local"
    assert row["installed"] is True
    assert row["ready"] is True
    assert row["signed_in"] is None
    assert "offline" in row["detail"].lower()


def test_status_ollama_unreachable(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(providers, "_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))
    row = _by_name(providers.provider_status(settings, Secrets()))["ollama"]
    assert row["ready"] is False


# --------------------------------------------------------------------------- #
# launch_cli_login (launcher/spawn MOCKED — never opens a terminal)
# --------------------------------------------------------------------------- #
def test_launch_login_spawns_command(monkeypatch):
    spawned = {}
    monkeypatch.setattr(providers, "_which", lambda b: f"C:/tools/{b}.CMD")
    monkeypatch.setattr(providers, "_spawn", lambda cmd: spawned.setdefault("cmd", cmd))

    cmd = providers.launch_cli_login("claude_cli")
    assert spawned["cmd"] == cmd
    assert any("claude" in str(a).lower() for a in cmd)

    cmd2 = providers.launch_cli_login("codex_cli")
    assert "login" in cmd2  # codex uses its login subcommand


def test_launch_login_not_installed_raises(monkeypatch):
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(providers, "_spawn", lambda cmd: pytest.fail("must not spawn"))
    with pytest.raises(providers.ProviderNotInstalled):
        providers.launch_cli_login("claude_cli")


# --------------------------------------------------------------------------- #
# test_connection (provider MOCKED — never runs a real CLI)
# --------------------------------------------------------------------------- #
class _OkProvider:
    name = "mock"

    def available(self):
        return True, ""

    def summarize(self, prompt, transcript_text, meta):
        return "## Notes\n- ok"


class _FailProvider:
    name = "mock"

    def available(self):
        from ai_record.summarizer import SummarizerUnavailable

        raise SummarizerUnavailable("not signed in")

    def summarize(self, *a):  # pragma: no cover - never reached
        raise AssertionError("summarize must not run when unavailable")


def test_test_connection_ok(settings):
    res = providers.test_connection("claude_cli", settings, Secrets(), provider_impl=_OkProvider())
    assert res == {"ok": True}


def test_test_connection_failure_is_reported(settings):
    res = providers.test_connection("claude_cli", settings, Secrets(), provider_impl=_FailProvider())
    assert res["ok"] is False
    assert "not signed in" in res["error"]


def test_test_connection_uses_make_provider(monkeypatch, settings):
    """When no impl is injected, it builds one via make_provider (patchable)."""
    monkeypatch.setattr(providers, "make_provider", lambda *a, **k: _OkProvider())
    res = providers.test_connection("ollama", settings, Secrets())
    assert res == {"ok": True}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_status_endpoint_requires_token(client):
    assert client.get("/api/providers/status").status_code == 401


def test_status_endpoint_shape(client, monkeypatch):
    monkeypatch.setattr(providers, "_which", lambda b: None)
    monkeypatch.setattr(providers, "_ollama_probe", lambda url, timeout=2.0: (False, []))
    r = client.get("/api/providers/status", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == client.ai_state.settings.summarizer_provider
    names = {p["name"] for p in body["providers"]}
    assert names == {"claude_cli", "codex_cli", "gemini", "ollama"}
    for p in body["providers"]:
        assert set(p) >= {"name", "label", "kind", "installed", "signed_in", "ready", "detail"}


def test_login_endpoint_cli_launched(client, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "launch_cli_login", lambda name, settings=None: calls.append(name))
    r = client.post("/api/providers/claude_cli/login", headers=H)
    assert r.status_code == 200
    assert r.json()["launched"] is True
    assert "hint" in r.json()
    assert calls == ["claude_cli"]


def test_login_endpoint_gemini_and_ollama_400(client):
    for name in ("gemini", "ollama"):
        r = client.post(f"/api/providers/{name}/login", headers=H)
        assert r.status_code == 400
        assert "error" in r.json()


def test_login_endpoint_not_installed_400(client, monkeypatch):
    def _boom(name, settings=None):
        raise providers.ProviderNotInstalled("Chưa cài claude trên máy này")

    monkeypatch.setattr(providers, "launch_cli_login", _boom)
    r = client.post("/api/providers/codex_cli/login", headers=H)
    assert r.status_code == 400
    assert "error" in r.json()


def test_login_endpoint_unknown_provider_404(client):
    assert client.post("/api/providers/nope/login", headers=H).status_code == 404


def test_test_endpoint_ok(client, monkeypatch):
    monkeypatch.setattr(
        providers, "test_connection", lambda *a, **k: {"ok": True}
    )
    r = client.post("/api/providers/claude_cli/test", headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_test_endpoint_failure_shape(client, monkeypatch):
    monkeypatch.setattr(
        providers, "test_connection", lambda *a, **k: {"ok": False, "error": "boom"}
    )
    r = client.post("/api/providers/gemini/test", headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "boom"}


def test_test_endpoint_unknown_provider_404(client):
    assert client.post("/api/providers/nope/test", headers=H).status_code == 404
