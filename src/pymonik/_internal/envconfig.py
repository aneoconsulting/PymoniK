"""Settings read from the environment (``PYMONIK_*``)."""

from __future__ import annotations

import os


def env_mapping(name: str) -> dict[str, str] | None:
    """Parse ``key=value,key=value`` from the environment variable ``name``.

    ``None`` when the variable is unset or empty.
    """
    text = os.getenv(name, "").strip()
    if not text:
        return None
    out: dict[str, str] = {}
    for item in text.split(","):
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{name}: expected key=value pairs separated by commas, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def env_int(name: str, default: int) -> int:
    text = os.getenv(name, "").strip()
    return int(text) if text else default


def backend() -> str:
    """``PYMONIK_BACKEND``: ``"armonik"`` (default) or ``"local"``."""
    value = os.getenv("PYMONIK_BACKEND", "").strip().lower() or "armonik"
    if value not in ("armonik", "local"):
        raise ValueError(f"PYMONIK_BACKEND={value!r}: expected 'armonik' or 'local'")
    return value
