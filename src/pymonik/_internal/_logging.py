"""Library-quiet logging.

PymoniK uses ``structlog``'s structured-kwargs API for readability, but
defers level / handler control to the standard library's ``logging``
module. The result:

- All pymonik log calls go through ``logging.getLogger("pymonik.<…>")``.
- That logger has a ``NullHandler`` attached at module load, so the
  library is **silent by default** — the conventional behaviour.
- :func:`enable_logging` attaches a console handler with a coloured
  renderer for users (or examples) that want to see what's happening.
- :func:`silence_logging` drops the handler again (idempotent).
- We *never* call ``structlog.configure(...)`` so the user's own
  structlog config — if they have one — stays untouched.

Modules in this package use ``get_logger(__name__)`` from this module
rather than importing ``structlog`` directly. The structlog kwargs API
still works (``log.info("msg", x=1, y=2)``); under the hood the
processor chain renders to a string and the stdlib logger handles
levels and handlers.
"""

from __future__ import annotations

import logging
import sys
from typing import Union

import structlog

LIB_NAME = "pymonik"


# Concrete renderers, instantiated once and dispatched per record so the
# active choice can be flipped after loggers have been built.
_RENDERERS = {
    "color": structlog.dev.ConsoleRenderer(colors=True),
    "plain": structlog.dev.ConsoleRenderer(colors=False),
    "json": structlog.processors.JSONRenderer(),
}


def _render(logger, method_name, event_dict):
    # Read ``_OPTS`` at call time (not at logger-construction time), so
    # toggling the renderer in :func:`enable_logging` takes effect on
    # already-bound module-level loggers like ``log = get_logger(__name__)``.
    return _RENDERERS[_OPTS["renderer"]](logger, method_name, event_dict)


def _processor_chain():
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        _render,
    ]


def get_logger(name: str = LIB_NAME):
    """Return a bound logger backed by ``logging.getLogger("pymonik.<short>")``.

    Pass ``__name__`` from a module; the leading package name is
    canonicalised to ``pymonik.<last>`` so the stdlib logger tree stays
    flat under one parent.
    """
    if name == LIB_NAME or not name:
        full = LIB_NAME
    else:
        # __name__ is typically "pymonik.session" or "pymonik._internal.submit"
        # — keep the trailing component, anchor under "pymonik".
        short = name.rsplit(".", 1)[-1]
        full = f"{LIB_NAME}.{short}"
    return structlog.wrap_logger(
        logging.getLogger(full),
        processors=_processor_chain(),
    )


def enable_logging(
    level: Union[int, str] = logging.INFO,
    *,
    color: bool = True,
    json: bool = False,
    stream=None,
) -> None:
    """Turn on console logging for pymonik.

    Args:
        level: stdlib logging level (``"INFO"`` / ``"DEBUG"`` / int).
        color: use ANSI colours in the renderer (auto-disabled when not
            attached to a TTY for piped output). Ignored when ``json=True``.
        json: emit one structured JSON record per line. Right choice for
            log-shipping pipelines (Seq's CLEF ingest, ELK, etc.); set by
            the worker entrypoint so pod logs are structured downstream.
        stream: where to write log records. Defaults to ``sys.stderr``.

    Idempotent: subsequent calls replace the previous handler so you
    can flip the level / renderer without leaking handlers.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper())
    if stream is None:
        stream = sys.stderr

    if json:
        _OPTS["renderer"] = "json"
    elif color and getattr(stream, "isatty", lambda: False)():
        _OPTS["renderer"] = "color"
    else:
        # Plain text for piped output: still readable, no ANSI bleed.
        _OPTS["renderer"] = "plain"

    pmk = logging.getLogger(LIB_NAME)
    pmk.handlers.clear()
    handler = logging.StreamHandler(stream=stream)
    # The structlog wrapper already produces a fully-formatted line; just
    # echo it. Adding any stdlib formatter would double-stamp the time.
    handler.setFormatter(logging.Formatter("%(message)s"))
    pmk.addHandler(handler)
    pmk.setLevel(level)
    pmk.propagate = False


def silence_logging() -> None:
    """Drop any handler added by :func:`enable_logging`. Idempotent."""
    pmk = logging.getLogger(LIB_NAME)
    pmk.handlers.clear()
    pmk.addHandler(logging.NullHandler())
    # Reset to default (libraries shouldn't propagate by default either).
    pmk.propagate = False


# Module-level renderer choice. Default ``"plain"`` so module-level
# ``log = get_logger(__name__)`` loggers — bound at import time before
# ``enable_logging`` runs — don't emit ANSI into downstream sinks.
# ``enable_logging`` flips this to ``"color"`` / ``"json"`` as requested.
_OPTS: dict = {"renderer": "plain"}


# Library-default: silent. Convention is to attach a NullHandler so
# stdlib's "no handler found" warning doesn't fire if the user logs
# without configuring.
logging.getLogger(LIB_NAME).addHandler(logging.NullHandler())
logging.getLogger(LIB_NAME).propagate = False
