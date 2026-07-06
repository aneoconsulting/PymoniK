"""Worker-side runtime environment builder.

Given an :class:`EnvSpec` from a task envelope, produce a venv at
``<root>/envs/<env_id>/`` containing the requested deps. Concurrent
first-uses for the same ``env_id`` serialise via an OS flock so the
install runs once. Subsequent tasks reuse the venv with ~0 overhead.

Identity rule: ``env_id = sha256(canonical(deps) | py_minor | pmk_ver)``.
Two clients submitting the same deps land in the same venv. The
canonicalisation lower-cases and sorts the deps strings — see
:func:`compute_env_id`.

Eviction: not our job. ``<root>`` is whatever the worker is configured
to use (typically ``/cache/internal``); the polling agent evicts the
whole tree when the cache is full.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from pymonik._internal._logging import get_logger
from pymonik.envelope import EnvSpec
from pymonik.errors import PymonikError

log = get_logger(__name__)


def _pymonik_version() -> str:
    try:
        from pymonik import __version__

        return __version__
    except Exception:
        return "unknown"


def _py_minor() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}"


def canonical_deps(deps: Iterable[str]) -> tuple[str, ...]:
    """Stable representation for hashing: strip, drop empties, lowercase, sort.

    Lowercasing matches PEP 503 normalisation for the *name* portion of a
    requirement; specifiers (``>=``, version numbers) are case-insensitive
    in practice. We don't try to parse PEP 508 here — two textually
    different specifiers that resolve to the same set are different envs,
    by design. The user controls the strings; we don't second-guess.
    """
    cleaned = sorted({d.strip().lower() for d in deps if d.strip()})
    return tuple(cleaned)


def compute_env_id(spec: EnvSpec) -> str:
    """Hash an EnvSpec into a stable id used as the venv directory name."""
    h = hashlib.sha256()
    h.update(b"v=3|")
    h.update(f"py={_py_minor()}|".encode())
    h.update(f"pmk={_pymonik_version()}|".encode())
    h.update(f"index={spec.index_url}|".encode())
    h.update(b"deps=")
    for d in canonical_deps(spec.deps):
        h.update(d.encode())
        h.update(b"\n")
    h.update(b"env=")
    # Env tuple is already sorted client-side; defensively re-sort here.
    for k, v in sorted(spec.env):
        h.update(k.encode())
        h.update(b"=")
        h.update(v.encode())
        h.update(b"\n")
    return h.hexdigest()[:32]


def default_envs_root() -> Path:
    """Worker-side root for venvs.

    Honours ``PYMONIK_ENVS_ROOT`` for tests / dev. In production the
    worker image sets it to ``/cache/internal``; outside the cluster
    we fall back to ``~/.cache/pymonik/envs`` so the same code path
    works under ``LocalCluster``.
    """
    env = os.getenv("PYMONIK_ENVS_ROOT")
    if env:
        return Path(env)
    if Path("/cache/internal").is_dir() and os.access("/cache/internal", os.W_OK):
        return Path("/cache/internal/envs")
    return Path.home() / ".cache" / "pymonik" / "envs"


def _uv_cache_dir(root: Path) -> Path:
    """``UV_CACHE_DIR`` for wheel reuse across env builds.

    Same parent as ``envs/`` so ``/cache/internal`` covers both. Falls
    back when ``PYMONIK_ENVS_ROOT`` is set to something exotic.
    """
    env = os.getenv("UV_CACHE_DIR")
    if env:
        return Path(env)
    return root.parent / "uv-cache"


class EnvBuildError(PymonikError):
    """``uv venv`` or ``uv pip install`` failed for an EnvSpec."""


def _flock(fd: int, op: int) -> None:
    while True:
        try:
            fcntl.flock(fd, op)
            return
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def ensure_env(spec: EnvSpec, *, root: Path | None = None) -> Path:
    """Resolve (or build) the venv for ``spec`` and return its directory.

    Concurrent calls for the same ``env_id`` serialise on a per-env
    lockfile; only one process runs ``uv pip install``. Other callers
    block until the install finishes, then reuse the same venv.

    Returns the venv root (``<root>/<env_id>/.venv``) so the caller can
    pick the python executable or extend ``sys.path`` from it.
    """
    if not spec.deps:
        raise EnvBuildError("ensure_env called with empty deps; nothing to build")

    root = root or default_envs_root()
    env_id = compute_env_id(spec)
    env_dir = root / env_id
    venv_dir = env_dir / ".venv"
    sentinel = env_dir / ".ready"
    lockfile = env_dir / ".lock"

    if sentinel.is_file() and _venv_python(venv_dir).exists():
        return venv_dir

    env_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lockfile), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under the lock — another process may have built it.
        if sentinel.is_file() and _venv_python(venv_dir).exists():
            return venv_dir

        log.info(
            "env build start",
            env_id=env_id,
            deps=list(canonical_deps(spec.deps)),
            index_url=spec.index_url or None,
        )
        t0 = time.monotonic()

        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

        env = os.environ.copy()
        env.setdefault("UV_CACHE_DIR", str(_uv_cache_dir(root)))
        env.setdefault("UV_PYTHON_DOWNLOADS", "never")

        # Build the venv against the worker's interpreter so cloudpickle
        # bytecode works the same in both directions. ``uv venv -p`` with
        # an absolute path pins it.
        try:
            subprocess.run(
                ["uv", "venv", "-p", sys.executable, str(venv_dir)],
                env=env,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise EnvBuildError(
                f"`uv venv` failed for env_id={env_id}: {e.stderr.decode(errors='replace')}"
            ) from e
        except FileNotFoundError as e:
            raise EnvBuildError(
                "uv is not on PATH; the worker image must include `uv`"
            ) from e

        install_cmd = ["uv", "pip", "install", "--python", str(_venv_python(venv_dir))]
        if spec.index_url:
            install_cmd.extend(["--index-url", spec.index_url])
        install_cmd.extend(spec.deps)
        try:
            subprocess.run(
                install_cmd,
                env=env,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise EnvBuildError(
                f"`uv pip install` failed for env_id={env_id}: "
                f"{e.stderr.decode(errors='replace')}"
            ) from e

        sentinel.write_text(f"{env_id}\n")
        log.info(
            "env build done",
            env_id=env_id,
            elapsed_s=round(time.monotonic() - t0, 2),
            venv=str(venv_dir),
        )
        return venv_dir
    finally:
        try:
            _flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def apply_env_overlay(env: tuple[tuple[str, str], ...]) -> dict[str, str | None]:
    """Apply env vars to ``os.environ``, returning a snapshot of prior values.

    Use with :func:`restore_env_overlay`. Keys that didn't exist before
    map to ``None`` so we can pop them on restore.
    """
    prior: dict[str, str | None] = {}
    for k, v in env:
        prior[k] = os.environ.get(k)
        os.environ[k] = v
    return prior


def restore_env_overlay(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def venv_site_packages(venv_dir: Path) -> Path:
    """Return the ``site-packages`` directory inside ``venv_dir``.

    Used by the ``isolate=False`` path to splice the env into the
    worker's ``sys.path`` without spawning a subprocess.
    """
    lib = venv_dir / "lib"
    if not lib.is_dir():
        raise EnvBuildError(f"venv has no lib/: {venv_dir}")
    candidates = [p for p in lib.iterdir() if p.name.startswith("python")]
    if not candidates:
        raise EnvBuildError(f"venv has no lib/python*/: {venv_dir}")
    sp = candidates[0] / "site-packages"
    if not sp.is_dir():
        raise EnvBuildError(f"venv site-packages missing: {sp}")
    return sp
