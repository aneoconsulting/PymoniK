"""``map`` and ``starmap`` follow Python stdlib semantics.

- ``map(f, *iterables)`` zips its iterables and applies the task to
  each tuple — like ``builtins.map``.
- ``starmap(f, args_iter)`` takes an iterable of tuples and unpacks
  each as positional args — like ``itertools.starmap``.

The two are different shapes; using the wrong one is the kind of bug
the type checker won't catch (everything is ``Iterable[Any]`` in the
end), so the tests here pin the public surface.
"""

from __future__ import annotations

import pytest

from pymonik import task
from pymonik.testing import LocalCluster


@task
def square(x: int) -> int:
    return x * x


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def add3(a: int, b: int, c: int) -> int:
    return a + b + c


def test_map_single_iterable():
    """One iterable, one positional arg per task."""
    with LocalCluster() as client:
        with client.session() as s:
            futs = square.map([1, 2, 3, 4])
            assert futs.results(timeout=10) == [1, 4, 9, 16]


def test_map_parallel_iterables():
    """Two iterables zipped, two args per task."""
    with LocalCluster() as client:
        with client.session() as s:
            futs = add.map([1, 3, 5], [2, 4, 6])
            assert futs.results(timeout=10) == [3, 7, 11]


def test_map_three_parallel_iterables():
    with LocalCluster() as client:
        with client.session() as s:
            futs = add3.map([1, 1, 1], [2, 2, 2], [3, 4, 5])
            assert futs.results(timeout=10) == [6, 7, 8]


def test_map_zip_stops_at_shortest():
    """Like Python's map: shortest iterable wins."""
    with LocalCluster() as client:
        with client.session() as s:
            futs = add.map([1, 2, 3, 4, 5], [10, 20])
            assert futs.results(timeout=10) == [11, 22]


def test_map_with_no_iterables_raises():
    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(TypeError, match="at least one iterable"):
                square.map()


def test_starmap_unpacks_tuples():
    with LocalCluster() as client:
        with client.session() as s:
            futs = add.starmap([(1, 2), (3, 4), (5, 6)])
            assert futs.results(timeout=10) == [3, 7, 11]


def test_local_call_unchanged():
    """The decorated function still works as a plain function."""
    assert add(2, 3) == 5
    assert square(7) == 49
