"""Jupyter / IPython rich-display helpers for Future / FutureList.

Loaded lazily from ``Future._repr_html_`` / ``_ipython_display_`` so the
core library has no IPython dependency. In notebook frontends we paint
an HTML snapshot and start a daemon thread that refreshes it (via
``display_id``) until every tracked future resolves — Modal-style live
progress without ipywidgets. Outside a notebook the thread is never
started; the static snapshot or plain ``__repr__`` is what the user sees.
"""

from __future__ import annotations

import html
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from pymonik.future import Future, FutureList


def _state(fut: "Future[Any]") -> str:
    if fut._error is not None:
        return "error"
    if fut._done.is_set():
        return "done"
    return "pending"


# Scoped class names + a single keyframe; safe to inline more than once
# per cell since duplicate <style> tags cascade harmlessly.
_CSS = """\
<style>
@keyframes pymonik-pulse { 0%,100% { opacity: .55 } 50% { opacity: 1 } }
.pymonik-fut {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 12px; display: inline-flex; align-items: center; gap: 8px;
  padding: 4px 10px; border-radius: 6px; background: #f6f8fa; color: #24292f;
  border: 1px solid #d0d7de;
}
.pymonik-fut .dot { width: 8px; height: 8px; border-radius: 50%; flex: none }
.pymonik-fut.pending .dot { background: #9a6700; animation: pymonik-pulse 1.2s ease-in-out infinite }
.pymonik-fut.done    .dot { background: #1a7f37 }
.pymonik-fut.error   .dot { background: #cf222e }
.pymonik-fut code { background: rgba(175,184,193,.2); padding: 0 4px; border-radius: 3px }

.pymonik-fl {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 12px; color: #24292f; max-width: 720px;
}
.pymonik-fl .head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px }
.pymonik-fl .bar { flex: 1; min-width: 80px; height: 4px; background: #d0d7de; border-radius: 2px; overflow: hidden }
.pymonik-fl .bar > .fill { height: 100%; background: #1a7f37; transition: width 200ms linear }
.pymonik-fl .grid { display: grid; gap: 1px; padding: 1px; background: #d0d7de; border-radius: 3px; width: max-content }
.pymonik-fl .cell { background: #afb8c1 }
.pymonik-fl .cell.pending { background: #afb8c1; animation: pymonik-pulse 1.5s ease-in-out infinite }
.pymonik-fl .cell.done    { background: #1a7f37 }
.pymonik-fl .cell.error   { background: #cf222e }
</style>
"""


def future_html(fut: "Future[Any]") -> str:
    state = _state(fut)
    label = {"pending": "pending", "done": "done", "error": "failed"}[state]
    tid = html.escape(fut._task_id)
    extra = ""
    if state == "error" and fut._error is not None:
        etype = html.escape(type(fut._error).__name__)
        extra = f' <span style="color:#cf222e">· {etype}</span>'
    return (
        _CSS
        + f'<div class="pymonik-fut {state}"><span class="dot"></span>'
        + f'<span>Future</span><code>{tid}</code>'
        + f'<span style="color:#656d76">{label}</span>{extra}</div>'
    )


def future_list_html(fl: "FutureList[Any]") -> str:
    futs = list(fl._futures)
    n = len(futs)
    done = sum(1 for f in futs if f._done.is_set() and f._error is None)
    failed = sum(1 for f in futs if f._error is not None)
    pending = n - done - failed
    pct = ((done + failed) * 100) // n if n else 100

    # Cells shrink as N grows so the heatmap stays around the same width.
    if n <= 256:
        cell_px, cols = 12, min(32, n or 1)
    elif n <= 1024:
        cell_px, cols = 6, min(64, n)
    else:
        cell_px, cols = 3, min(128, n)

    cells: list[str] = []
    for f in futs:
        st = _state(f)
        title = html.escape(f._task_id)
        cells.append(f'<div class="cell {st}" title="{title}"></div>')
    grid_style = f"grid-template-columns: repeat({cols}, {cell_px}px);"
    cell_size_css = (
        f"<style>.pymonik-fl .cell {{ width: {cell_px}px; height: {cell_px}px }}</style>"
    )

    failed_str = (
        f' · <span style="color:#cf222e">{failed} failed</span>' if failed else ""
    )
    return (
        _CSS + cell_size_css
        + '<div class="pymonik-fl">'
        + '<div class="head">'
        + '<strong>FutureList</strong>'
        + f'<span>{done}/{n} done</span>'
        + f'<span style="color:#656d76">· {pending} pending{failed_str}</span>'
        + f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'
        + '</div>'
        + f'<div class="grid" style="{grid_style}">' + "".join(cells) + "</div>"
        + '</div>'
    )


class _Snapshot:
    """Tiny carrier so ``display(...)`` picks HTML in notebooks, text elsewhere."""

    __slots__ = ("_html", "_text")

    def __init__(self, html: str, text: str) -> None:
        self._html = html
        self._text = text

    def _repr_html_(self) -> str:
        return self._html

    def __repr__(self) -> str:
        return self._text


def _is_jupyter_frontend() -> bool:
    """True only for kernel-backed frontends that re-render display_id updates.

    Terminal IPython would treat each ``handle.update`` as a fresh print —
    spammy and pointless. We opt out there.
    """
    try:
        from IPython import get_ipython  # type: ignore
    except Exception:
        return False
    try:
        ip = get_ipython()
    except Exception:
        return False
    if ip is None:
        return False
    cls = ip.__class__.__name__
    # ZMQInteractiveShell = Jupyter; Shell = Google Colab.
    return cls in ("ZMQInteractiveShell", "Shell")


def display_live(
    obj: Any,
    html_fn: Callable[[Any], str],
    *,
    futures: list["Future[Any]"],
    interval: float = 0.5,
    max_seconds: float = 3600.0,
) -> None:
    """Display ``obj`` once, then refresh until ``futures`` are all done.

    A no-op outside Jupyter/Colab. Static snapshot only when every future
    is already resolved at display time.
    """
    try:
        from IPython.display import display  # type: ignore
    except Exception:
        return

    text_repr = repr(obj)
    snap = _Snapshot(html_fn(obj), text_repr)

    if not _is_jupyter_frontend() or all(f._done.is_set() for f in futures):
        display(snap)
        return

    handle = display(snap, display_id=True)
    if handle is None:
        return

    def _update_loop() -> None:
        import time as _t

        deadline = _t.monotonic() + max_seconds
        while _t.monotonic() < deadline:
            if all(f._done.is_set() for f in futures):
                break
            try:
                handle.update(_Snapshot(html_fn(obj), repr(obj)))
            except Exception:
                return
            _t.sleep(interval)
        try:
            handle.update(_Snapshot(html_fn(obj), repr(obj)))
        except Exception:
            pass

    threading.Thread(target=_update_loop, daemon=True, name="pymonik-display").start()
