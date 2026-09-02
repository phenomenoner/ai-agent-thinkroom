from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from thinkroom.api import create_app
from thinkroom.backends import PrimeAgentBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.ports import BackendError, BackendTransportMetrics
from thinkroom.process_backend import ProcessIsolatedBackend
from thinkroom.schemas import BackendRequestV1, FrameInputV1, ResearchRequest
from thinkroom.service import ThinkroomService


class RepeatedCancellationSuppressor:
    name = "repeated-cancellation-suppressor"
    model = "test-v1"

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue


class ReturningGrandchildBackend:
    name = "returning-grandchild"
    model = "test-v1"

    def __init__(self, pid_path: str) -> None:
        self.pid_path = pid_path

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            start_new_session=False,
        )
        Path(self.pid_path).write_text(str(process.pid), encoding="ascii")
        return {"status": "returned"}


class OutputLimitBackend:
    name = "output-limit"
    model = "test-v1"

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        raise BackendError(
            "OUTPUT_LIMIT_EXCEEDED",
            "provider response exceeded byte limit",
            audit_status="OUTPUT_LIMIT_ACCOUNTED_TRANSPORT",
            transport_metrics=BackendTransportMetrics(
                raw_transport_bytes=1234,
                accounted_transport_bytes=987,
                event_count=9,
                max_event_bytes=456,
                message_update_count=7,
                message_snapshot_bytes=890,
                message_partial_bytes=765,
                message_delta_bytes=321,
            ),
        )


class PausingCleanupBackend(ProcessIsolatedBackend):
    def __init__(self) -> None:
        super().__init__(ScriptedBackend(), context_name="forkserver")
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()

    async def _stop_process(self, process, control) -> None:
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        await super()._stop_process(process, control)


class SuppressingService(ThinkroomService):
    def _selected_backend(self) -> RepeatedCancellationSuppressor:
        return RepeatedCancellationSuppressor()


def frame_request() -> BackendRequestV1:
    return BackendRequestV1(
        phase="frame",
        job_id="job",
        attempt_id="attempt",
        prompt_version="test-v1",
        input=FrameInputV1(
            question="Should provider execution use a killable process boundary?",
            domain="coding",
            guidance="Assess lifecycle containment.",
            safety="Do not modify external systems.",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="correlation",
    )


@pytest.mark.asyncio
async def test_process_isolated_backend_returns_valid_result() -> None:
    backend = ProcessIsolatedBackend(ScriptedBackend(), shutdown_grace_seconds=0.2)
    result = await backend.invoke(frame_request())
    assert result["decision"] == frame_request().input.question
    assert backend.active_process_count == 0


@pytest.mark.asyncio
async def test_process_isolated_backend_preserves_safe_output_limit_audit_status() -> None:
    backend = ProcessIsolatedBackend(OutputLimitBackend(), shutdown_grace_seconds=0.2)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(frame_request())

    assert caught.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert caught.value.audit_status == "OUTPUT_LIMIT_ACCOUNTED_TRANSPORT"
    assert caught.value.transport_metrics == BackendTransportMetrics(
        raw_transport_bytes=1234,
        accounted_transport_bytes=987,
        event_count=9,
        max_event_bytes=456,
        message_update_count=7,
        message_snapshot_bytes=890,
        message_partial_bytes=765,
        message_delta_bytes=321,
    )
    assert backend.active_process_count == 0


@pytest.mark.asyncio
async def test_process_isolated_backend_hard_kills_repeated_cancellation_suppressor() -> None:
    backend = ProcessIsolatedBackend(
        RepeatedCancellationSuppressor(),
        shutdown_grace_seconds=0.2,
    )

    invocation = asyncio.create_task(backend.invoke(frame_request()))
    for _ in range(100):
        if backend.active_process_count == 1:
            break
        await asyncio.sleep(0.01)
    assert backend.active_process_count == 1
    invocation.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
        invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invocation, timeout=2)

    assert backend.active_process_count == 0
    assert not any(process.is_alive() for process in multiprocessing.active_children())


@pytest.mark.asyncio
async def test_normal_provider_return_reaps_its_process_group(tmp_path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    backend = ProcessIsolatedBackend(
        ReturningGrandchildBackend(str(pid_path)), context_name="forkserver"
    )
    grandchild_pid = 0
    try:
        result = await asyncio.wait_for(backend.invoke(frame_request()), timeout=15)
        assert result == {"status": "returned"}
        grandchild_pid = int(pid_path.read_text(encoding="ascii"))
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)
        assert backend.active_process_count == 0
    finally:
        if grandchild_pid > 0:
            try:
                os.kill(grandchild_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_prime_result_settles_when_descendant_keeps_stderr_open(tmp_path) -> None:
    descendant_pid_path = tmp_path / "prime-descendant.pid"
    executable = tmp_path / "prime-with-stderr-descendant"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, subprocess, sys\n"
        f"pid_path = pathlib.Path({str(descendant_pid_path)!r})\n"
        "descendant = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(3600)'], stdout=subprocess.DEVNULL)\n"
        "pid_path.write_text(str(descendant.pid))\n"
        "command = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id': command.get('id'), 'type': 'response', 'command': 'prompt', "
        "'success': True}), flush=True)\n"
        "print(json.dumps({'type': 'message_end', 'message': {'role': 'custom', "
        "'customType': 'agent_message', 'details': {'message': 'child done', "
        "'fromRelationship': 'child', 'from': {'sessionName': 'thinkroom-frame-worker'}}}}), "
        "flush=True)\n"
        "cleanup = command['message'].split('```python\\n', 1)[1].split('\\n```', 1)[0]\n"
        "print(json.dumps({'type': 'tool_execution_start', 'toolName': 'ipython', "
        "'toolCallId': 'cleanup-1', 'args': {'code': cleanup}}), flush=True)\n"
        "print(json.dumps({'type': 'tool_execution_end', 'toolName': 'ipython', "
        "'toolCallId': 'cleanup-1', 'isError': False, "
        "'result': 'THINKROOM_CHILD_CLEANED:thinkroom-frame-worker'}), flush=True)\n"
        "result = json.dumps({'schema_version':1,'decision':'d','scope':'s','constraints':['c'],"
        "'success_criteria':['s'],'ambiguities':['a'],'research_questions':['q']})\n"
        "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', "
        "'content': [{'type': 'text', 'text': result}], 'stopReason': 'stop'}}), flush=True)\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'custom', 'customType': 'agent_message', 'content': 'child done', "
        "'details': {'message': 'child done', 'fromRelationship': 'child', "
        "'from': {'sessionName': 'thinkroom-frame-worker'}}},"
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': result}], "
        "'stopReason': 'stop'}]}), flush=True)\n"
        "sys.stdin.read()\n"
    )
    executable.chmod(0o755)
    backend = ProcessIsolatedBackend(
        PrimeAgentBackend(str(executable), "", "", "off", timeout=3),
        shutdown_grace_seconds=0.2,
        context_name="forkserver",
    )
    descendant_pid = 0
    try:
        result = await asyncio.wait_for(backend.invoke(frame_request()), timeout=8)
        assert result["decision"] == "d"
        descendant_pid = int(descendant_pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
        assert backend.active_process_count == 0
    finally:
        if descendant_pid > 0:
            try:
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_caller_cancellation_during_tree_cleanup_is_preserved() -> None:
    backend = PausingCleanupBackend()
    invocation = asyncio.create_task(backend.invoke(frame_request()))
    await asyncio.wait_for(backend.cleanup_started.wait(), timeout=15)

    invocation.cancel()
    backend.allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invocation, timeout=5)
    assert backend.active_process_count == 0


@pytest.mark.asyncio
async def test_service_readiness_and_lease_follow_process_containment(tmp_path) -> None:
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'service.sqlite'}",
        max_concurrency=1,
        backend_timeout_seconds=10,
    )
    service = SuppressingService(settings)
    await service.start()
    try:
        assert service.engine is not None
        engine = service.engine
        job_id, _ = await engine.submit(
            ResearchRequest(question="Should cancellation preserve physical capacity bounds?")
        )
        assert isinstance(engine.backend, ProcessIsolatedBackend)
        for _ in range(300):
            if engine.backend.active_process_count == 1:
                break
            await asyncio.sleep(0.01)
        assert engine.backend.active_process_count == 1
        await engine.cancel(job_id)
        for _ in range(300):
            if engine._detached_provider_tasks:
                break
            await asyncio.sleep(0.01)
        assert len(engine._detached_provider_tasks) == 1
        assert engine._semaphore._value == 0

        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 503

        for _ in range(300):
            if not engine._detached_provider_tasks:
                break
            await asyncio.sleep(0.01)
        assert not engine._detached_provider_tasks
        assert engine._semaphore._value == 1
        assert service.ready
    finally:
        await service.stop()
    assert service.lock.handle is None
    assert service.repo.db is None


@pytest.mark.asyncio
async def test_immediate_service_stop_does_not_orphan_detached_provider(tmp_path) -> None:
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'immediate-stop.sqlite'}",
        max_concurrency=1,
        backend_timeout_seconds=10,
    )
    service = SuppressingService(settings)
    await service.start()
    assert service.engine is not None
    engine = service.engine
    assert isinstance(engine.backend, ProcessIsolatedBackend)
    job_id, _ = await engine.submit(
        ResearchRequest(question="Should immediate shutdown reap a detached provider child?")
    )
    for _ in range(300):
        if engine.backend.active_process_count == 1:
            break
        await asyncio.sleep(0.01)
    assert engine.backend.active_process_count == 1
    provider_pid = next(iter(engine.backend._processes)).pid
    assert provider_pid is not None
    assert await engine.cancel(job_id)
    for _ in range(300):
        if engine._detached_provider_tasks:
            break
        await asyncio.sleep(0.01)
    assert engine._detached_provider_tasks

    await asyncio.wait_for(service.stop(), timeout=3)

    active_pids = {process.pid for process in multiprocessing.active_children()}
    assert provider_pid not in active_pids
    assert not engine._detached_provider_tasks
    assert service.repo.db is None
    assert service.lock.handle is None
