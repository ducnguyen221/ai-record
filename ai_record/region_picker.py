"""Interactive screen-region picker (rubber-band selection over the whole desktop).

:func:`pick_region` shows a fullscreen, semi-transparent, always-on-top tkinter
overlay spanning the entire virtual desktop and lets the user drag a rectangle,
returning ``{"x","y","w","h"}`` in PHYSICAL pixels (primary-monitor-relative — the
same coordinate space as :mod:`ai_record.screens`) or ``None`` on Esc / cancel /
when no display is available.

tkinter runs in a DEDICATED thread (the ``Tk()`` root, its bindings, ``mainloop`` and
``destroy`` all live in that one thread) so this is safe to call from a non-main
thread such as a pywebview API handler. The module is import-safe with no display:
``tkinter`` is imported lazily inside the worker, and any failure returns ``None``
rather than raising.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import screens

log = logging.getLogger("ai_record.region_picker")

# Bounded wait so a wedged Tk overlay (never dismissed) can't hang the caller forever.
_PICK_TIMEOUT_S = 180.0
# Only ONE overlay at a time: a second concurrent pick_region() must not spin up a
# second Tk() root (two roots in one process misbehave) — it is rejected → None.
_PICK_LOCK = threading.Lock()


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return True


def _geometry_string(bounds: dict) -> str:
    """Tk geometry ``WxH+X+Y`` covering the whole virtual desktop.

    Offsets are signed and MAY be negative (a monitor left of / above the primary);
    Tk accepts ``+-1920`` for a negative absolute X, so the ``+{x}`` form is
    well-formed for both signs.
    """
    return (
        f"{int(bounds['w'])}x{int(bounds['h'])}"
        f"+{int(bounds['x'])}+{int(bounds['y'])}"
    )


def pick_region() -> dict | None:
    """Show the overlay and return the selected region, or ``None`` on cancel/unavailable.

    Runs the entire tkinter lifecycle on a dedicated daemon thread and blocks the
    caller (bounded by :data:`_PICK_TIMEOUT_S`) until the user finishes (drag-release,
    Esc, timeout, or failure). Re-entrant calls are rejected (→ ``None``). Fully guarded.
    """
    if not _PICK_LOCK.acquire(blocking=False):
        log.info("region picker already active; rejecting concurrent pick_region()")
        return None
    try:
        result: dict[str, Any] = {"region": None, "root": None}
        done = threading.Event()

        def _run() -> None:
            try:
                _run_picker(result)
            except Exception as exc:  # display/tkinter unavailable → cancel, never raise
                log.info("region picker unavailable: %s", exc)
                result["region"] = None
            finally:
                done.set()

        t = threading.Thread(target=_run, name="region-picker", daemon=True)
        t.start()
        if not done.wait(timeout=_PICK_TIMEOUT_S):
            # Timed out: tear down the stashed Tk root so its mainloop exits, then
            # report cancel. Best-effort — destroy is guarded.
            log.warning("region picker timed out after %.0fs; cancelling", _PICK_TIMEOUT_S)
            root = result.get("root")
            if root is not None:
                with _suppress():
                    root.destroy()
            return None
        return result["region"]
    finally:
        _PICK_LOCK.release()


def _run_picker(result: dict) -> None:
    import tkinter as tk

    screens.set_dpi_aware()
    bounds = screens.virtual_screen_bounds()

    root = tk.Tk()
    # Stash the root so a timeout/cancel path in pick_region() can destroy it.
    result["root"] = root
    root.overrideredirect(True)
    with _suppress():
        root.attributes("-alpha", 0.3)
    with _suppress():
        root.attributes("-topmost", True)
    # Cover the WHOLE virtual desktop (all monitors), not just the primary. NOTE: no
    # ``-fullscreen`` — on Windows it snaps the window to a SINGLE monitor, which breaks
    # the overlay on layouts with monitors left of / above the primary. overrideredirect
    # + an explicit signed geometry + topmost spans the whole virtual desktop instead.
    root.geometry(_geometry_string(bounds))
    root.configure(bg="black")

    canvas = tk.Canvas(root, cursor="cross", bg="gray15", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    state: dict[str, Any] = {"sx": 0, "sy": 0, "cx": 0, "cy": 0, "rect": None}

    def on_press(e) -> None:
        # x_root/y_root are absolute screen (physical) coordinates; canvas x/y are
        # local to the overlay and used only to draw the marquee.
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
        with _suppress():
            root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_cancel)
    with _suppress():
        root.focus_force()

    root.mainloop()
