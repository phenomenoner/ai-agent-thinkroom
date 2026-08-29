from __future__ import annotations

import asyncio
import multiprocessing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thinkroom.process_backend import ProcessIsolatedBackend
from thinkroom.schemas import BackendRequestV1, FrameInputV1


class BoundedAsyncCleanupBackend:
    name = "bounded-async-cleanup"
    model = "test-v1"

    def __init__(self, started: Any, completed: Any) -> None:
        self.started = started
        self.completed = completed

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.05)
            self.completed.set()


def request() -> BackendRequestV1:
    return BackendRequestV1(
        phase="frame",
        job_id="job",
        attempt_id="attempt",
        prompt_version="test-v1",
        input=FrameInputV1(
            question="Should bounded provider cleanup finish before hard containment?",
            domain="coding",
            guidance="Assess cleanup ownership.",
            safety="Do not modify external systems.",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="correlation",
    )


@pytest.mark.asyncio
async def test_cooperative_async_cleanup_finishes_within_process_grace() -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    context = multiprocessing.get_context("forkserver")
    started = context.Event()
    completed = context.Event()
    backend = ProcessIsolatedBackend(
        BoundedAsyncCleanupBackend(started, completed),
        shutdown_grace_seconds=0.5,
    )
    try:
        invocation = asyncio.create_task(backend.invoke(request()))
        assert await asyncio.to_thread(started.wait, 15)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(invocation, timeout=3)
        await asyncio.sleep(0)
        assert completed.is_set()
        assert backend.active_process_count == 0
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)
