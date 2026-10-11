"""Asynchronous analytics writer: a bounded queue, one worker, batched commits.

Design constraints from the specification, and how they are met:

* *The request path must not block.* The recorder only ever calls ``submit``, which
  appends to an unbounded deque and wakes the worker. If the deque exceeds its limit
  the oldest unfinished buffer is counted as dropped instead of being waited on —
  except for payment blocks, which are re-marked so a ``blocked`` fact can never be
  silently lost.
* *One logical request, many attempts.* Each in-flight request owns one
  :class:`RequestBuffer` carrying its logical-request row and its attempt rows; both
  are written together in one transaction.
* *A failing database must not break requests.* Any write error is logged once per
  batch and counted. FreeGuard fails closed independently of analytics.
"""

import asyncio
import contextlib
from collections import deque

from loguru import logger

from free_claude_code.application.analytics.models import (
    LogicalRequestRecord,
    RequestStatus,
    UpstreamAttemptRecord,
)
from free_claude_code.core.json_types import JsonObject

_BATCH_LIMIT = 64
_QUEUE_LIMIT = 2048
# Upper bound on waiting for the worker to leave its current batch. Shutdown must not
# block behind a database that stopped answering; Uvicorn bounds HTTP draining, not the
# application lifespan, so this wait is the only limit on that path.
_WORKER_EXIT_SECONDS = 2.0


class RequestBuffer:
    """One in-flight logical request plus the attempts it has produced so far."""

    __slots__ = ("attempts", "queued", "record")

    def __init__(self, record: LogicalRequestRecord) -> None:
        self.record = record
        self.attempts: dict[str, UpstreamAttemptRecord] = {}
        self.queued = False


def blocked_priority(status: RequestStatus) -> bool:
    """Whether one status must survive queue pressure."""

    return status is RequestStatus.BLOCKED


class AnalyticsWriter:
    """Durable, non-blocking accounting writer."""

    def __init__(
        self,
        store,
        *,
        queue_limit: int = _QUEUE_LIMIT,
        batch_limit: int = _BATCH_LIMIT,
    ) -> None:
        self._store = store
        self._batch_limit = max(1, batch_limit)
        self._queue_limit = max(1, queue_limit)
        self._queue: deque[RequestBuffer] = deque()
        self._queued_ids: set[str] = set()
        self._buffers: dict[str, RequestBuffer] = {}
        self._wakeup = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped = 0
        self._written_requests = 0
        self._written_attempts = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def durable(self) -> bool:
        return self._store is not None and self._store.durable

    def start(self) -> None:
        if self._worker is not None or self._closed:
            return
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name="fcc-analytics-writer"
        )

    async def close(self) -> None:
        """Stop the worker, then flush whatever it left behind.

        The worker parks on the wake-up event as soon as the queue drains, so it has to be
        woken here: awaiting it without a wake-up waits for a submit that can never arrive.
        Both waits are bounded, because a stuck database must not be able to hang shutdown.
        """

        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None:
            self._wakeup.set()
            _, pending = await asyncio.wait({worker}, timeout=_WORKER_EXIT_SECONDS)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning(
                    "Analytics writer was still flushing after {}s; its batch is "
                    "abandoned and the remaining queue is written from shutdown.",
                    _WORKER_EXIT_SECONDS,
                )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._flush(), timeout=_WORKER_EXIT_SECONDS)

    # ---------------------------------------------------------------- intake

    def track(self, record: LogicalRequestRecord) -> RequestBuffer:
        """Return the in-flight buffer for one logical request."""

        buffer = self._buffers.get(record.request_id)
        if buffer is None:
            buffer = RequestBuffer(record)
            self._buffers[record.request_id] = buffer
        else:
            buffer.record = record
        return buffer

    def untrack(self, request_id: str) -> None:
        self._buffers.pop(request_id, None)

    def buffer(self, request_id: str) -> RequestBuffer | None:
        return self._buffers.get(request_id)

    def submit(self, buffer: RequestBuffer) -> bool:
        """Hand one request buffer to the worker without ever blocking."""

        if self._closed or buffer.queued:
            return False
        if len(self._queued_ids) >= self._queue_limit:
            # Shed the oldest unfinished write rather than waiting for the disk.
            for candidate in self._queue:
                if blocked_priority(candidate.record.status):
                    continue
                self._queue.remove(candidate)
                self._queued_ids.discard(candidate.record.request_id)
                candidate.queued = False
                self._dropped += 1
                break
            else:
                self._dropped += 1
                return False
        self._queue.append(buffer)
        self._queued_ids.add(buffer.record.request_id)
        buffer.queued = True
        self._wakeup.set()
        return True

    # ------------------------------------------------------------ background

    async def _run(self) -> None:
        while not self._closed:
            await self._wakeup.wait()
            self._wakeup.clear()
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._dropped += 1
                self._last_error = type(exc).__name__
                logger.warning(
                    "Analytics flush failed (events dropped): exc_type={}",
                    type(exc).__name__,
                )
            if self._closed:
                return
            if not self._queued_ids:
                # Nothing pending: sleep until the next submit.
                continue

    async def _flush(self) -> None:
        batch: list[RequestBuffer] = []
        while self._queue and len(batch) < self._batch_limit:
            buffer = self._queue.popleft()
            self._queued_ids.discard(buffer.record.request_id)
            buffer.queued = False
            batch.append(buffer)
        if not batch:
            return
        try:
            for buffer in batch:
                await self._store.upsert_request(buffer.record)
                await self._store.upsert_attempts(
                    buffer.record, list(buffer.attempts.values())
                )
        except Exception as exc:
            self._last_error = type(exc).__name__
            self._dropped += len(batch)
            logger.warning(
                "Analytics batch write failed (events dropped): exc_type={}",
                type(exc).__name__,
            )
            return
        self._written_requests += len(batch)
        self._written_attempts += sum(len(buffer.attempts) for buffer in batch)

    # ---------------------------------------------------------------- metrics

    @property
    def dropped_events(self) -> int:
        return self._dropped

    def stats(self) -> JsonObject:
        return {
            "durable": self.durable,
            "pending": len(self._queued_ids),
            "dropped_events": self._dropped,
            "written_requests": self._written_requests,
            "written_attempts": self._written_attempts,
            "last_error": self._last_error,
        }

    async def flush_now(self) -> None:
        """Force an immediate flush (tests and shutdown)."""

        await self._flush()


__all__ = ["AnalyticsWriter", "RequestBuffer", "blocked_priority"]
