"""Entrypoint: preflight → start the server thread → open the pywebview window.

``python -m ai_record`` and ``main.py`` both land here. Uvicorn and pywebview are
imported lazily so importing this module (e.g. for tests) never requires them.
Startup sequence follows SPEC.md §11.2.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import socket
import threading
import time

from .config import Secrets, Settings, resolve_sessions_root, localappdata_dir
from .server import AppState, create_app
from .store import SessionStore

log = logging.getLogger("ai_record")


def _find_free_port(preferred: int, tries: int = 10) -> int:
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def _run_server(app, host: str, port: int) -> None:
    import uvicorn  # type: ignore

    uvicorn.run(app, host=host, port=port, log_level="warning")


def _wait_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    localappdata_dir().mkdir(parents=True, exist_ok=True)

    settings = Settings.load()
    secrets = Secrets()
    store = SessionStore(resolve_sessions_root(settings), settings)

    # Retention + incomplete-session detection (offered as recovery in the UI).
    pruned = store.apply_retention()
    if pruned:
        log.info("retention pruned %d sessions", pruned)
    incomplete = store.detect_incomplete()
    if incomplete:
        log.info("found %d incomplete session(s) for recovery: %s",
                 len(incomplete), [m.session_id for m in incomplete])

    token = _secrets.token_urlsafe(32)
    port = _find_free_port(settings.server_port)
    state = AppState(settings, store=store, secrets=secrets, token=token, port=port)
    app = create_app(state)

    server_thread = threading.Thread(target=_run_server, args=(app, "127.0.0.1", port), daemon=True)
    server_thread.start()
    if not _wait_ready(port):
        log.error("server did not become ready on port %d", port)
        return

    url = f"http://127.0.0.1:{port}?token={token}"
    log.info("ai-record ready at %s", url)

    try:
        import webview  # type: ignore

        webview.create_window(
            "ai-record",
            url,
            width=520,
            height=160,
            frameless=True,
            on_top=True,
            resizable=True,
        )
        webview.start()
    except Exception as exc:
        log.warning("pywebview unavailable (%s); open the URL manually:\n  %s", exc, url)
        try:
            while server_thread.is_alive():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
