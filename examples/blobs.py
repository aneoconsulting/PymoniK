"""Blobs — explicit upload, materialize-to-path, and auto-spill.

Three flows in one example:

1. ``blob.upload(Path(...))`` — file contents delivered to the task as bytes.
2. ``blob.materialize(Path(...), at=...)`` — file written to the worker FS at a
   specific path; the task parameter receives a ``pathlib.Path`` to it.
3. Auto-spill — a large plain-Python arg (above the spill threshold) is
   transparently uploaded and rewired as a data dependency. User code looks
   identical to the inline form.

Also demonstrates content-hash dedup: the second upload of the same bytes
reuses the first result id and skips the network round-trip.

    uv run python examples/blobs.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from pymonik import PymonikClient, blob, current, task
import pymonik


@task
def fingerprint_bytes(label: str, payload: bytes) -> str:
    ctx = current()
    ctx.log.info("task got bytes", label=label, size=len(payload))
    return f"{label}: {len(payload)} bytes; head={payload[:8]!r}"


@task
def read_config(cfg: Path) -> str:
    ctx = current()
    ctx.log.info("task reads materialized file", path=str(cfg))
    return f"config at {cfg} says: {cfg.read_text().strip()!r}"


@task
def sum_samples(samples: list[float]) -> float:
    """Receives a (possibly auto-spilled) big list. User code is oblivious."""
    current().log.info("summing", n=len(samples))
    return sum(samples)


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--samples", type=int, default=200_000)
    args = ap.parse_args()

    # Make two tiny local files we can blob/materialize.
    with tempfile.TemporaryDirectory() as tmp:
        weights_path = Path(tmp) / "weights.bin"
        weights_path.write_bytes(b"WEIGHTS" + b"\x01" * 10_000)

        cfg_path = Path(tmp) / "app.toml"
        cfg_path.write_text("mode='production'\nvalue=42\n")

        with PymonikClient() as client:
            with client.session(partition=args.partition) as s:
                # (1) Explicit blob: file bytes → delivered as `bytes`.
                weights = blob.upload(weights_path)
                print("uploaded:", weights)

                # Dedup: second call finds the cache and returns the same handle shape.
                weights_again = blob.upload(weights_path)
                assert weights.result_id == weights_again.result_id, "dedup failed"
                print("second upload reused:", weights_again.result_id[:8] + "…")

                # (2) Materialize: worker writes the bytes to this path before the task.
                cfg = blob.materialize(cfg_path, at="/tmp/pmk_app.toml")
                print("materialize:", cfg)

                # (3) Auto-spill: a half-million-float list is well above 256 KiB
                # cloudpickled; submission will quietly turn it into a Blob.
                big = [x * 0.01 for x in range(args.samples)]
                print(f"big list size = {args.samples} floats")

                f1 = fingerprint_bytes.spawn("weights", weights)
                f2 = read_config.spawn(cfg)
                f3 = sum_samples.spawn(big)

                print(f1.result(timeout=120))
                print(f2.result(timeout=120))
                print(f"sum_samples -> {f3.result(timeout=120):.2f}")


if __name__ == "__main__":
    main()
