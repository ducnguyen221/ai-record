"""Interactive screen-region picker (rubber-band selection over the whole desktop).

:func:`pick_region` shows a fullscreen, semi-transparent, always-on-top overlay
spanning the entire virtual desktop and lets the user drag a rectangle, returning
``{"x","y","w","h"}`` in PHYSICAL pixels (primary-monitor-relative — the same
coordinate space as :mod:`ai_record.screens`) or ``None`` on Esc / cancel / when no
display is available.

CRASH SAFETY (why a subprocess): tkinter/Tcl is single-threaded and MUST run on its
process's main thread. The app's main thread is owned by pywebview, so running Tk on a
*background* thread crashes the whole process inside ``tcl86t.dll`` (observed: pythonw
faulting module tcl86t.dll, exc 0x80000003). So :func:`pick_region` launches a SEPARATE
short-lived Python process that runs the Tk overlay on *its own* main thread and writes
the chosen region to a file. Even if Tk crashes, only that child dies — the app is
untouched. The parent never imports tkinter.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("ai_record.region_picker")

# Bounded wait so a wedged overlay (never dismissed) can't hang the caller forever.
_PICK_TIMEOUT_S = 180.0
# Only ONE overlay at a time.
_PICK_LOCK = threading.Lock()
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _creationflags() -> int:
    # No console window for the (pythonw) child.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _geometry_string(bounds: dict) -> str:
    """Tk geometry ``WxH+X+Y`` covering the whole virtual desktop. Offsets are signed
    and may be negative (a monitor left of / above the primary); Tk accepts ``+-1920``."""
    return (
        f"{int(bounds['w'])}x{int(bounds['h'])}"
        f"+{int(bounds['x'])}+{int(bounds['y'])}"
    )


def pick_region() -> dict | None:
    """Show the overlay in a child process and return the selected region, or ``None``.

    Blocks the caller (bounded by :data:`_PICK_TIMEOUT_S`) until the child exits.
    Re-entrant calls are rejected (→ ``None``). Fully guarded — any failure returns
    ``None`` rather than raising, and a Tk crash in the child cannot take down the app.
    """
    if not _PICK_LOCK.acquire(blocking=False):
        log.info("region picker already active; rejecting concurrent pick_region()")
        return None
    out_path = None
    try:
        fd, out_path = tempfile.mkstemp(prefix="ai-record-region-", suffix=".json")
        os.close(fd)
        proc = subprocess.Popen(
            [sys.executable, "-m", "ai_record.region_picker", "--out", out_path],
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
        try:
            proc.wait(timeout=_PICK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("region picker timed out after %.0fs; cancelling", _PICK_TIMEOUT_S)
            try:
                proc.kill()
            except Exception:
                pass
            return None
        # The child writes {"region": {...}|null}; read it back.
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            region = data.get("region")
            return region if region else None
        except Exception as exc:
            log.info("region picker produced no result: %s", exc)
            return None
    except Exception as exc:
        log.info("region picker unavailable: %s", exc)
        return None
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except Exception:
                pass
        _PICK_LOCK.release()


# --------------------------------------------------------------------------- #
# Child process: the actual Tk overlay, run on ITS OWN main thread.
# --------------------------------------------------------------------------- #
def _run_overlay() -> dict | None:
    """Run the fullscreen rubber-band overlay on the current (main) thread.

    Returns the selected region dict or ``None``. Import-safe: tkinter is imported
    here, inside the child; any failure returns ``None``.
    """
    import tkinter as tk  # noqa: PLC0415 — child-only, lazy on purpose

    from . import screens

    screens.set_dpi_aware()
    bounds = screens.virtual_screen_bounds()
    result: dict = {"region": None}

    root = tk.Tk()
    root.overrideredirect(True)
    try:
        root.attributes("-alpha", 0.3)
    except Exception:
        pass
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    # Cover the WHOLE virtual desktop (all monitors). No ``-fullscreen`` (on Windows it
    # snaps to a single monitor); overrideredirect + signed geometry + topmost spans all.
    root.geometry(_geometry_string(bounds))
    root.configure(bg="black")

    canvas = tk.Canvas(root, cursor="cross", bg="gray15", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    state: dict = {"sx": 0, "sy": 0, "cx": 0, "cy": 0, "rect": None}

    def on_press(e) -> None:
        state["sx"], state["sy"] = e.x_root, e.y_root
        state["cx"], state["cy"] = e.x, e.y
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def on_drag(e) -> None:
        if state["rect"] is not None:
            canvas.coords(state["rect"], state["cx"], state["cy"], e.x, e.y)

    def on_release(e) -> None:
        x1, y1 = e.x_root, e.y_root
        rx, ry = min(state["sx"], x1), min(state["sy"], y1)
        rw, rh = abs(x1 - state["sx"]), abs(y1 - state["sy"])
        if rw >= 2 and rh >= 2:
            result["region"] = screens.sanitize_region(
                {"x": rx, "y": ry, "w": rw, "h": rh}, bounds=bounds
            )
        else:
            result["region"] = None
        _close()

    def on_cancel(_e=None) -> None:
        result["region"] = None
        _close()

    def _close() -> None:
        try:
            root.destroy()
        except Exception:
            pass

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_cancel)
    try:
        root.focus_force()
    except Exception:
        pass

    root.mainloop()
    return result["region"]


def _main(argv: list[str]) -> int:
    out_path = None
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
    region = None
    try:
        region = _run_overlay()
    except Exception as exc:  # display/tkinter unavailable → cancel
        logging.getLogger("ai_record.region_picker").info("overlay failed: %s", exc)
        region = None
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump({"region": region}, fh)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
