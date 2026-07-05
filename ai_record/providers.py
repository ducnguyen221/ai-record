"""Per-machine AI-provider connection status, sign-in, and connection test.

This module makes the AI Summary/Analyze providers (Claude CLI, Codex CLI, Gemini,
Ollama) *connectable per machine* — like activating a CLI in a terminal. On any
machine the user signs in with THAT machine's own account.

SECURITY MODEL (preserved verbatim from the app's design):
    The app NEVER stores or embeds CLI credentials. ``ClaudeCliSummarizer`` /
    ``CodexCliSummarizer`` shell out to the LOCAL ``claude`` / ``codex`` binary,
    which uses that machine's own login (stored in the user's home, outside the
    repo). Gemini uses a key in the OS keychain (keyring). Ollama is local/offline.

    Nothing here reads, captures, logs, or transmits any token or credential. For
    the CLIs we check only for the *existence* of the login/config file (never its
    contents) and, on sign-in, we simply launch the CLI's OWN interactive login in
    a new terminal window so its normal OAuth/browser flow can complete.

Everything is import-safe (no heavy deps, no hardware) and every side-effecting
helper (home dir, Ollama probe, terminal spawn, provider construction) is a small
module-level function so tests can monkeypatch it without touching the real system.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import Secrets, Settings
from .summarizer import (
    SummarizerError,
    SummarizerUnavailable,
    build_summary,
    make_provider,
)

log = logging.getLogger("ai_record.providers")

# Provider identities (kind drives the UI + which actions are offered).
CLI_PROVIDERS = ("claude_cli", "codex_cli")
_LABELS = {
    "claude_cli": "Claude CLI",
    "codex_cli": "Codex CLI",
    "gemini": "Gemini",
    "ollama": "Ollama",
}
_CLI_BINARY = {"claude_cli": "claude", "codex_cli": "codex"}


class ProviderNotInstalled(RuntimeError):
    """The CLI whose login was requested is not installed on this machine."""


# --------------------------------------------------------------------------- #
# Small, monkeypatchable primitives
# --------------------------------------------------------------------------- #
def _home() -> Path:
    """Return the user's home dir (overridable in tests)."""
    return Path.home()


def _which(binary: str) -> str | None:
    """Resolve a binary on PATH (thin wrapper so tests can patch one place)."""
    return shutil.which(binary)


def _ollama_probe(url: str, timeout: float = 2.0) -> tuple[bool, list[str]]:
    """Best-effort ``GET {url}/api/tags``. Returns ``(reachable, model_names)``.

    Never raises; a down/absent server yields ``(False, [])``. Monkeypatched in tests.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = [str(m.get("name") or m.get("model") or "") for m in (data.get("models") or [])]
        return True, [n for n in names if n]
    except Exception:  # pragma: no cover - server not running / offline
        return False, []


# --------------------------------------------------------------------------- #
# CLI sign-in detection — EXISTENCE ONLY, never read file contents
# --------------------------------------------------------------------------- #
def _cli_signed_in(name: str) -> bool | None:
    """Best-effort: does a local login/config artefact exist for this CLI?

    Returns ``True`` if a known auth/config path exists, else ``None`` (UNKNOWN —
    not necessarily logged out). We test path *existence* only and never open the
    file, so no credential is ever read.
    """
    home = _home()
    if name == "claude_cli":
        candidates = [home / ".claude.json", home / ".claude" / ".credentials.json"]
    elif name == "codex_cli":
        candidates = [home / ".codex" / "auth.json", home / ".codex" / "config.json"]
    else:  # pragma: no cover - only CLIs call here
        return None
    for p in candidates:
        try:
            if p.exists():
                return True
        except OSError:  # pragma: no cover - permission/path quirk
            continue
    return None


def _cli_status(name: str) -> dict[str, Any]:
    binary = _CLI_BINARY[name]
    installed = _which(binary) is not None
    signed_in = _cli_signed_in(name) if installed else None
    ready = installed and signed_in is not False
    if not installed:
        detail = f"Chưa cài {binary} trên máy này"
    elif signed_in:
        detail = "Đã đăng nhập trên máy này"
    else:
        detail = "Đã cài — bấm Đăng nhập nếu chưa kết nối"
    return {
        "name": name,
        "label": _LABELS[name],
        "kind": "cli",
        "installed": installed,
        "signed_in": signed_in,
        "ready": bool(ready),
        "detail": detail,
    }


def _gemini_status(secrets: Secrets) -> dict[str, Any]:
    key_set = secrets.is_set("gemini_api_key")
    return {
        "name": "gemini",
        "label": _LABELS["gemini"],
        "kind": "api",
        "installed": True,
        "signed_in": bool(key_set),
        "ready": bool(key_set),
        "detail": "API key đã đặt" if key_set else "Chưa đặt Gemini API key (mục Secrets)",
    }


def _ollama_status(settings: Settings) -> dict[str, Any]:
    reachable, models = _ollama_probe(settings.ollama_url)
    model = settings.ollama_model
    pulled = model in models if models else False
    if not reachable:
        detail = f"Chạy offline — không cần đăng nhập. Không thấy Ollama tại {settings.ollama_url}"
    elif pulled:
        detail = f"Chạy offline — không cần đăng nhập. Model '{model}' đã sẵn sàng"
    else:
        detail = f"Chạy offline — không cần đăng nhập. Model '{model}' chưa tải (ollama pull {model})"
    return {
        "name": "ollama",
        "label": _LABELS["ollama"],
        "kind": "local",
        "installed": reachable,
        "signed_in": None,
        "ready": reachable,
        "detail": detail,
    }


def provider_status(settings: Settings, secrets: Secrets | None = None) -> list[dict[str, Any]]:
    """One status entry per AI provider (SPEC §5.6 + per-machine connection feature).

    Never raises; never reads a credential; existence checks + a 2s local probe only.
    """
    secrets = secrets or Secrets()
    return [
        _cli_status("claude_cli"),
        _cli_status("codex_cli"),
        _gemini_status(secrets),
        _ollama_status(settings),
    ]


# --------------------------------------------------------------------------- #
# Trigger the CLI's OWN interactive login (new visible terminal)
# --------------------------------------------------------------------------- #
def _login_command(name: str, exe: str) -> list[str]:
    """The exact command that opens the CLI's interactive login in a NEW terminal.

    Small, overridable helper. On Windows we spawn a fresh visible ``cmd`` window
    (the app itself is windowless) so the CLI's OAuth/browser flow can complete:
    running ``claude`` interactively triggers its login when unauthenticated; codex
    uses its ``login`` subcommand.
    """
    if name == "claude_cli":
        return ["cmd", "/c", "start", "", "cmd", "/k", exe]
    if name == "codex_cli":
        return ["cmd", "/c", "start", "", "cmd", "/k", exe, "login"]
    raise ValueError(f"no interactive login for {name!r}")


def _spawn(cmd: list[str]) -> None:  # pragma: no cover - real terminal spawn, mocked in tests
    """Launch a detached, VISIBLE terminal (no CREATE_NO_WINDOW — the window is the point)."""
    subprocess.Popen(cmd, close_fds=True)


def launch_cli_login(name: str, settings: Settings | None = None) -> list[str]:
    """Open the local CLI's own interactive login in a new terminal window.

    Returns the launched command (handy for logging/tests). Raises
    :class:`ProviderNotInstalled` if the CLI is absent, so the caller can map it to a
    clear 400. Never touches or transmits credentials — the CLI owns its login.
    """
    binary = _CLI_BINARY.get(name)
    if binary is None:
        raise ValueError(f"no interactive login for {name!r}")
    exe = _which(binary)
    if exe is None:
        raise ProviderNotInstalled(f"Chưa cài {binary} trên máy này — không thể đăng nhập.")
    cmd = _login_command(name, exe)
    _spawn(cmd)
    log.info("launched interactive login for %s", name)
    return cmd


# --------------------------------------------------------------------------- #
# Test connection — the real "am I connected?" check (a tiny real summarize)
# --------------------------------------------------------------------------- #
def _probe_session() -> SimpleNamespace:
    """A 1–2 line fake transcript session for a lightweight connectivity summarize."""
    utterances = [
        SimpleNamespace(
            seq=1, speaker="You", start=0.0, end=1.0, translation=None,
            text="Xin chào, đây là một bài kiểm tra kết nối ngắn.",
        ),
        SimpleNamespace(
            seq=2, speaker="Them", start=1.0, end=2.0, translation=None,
            text="This is a short provider connection test.",
        ),
    ]
    return SimpleNamespace(utterances=utterances, meta={"title": "connection-test"})


def _short(msg: str, limit: int = 300) -> str:
    """Trim an error to a short single line (defensive: never surface a secret)."""
    one = " ".join((msg or "").split())
    return one[:limit]


def test_connection(
    name: str,
    settings: Settings,
    secrets: Secrets | None = None,
    *,
    provider_impl: Any | None = None,
    timeout_s: int | None = 30,
) -> dict[str, Any]:
    """Run a TINY real summarize through ``name`` and report connectivity.

    Uses the existing ``make_provider`` + ``build_summary`` path with a short prompt
    (scenario ``minutes`` → one provider call) and a short timeout. Returns
    ``{"ok": True}`` or ``{"ok": False, "error": "<short reason>"}``; never raises and
    never surfaces a secret.
    """
    secrets = secrets or Secrets()
    if timeout_s is not None:
        try:
            settings = settings.update({"summary_timeout_s": int(timeout_s)})
        except Exception:  # pragma: no cover - defensive
            pass
    session = _probe_session()
    try:
        impl = provider_impl or make_provider(name, settings, secrets)
        build_summary(session, "minutes", name, settings, secrets, provider_impl=impl)
        return {"ok": True}
    except SummarizerUnavailable as exc:
        return {"ok": False, "error": _short(str(exc))}
    except SummarizerError as exc:
        return {"ok": False, "error": _short(str(exc))}
    except Exception as exc:  # any provider/SDK error → concise, secret-free reason
        log.info("provider test failed for %s: %s", name, type(exc).__name__)
        return {"ok": False, "error": _short(f"{type(exc).__name__}: {exc}")}
