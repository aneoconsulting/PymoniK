"""Partition name mapping (``PymonikClient(partition_map=...)``).

A map sends the partition names a program uses to the partitions a
cluster has. A name that is a key maps to its value. Any other name maps
to the value of the ``"*"`` key, or stays as it is when there is no
``"*"``.

Task objects pickled inside a submission (tasks that a task body spawns
or tail-calls) carry the mapped name, so sub-tasks submitted by a worker
land on the same partitions as the client's.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager

_shipping: contextvars.ContextVar[Mapping[str, str] | None] = contextvars.ContextVar(
    "_pymonik_shipping_partition_map", default=None
)


def resolve(name: str, partition_map: Mapping[str, str] | None) -> str:
    """The partition ``name`` maps to. Empty names stay empty (the session default)."""
    if not name or not partition_map:
        return name
    if name in partition_map:
        return partition_map[name]
    return partition_map.get("*", name)


def resolve_all(names: Iterable[str], partition_map: Mapping[str, str] | None) -> tuple[str, ...]:
    """Map each name, keep the first occurrence of each result, in order."""
    return tuple(dict.fromkeys(resolve(n, partition_map) for n in names))


@contextmanager
def shipping(partition_map: Mapping[str, str] | None) -> Iterator[None]:
    """While active, pickled ``Task`` objects carry mapped partition names."""
    token = _shipping.set(partition_map)
    try:
        yield
    finally:
        _shipping.reset(token)


def shipping_map() -> Mapping[str, str] | None:
    return _shipping.get()
