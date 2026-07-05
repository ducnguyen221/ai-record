"""Entrypoint: preflight → start the server thread → open the pywebview window.

``python -m ai_record`` and ``main.py`` both land here. Uvicorn and pywebview are
imported lazily so importing this module (e.g. for tests) never requires them.
Startup sequence follows SPEC.md §11.2.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Secrets, Settings, resolve_sessions_root, localappdata_dir
from .server import AppState, create_app, _stop_capture
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


def _apply_windows_taskbar_icon(ico_path: str, title: str) -> None:
    """Windows: set an explicit AppUserModelID and push the .ico onto the window via
    WM_SETICON so the TASKBAR shows the AI Record logo instead of pythonw.exe's icon.
    Best-effort; a background thread waits for the (frameless) window to exist."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DucNguyen.AIRecord")
    except Exception:
        log.debug("AUMID set failed", exc_info=True)

    def _worker() -> None:
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            pid = k.GetCurrentProcessId()
            WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
            IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
            big = u.LoadImageW(0, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            small = u.LoadImageW(0, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if not big and not small:
                log.warning("taskbar icon: could not load %s", ico_path)
                return
            proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            found: list[int] = []

            def _cb(hwnd, _lp):
                wpid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                # top-level, visible window owned by THIS process = the app window
                if wpid.value == pid and u.IsWindowVisible(hwnd) and not u.GetWindow(hwnd, 4):
                    found.append(hwnd)
                return True

            cb = proc(_cb)
            for _ in range(80):
                found.clear()
                u.EnumWindows(cb, 0)
                if found:
                    GWL_STYLE, WS_THICKFRAME, WS_MAXIMIZEBOX = -16, 0x40000, 0x10000
                    SWP = 2 | 1 | 4 | 0x20  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
                    for hwnd in found:
                        if big:
                            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
                        if small:
                            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
                        # Frameless windows lack a sizing border → can't be resized by
                        # dragging edges/corners. Add WS_THICKFRAME so the user can.
                        st = u.GetWindowLongW(hwnd, GWL_STYLE)
                        u.SetWindowLongW(hwnd, GWL_STYLE, st | WS_THICKFRAME | WS_MAXIMIZEBOX)
                        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
                    log.info("taskbar icon + resize border applied to %d window(s)", len(found))
                    return
                time.sleep(0.2)
            log.warning("taskbar icon: no owned top-level window found")
        except Exception:
            log.debug("taskbar icon set failed", exc_info=True)

    threading.Thread(target=_worker, daemon=True).start()


def main() -> None:
    localappdata_dir().mkdir(parents=True, exist_ok=True)
    # pythonw.exe (windowless / desktop shortcut) has NO console: sys.stdout and
    # sys.stderr are None. Libraries that write there (uvicorn's logging) would then
    # crash the server thread before it binds -> "server did not become ready".
    # Point the streams at a file so windowless startup succeeds.
    if sys.stdout is None or sys.stderr is None:
        _console = open(localappdata_dir() / "ai-record.console.log", "a",
                        encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _console
        if sys.stderr is None:
            sys.stderr = _console
    # Log to a file so the app runs windowless (pythonw, no console) but still leaves diagnostics.
    _handlers: list[logging.Handler] = [
        RotatingFileHandler(localappdata_dir() / "ai-record.log",
                            maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stderr is not None:  # console builds only; pythonw has no stderr
        _handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=_handlers)

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
        try:
            import webview  # type: ignore

            class _WindowApi:
                """Exposed to the page as window.pywebview.api; lets the UI resize
                the native window when switching compact <-> expanded views."""
                def __init__(self) -> None:
                    self._window = None

                def resize(self, width, height):
                    if self._window is not None:
                        try:
                            self._window.resize(int(width), int(height))
                        except Exception:
                            log.debug("window resize failed", exc_info=True)

                def exit(self):
                    """Quit cleanly: destroy the window so webview.start() returns and
                    the finally-block below stops capture + finalizes the session."""
                    if self._window is not None:
                        try:
                            self._window.destroy()
                        except Exception:
                            log.debug("window destroy failed", exc_info=True)

                def open_external(self, target_url):
                    """Open a URL in the user's default browser (e.g. the logo -> website)."""
                    try:
                        import webbrowser
                        webbrowser.open(str(target_url))
                    except Exception:
                        log.debug("open_external failed", exc_info=True)

            _api = _WindowApi()
            _icon = str(Path(__file__).resolve().parent / "assets" / "ai-record.ico")
            _apply_windows_taskbar_icon(_icon, "AI Record")
            _api._window = webview.create_window(
                "AI Record",
                url,
                js_api=_api,
                width=560,
                height=250,          # compact size; tall enough that the Translate popover fits w/o scroll (matches app.js)
                min_size=(380, 120),
                frameless=True,
                on_top=True,
                resizable=True,
            )
            # Blocks until the user closes the window.
            try:
                webview.start(icon=_icon)
            except TypeError:
                webview.start()  # older pywebview without the icon kwarg
        except Exception as exc:
            log.warning("pywebview unavailable (%s); open the URL manually:\n  %s", exc, url)
            try:
                while server_thread.is_alive():
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
    finally:
        # Window closed (or Ctrl-C): stop capture, finalize the active session,
        # and join capture/pipeline threads. The daemon server thread dies on exit.
        log.info("shutting down: stopping capture and finalizing session")
        _stop_capture(state)


if __name__ == "__main__":
    main()
