"""Optional OpenTelemetry instrumentation.

Off by default, no overhead. Two ways to enable:

- ``PymonikClient(otel=True)`` — explicit.
- Standard OTel env vars (``OTEL_EXPORTER_OTLP_ENDPOINT`` /
  ``OTEL_TRACES_EXPORTER`` / ...) detected on construction → auto-enable.

When ``opentelemetry-api`` is not installed, every primitive here is a
no-op. Pymonik runs unchanged. Install with ``pip install pymonik[otel]``
to pull the OTel API + SDK + OTLP exporter.

Two integration points, both zero-cost when disabled:

- :func:`start_span(name, attrs)` — context manager wrapping a code block.
  Yields the span (or None if otel is disabled / not installed) so call
  sites can ``set_attribute`` lazily.
- :func:`inject_context(carrier)` / :func:`use_extracted_context(carrier)`
  — W3C Trace Context propagation. The submit pipeline injects the
  *current* span's context onto the task envelope on the client; the
  worker extracts and attaches it before running the user function.

Resulting trace shape:

    pymonik.session.open
    └── pymonik.submit (count=N, func=...)
        ├── pymonik.task.run [worker pod] (task_id=..., attempt=1)
        ├── pymonik.task.run [worker pod]
        └── ...
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Generator, Mapping

_AVAILABLE = False
_trace: Any = None
_otel_context: Any = None
_propagate: Any = None
SpanKind: Any = None
Status: Any = None
StatusCode: Any = None
Span: Any = Any

try:
    from opentelemetry import context as _otel_context  # noqa: F811
    from opentelemetry import propagate as _propagate  # noqa: F811
    from opentelemetry import trace as _trace  # noqa: F811
    from opentelemetry.trace import (  # noqa: F811
        Span,
        SpanKind,
        Status,
        StatusCode,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover — covered by the no-op tests
    pass


_initialised = False
_enabled = False


def _is_enabled_via_env() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    return any(
        k in os.environ
        for k in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_TRACES_EXPORTER",
        )
    )


def setup(*, force: bool | None = None, service_name: str = "pymonik") -> bool:
    """Initialise OTel. Idempotent across a process.

    ``force=None`` (default) auto-enables when standard OTel env vars are
    set. ``True`` enables unconditionally; ``False`` keeps everything
    no-op even if env vars are present.

    Returns the resulting enabled state.
    """
    global _initialised, _enabled
    if _initialised:
        return _enabled
    _initialised = True

    if not _AVAILABLE:
        return False
    if force is False:
        return False

    enable = force is True or (force is None and _is_enabled_via_env())
    if not enable:
        return False

    # If the user already configured a TracerProvider (e.g. their app uses
    # OTel for other things), don't fight them. Just enable our spans.
    current = _trace.get_tracer_provider()
    if not _is_noop_provider(current):
        _enabled = True
        return True

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # API installed but SDK isn't — user wants their own setup.
        _enabled = True
        return True

    # The standard OTel env var wins over the constructor default; users
    # expect ``OTEL_SERVICE_NAME=...`` to take effect without code change.
    effective_name = os.getenv("OTEL_SERVICE_NAME") or service_name
    resource = Resource.create({"service.name": effective_name})
    provider = TracerProvider(resource=resource)

    exporter = _build_default_exporter()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    _trace.set_tracer_provider(provider)
    _enabled = True

    # Auto-instrument outgoing gRPC calls so the W3C ``traceparent``
    # header lands in every RPC's metadata. ArmoniK.Core's AspNetCore
    # middleware extracts it server-side and chains its activities
    # under ours. The instrumentor is optional; if it can't be
    # installed (missing dep, environment quirk), log loudly so the
    # user knows the cluster-side spans won't link to client spans.
    try:
        from opentelemetry.instrumentation.grpc import (  # type: ignore[import-not-found]
            GrpcInstrumentorClient,
        )
    except ImportError as e:
        from pymonik._internal._logging import get_logger

        get_logger(__name__).warning(
            "otel: gRPC client instrumentation unavailable — "
            "ArmoniK control-plane and agent spans won't chain into "
            "client traces. Install pymonik[otel] (which pulls "
            "opentelemetry-instrumentation-grpc).",
            error=str(e),
        )
    else:
        try:
            GrpcInstrumentorClient().instrument()
        except Exception as e:  # noqa: BLE001
            from pymonik._internal._logging import get_logger

            get_logger(__name__).warning(
                "otel: gRPC client instrumentation failed to apply — "
                "client traces will be disjoint from cluster-side spans.",
                error=f"{type(e).__name__}: {e}",
            )

    return True


def _is_noop_provider(provider: Any) -> bool:
    name = type(provider).__name__
    return name in ("NoOpTracerProvider", "ProxyTracerProvider", "DefaultTracerProvider")


def _build_default_exporter():
    # Honour OTEL_TRACES_EXPORTER first (the standard env var). "console"
    # is convenient for smoke tests when you don't have a collector yet.
    requested = os.getenv("OTEL_TRACES_EXPORTER", "").lower()
    if requested == "console":
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            return ConsoleSpanExporter()
        except ImportError:
            return None
    if requested in ("otlp", "otlp-grpc", ""):
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            return OTLPSpanExporter()
        except ImportError:
            pass
    if requested in ("otlp-http", ""):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            return OTLPSpanExporter()
        except ImportError:
            pass
    # Last resort: print spans to stdout so the user sees *something*.
    try:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    except ImportError:
        return None


def is_enabled() -> bool:
    return _enabled


def _tracer():
    if not _enabled or not _AVAILABLE:
        return None
    return _trace.get_tracer("pymonik")


def _kind_for(kind: str) -> Any:
    if not _AVAILABLE:
        return None
    return {
        "client": SpanKind.CLIENT,
        "server": SpanKind.SERVER,
        "internal": SpanKind.INTERNAL,
        "producer": SpanKind.PRODUCER,
        "consumer": SpanKind.CONSUMER,
    }.get(kind, SpanKind.INTERNAL)


@contextmanager
def start_span(
    name: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    kind: str = "internal",
) -> Generator[Any, None, None]:
    """Start a span; yield the span object (or ``None`` if otel is off).

    Records exceptions as span events and marks status=ERROR before
    re-raising, so failures show up in the trace UI without extra code
    at the call site.
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(
        name, kind=_kind_for(kind), attributes=dict(attrs) if attrs else None
    ) as span:
        try:
            yield span
        except BaseException as e:
            span.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"))
            span.record_exception(e)
            raise


def start_long_span(
    name: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    kind: str = "internal",
) -> tuple[Any, Any]:
    """Start a span that outlives a ``with`` block. Returns ``(span, token)``.

    Use this for spans whose lifetime is tied to a Python object's
    enter/exit (e.g. a Session that keeps the span open across many
    method calls). Pair with :func:`end_long_span` to close.

    Returns ``(None, None)`` when OTel is disabled — the caller can
    pass these straight to :func:`end_long_span` without checking.
    """
    tracer = _tracer()
    if tracer is None or not _AVAILABLE:
        return (None, None)
    span = tracer.start_span(
        name, kind=_kind_for(kind), attributes=dict(attrs) if attrs else None
    )
    # Make the span the current context so anything started after it
    # (start_span, start_as_current_span, ...) becomes a child.
    ctx = _trace.set_span_in_context(span)
    token = _otel_context.attach(ctx)
    return span, token


def end_long_span(span: Any, token: Any) -> None:
    """Counterpart to :func:`start_long_span`. No-ops on (None, None)."""
    if not _AVAILABLE:
        return
    if token is not None:
        _otel_context.detach(token)
    if span is not None:
        span.end()


def inject_context(carrier: dict[str, str]) -> None:
    """Inject the current trace context into ``carrier`` (W3C headers).

    No-op when otel isn't enabled. Safe to call unconditionally —
    pymonik's submit pipeline does on every batch.
    """
    if not _enabled or not _AVAILABLE:
        return
    _propagate.inject(carrier)


@contextmanager
def use_extracted_context(carrier: Mapping[str, str]) -> Generator[None, None, None]:
    """Attach the trace context found in ``carrier`` for the duration.

    Used by the worker: read ``traceparent`` / ``tracestate`` (or any
    propagator's keys) off ``task_handler.task_options.options``, attach,
    run the task. Spans created inside the block become children of the
    client's submit span.
    """
    if not _enabled or not _AVAILABLE:
        yield
        return
    ctx = _propagate.extract(dict(carrier))
    token = _otel_context.attach(ctx)
    try:
        yield
    finally:
        _otel_context.detach(token)


def current_trace_id_hex() -> str | None:
    """Hex trace id of the current span, or ``None`` if not in a trace.

    Useful for log correlation — the user can grep their UI for the id
    we logged at submission time.
    """
    if not _enabled or not _AVAILABLE:
        return None
    span = _trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}"
