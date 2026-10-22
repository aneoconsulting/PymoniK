"""A cancellable ``Events.GetEvents`` stream.

``armonik.client.ArmoniKEvents.get_events`` keeps the gRPC call to itself,
so a reader blocked on it only wakes up on the next event or when the
server ends the stream. ``EventStream`` holds the call, so ``cancel()``
ends the stream at once from another thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from typing import Any, cast

from armonik.client.events import ArmoniKEvents
from armonik.common import EventTypes
from armonik.protogen.client.events_service_pb2_grpc import EventsStub
from armonik.protogen.common.events_common_pb2 import EventSubscriptionRequest
from armonik.protogen.common.results_filters_pb2 import Filters as ResultFilters
from armonik.protogen.common.tasks_filters_pb2 import Filters as TaskFilters


class EventStream:
    """Iterate ``(event_type, event)`` pairs of one session.

    The event objects are the ``armonik.common`` dataclasses
    (``ResultStatusUpdateEvent``, ``NewTaskEvent``, ...). Iteration ends
    when the server closes the stream. After ``cancel()`` it raises
    ``grpc.RpcError`` (status ``CANCELLED``), or ends at once when
    cancelled before it started.
    """

    def __init__(self, channel: Any, session_id: str, event_types: Iterable[EventTypes]) -> None:
        self._stub = EventsStub(channel)
        self._request = EventSubscriptionRequest(
            session_id=session_id,
            returned_events=cast(Any, list(event_types)),
            tasks_filters=TaskFilters(),
            results_filters=ResultFilters(),
        )
        self._lock = threading.Lock()
        self._call: Any = None
        self._cancelled = False

    def __iter__(self) -> Iterator[tuple[EventTypes, Any]]:
        with self._lock:
            if self._cancelled:
                return
            self._call = self._stub.GetEvents(self._request)
        for message in self._call:
            kind = message.WhichOneof("update")
            parse = ArmoniKEvents._events_obj_mapping.get(kind)
            if parse is None:
                continue
            yield EventTypes.from_string(kind), parse.from_raw_event(getattr(message, kind))

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            call = self._call
        if call is not None:
            call.cancel()
