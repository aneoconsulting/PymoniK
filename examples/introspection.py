"""Fluent introspection: ``client.tasks.where(...).list()`` and friends.

Cluster-wide queries on the client, session-scoped queries on the
session, mutation verbs on each:

- ``client.tasks.where(status=ERROR).cancel()``
- ``session.results.where(status=COMPLETED).download()``
- ``client.sessions.where(status=PAUSED).resume()``

Field names are homogenised: ``id`` works on every resource (task,
result, session, partition); the upstream-native names (``task_id``,
``result_id``, ``session_id``) also resolve so either shape is fine.

This example walks through the read paths against your real cluster
and demonstrates the query / mutation verbs without actually destroying
anything.

    uv run python examples/introspection.py --partition pymonikv1
"""

from __future__ import annotations

import argparse

from armonik.common import SessionStatus, TaskStatus

from pymonik import PymonikClient, task
import pymonik


@task
def add(a: int, b: int) -> int:
    return a + b


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    with PymonikClient() as client:
        # ---- cluster-wide reads ----
        print("== partitions ==")
        for p in client.partitions.order_by("priority").list():
            print(f"  {p.id:<14}  priority={p.priority}  pod_max={p.pod_max}")

        print("\n== sessions (newest 5, completed only)  via .list() ==")
        completed_sessions = (
            client.sessions
            .where(status=SessionStatus.CLOSED)
            .order_by("-status")     # only STATUS is filterable upstream
            .limit(5)
            .list()
        )
        for s in completed_sessions:
            print(f"  {s.id}  status={s.status.name}  parts={s.partition_ids}")

        print(f"\n== sessions count by status ==")
        for st_name in ("RUNNING", "PAUSED", "CLOSED", "CANCELLED"):
            try:
                st = getattr(SessionStatus, st_name)
                n = client.sessions.where(status=st).count()
                print(f"  {st_name:<10} {n}")
            except AttributeError:
                pass  # status name differs across armonik versions

        # ---- submit a few tasks so we have something to query ----
        with client.session(partition=args.partition) as s:
            print(f"\n== running 4 tasks in session {s.session_id[:8]}… ==")
            futs = add.map(range(4), range(1, 5))
            results = [f.result(timeout=60) for f in futs]
            print(f"  results: {results}")

            # ---- session-scoped reads ----
            print("\n== this session's tasks (homogenised .id) ==")
            for t in s.tasks.order_by("created_at").list():
                print(
                    f"  id={t.id[:8]}…  status={t.status.name}  "
                    f"partition={t.partition_id}"
                )

            print("\n== count by status (cluster total + this session) ==")
            cluster_completed = client.tasks.where(status=TaskStatus.COMPLETED).count()
            sess_completed = s.tasks.where(status=TaskStatus.COMPLETED).count()
            print(f"  COMPLETED — cluster={cluster_completed}  session={sess_completed}")

            print("\n== results in this session, by status ==")
            n_completed = s.results.where(status=2).count()  # ResultStatus.COMPLETED == 2
            print(f"  results in session: {n_completed} completed")

            print("\n== results.first() (id = homogenised result_id) ==")
            r = s.results.first()
            if r:
                print(f"  first: id={r.id[:8]}…  status={r.status.name}  "
                      f"size={r.size_bytes}  name={r.name!r}")

            # ---- iteration with limits ----
            print("\n== async iteration over a fan-out (limit=3) ==")
            for t in s.tasks.where(status=TaskStatus.COMPLETED).limit(3):
                print(f"  -> {t.id[:8]}… completed at {t.ended_at}")

            # ---- mutations: kept conservative; no actual delete here ----
            print("\n== predicate suffixes ==")
            print(f"  by id__in:        {s.tasks.where(id__in=[t.id for t in s.tasks.list()[:2]]).count()}")
            print(f"  by status__ne:    {s.tasks.where(status__ne=TaskStatus.ERROR).count()}")
            try:
                print(f"  by partition__startswith='py': "
                      f"{client.tasks.where(partition_id__startswith='py').limit(5).count()}")
            except ValueError as e:
                # not all clusters support all suffixes on every field
                print(f"  startswith query unsupported on this cluster: {e}")

            print("\n== mutation surface (NOT firing — just shape) ==")
            print("  s.tasks.where(status=TaskStatus.ERROR).cancel()        # int")
            print("  s.results.where(status=2).delete(batch_size=100)        # int")
            print("  s.results.where(status=2).download()                    # dict[id, bytes]")
            print("  s.results.where(status=2).download_to('./out')          # int (files)")
            print("  client.sessions.where(status=...).pause() / .resume() / .close() / .cancel() / .delete() / .purge()")


if __name__ == "__main__":
    main()
