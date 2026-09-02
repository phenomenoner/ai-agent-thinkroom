from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thinkroom.backends import FailoverBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.ports import BackendError
from thinkroom.process_backend import ProcessIsolatedBackend
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import BackendRequestV1, FrameInputV1, JobState, ResearchRequest
from thinkroom.service import ThinkroomService


class StaticBackend:
    def __init__(
        self,
        name: str,
        model: str,
        *,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.result = result or {"ok": True}
        self.error = error
        self.calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class RejectingAudit:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    def start(self, request: BackendRequestV1, backend: str, model: str) -> int:
        self.started.append((backend, model))
        return len(self.started)

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
    ) -> bool:
        return False


class CapturingAudit:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def start(self, request: BackendRequestV1, backend: str, model: str) -> int:
        return 1

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
    ) -> bool:
        self.statuses.append(output_status)
        return True


class BlockingBackend:
    name = "prime:openrouter"
    model = "z-ai/glm-5.3-flash"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


def frame_request() -> BackendRequestV1:
    return BackendRequestV1(
        phase="frame",
        job_id="job-1",
        attempt_id="attempt-1",
        prompt_version="test-v1",
        input=FrameInputV1(
            question="Should this important runtime route fail over?",
            context="",
            domain="coding",
            strategy="orthogonal",
            guidance="Compare correctness, operability, and testability.",
            safety="Advisory only; do not perform external effects.",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        correlation_id="correlation-1",
    )


@pytest.mark.asyncio
async def test_failover_backend_keeps_primary_result_and_audit_identity() -> None:
    primary = StaticBackend("prime:openrouter", "z-ai/glm-5.3-flash")
    fallback = StaticBackend("prime:openai-codex", "gpt-5.6-terra")
    backend = FailoverBackend(primary, fallback)
    request = frame_request()

    assert await backend.invoke(request) == {"ok": True}
    identity = backend.take_invocation_identity(request)

    assert (primary.calls, fallback.calls) == (1, 0)
    assert identity.backend == "prime:openrouter"
    assert identity.model == "z-ai/glm-5.3-flash"
    assert not identity.used_fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["PROVIDER_ERROR", "BACKEND_TIMEOUT"])
async def test_failover_backend_uses_fallback_only_for_provider_availability(code: str) -> None:
    primary = StaticBackend(
        "prime:openrouter",
        "z-ai/glm-5.3-flash",
        error=BackendError(code, "primary unavailable"),
    )
    fallback = StaticBackend("prime:openai-codex", "gpt-5.6-terra", result={"route": "fallback"})
    backend = FailoverBackend(primary, fallback)
    request = frame_request()

    assert await backend.invoke(request) == {"route": "fallback"}
    identity = backend.take_invocation_identity(request)

    expected_primary_calls = 2 if code == "PROVIDER_ERROR" else 1
    assert (primary.calls, fallback.calls) == (expected_primary_calls, 1)
    assert identity.backend == "prime:openai-codex"
    assert identity.model == "gpt-5.6-terra"
    assert identity.used_fallback
    assert identity.primary_error_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "MALFORMED_PROVIDER_OUTPUT",
        "OUTPUT_LIMIT_EXCEEDED",
        "CONTEXT_LIMIT_EXCEEDED",
        "INVALID_REQUEST",
        "UNSUPPORTED_PHASE",
        "DEADLINE_EXCEEDED",
    ],
)
async def test_failover_backend_does_not_cross_provider_for_nonavailability_errors(
    code: str,
) -> None:
    primary = StaticBackend("prime:openrouter", "glm", error=BackendError(code, "not eligible"))
    fallback = StaticBackend("prime:openai-codex", "terra")
    backend = FailoverBackend(primary, fallback)
    request = frame_request()

    with pytest.raises(BackendError) as raised:
        await backend.invoke(request)

    identity = backend.take_invocation_identity(request)
    assert raised.value.code == code
    assert (primary.calls, fallback.calls) == (1, 0)
    assert identity.backend == "prime:openrouter"
    assert not identity.used_fallback


@pytest.mark.asyncio
async def test_failover_audit_preserves_semantic_output_limit_without_crossing_provider() -> None:
    primary = StaticBackend(
        "prime:openrouter",
        "glm",
        error=BackendError(
            "OUTPUT_LIMIT_EXCEEDED",
            "final text exceeded byte limit",
            audit_status="OUTPUT_LIMIT_FINAL_TEXT",
        ),
    )
    fallback = StaticBackend("prime:openai-codex", "terra")
    backend = FailoverBackend(primary, fallback)
    audit = CapturingAudit()

    with pytest.raises(BackendError) as raised:
        await backend.invoke_with_audit(frame_request(), audit)

    assert raised.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert raised.value.audit_status == "OUTPUT_LIMIT_FINAL_TEXT"
    assert audit.statuses == ["OUTPUT_LIMIT_FINAL_TEXT"]
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.asyncio
async def test_failover_backend_never_turns_cancellation_into_fallback() -> None:
    primary = StaticBackend("prime:openrouter", "glm", error=asyncio.CancelledError())
    fallback = StaticBackend("prime:openai-codex", "terra")
    backend = FailoverBackend(primary, fallback)
    request = frame_request()

    with pytest.raises(asyncio.CancelledError):
        await backend.invoke(request)

    identity = backend.take_invocation_identity(request)
    assert (primary.calls, fallback.calls) == (1, 0)
    assert identity.backend == "prime:openrouter"
    assert not identity.used_fallback


@pytest.mark.asyncio
async def test_failover_backend_does_not_fallback_after_losing_attempt_ownership() -> None:
    primary = StaticBackend(
        "prime:openrouter", "glm", error=BackendError("PROVIDER_ERROR", "primary failed")
    )
    fallback = StaticBackend("prime:openai-codex", "terra")
    backend = FailoverBackend(primary, fallback)
    audit = RejectingAudit()

    with pytest.raises(BackendError) as raised:
        await backend.invoke_with_audit(frame_request(), audit)

    assert raised.value.code == "STALE_ATTEMPT"
    assert audit.started == [("prime:openrouter", "glm")]
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.asyncio
async def test_failover_backend_preserves_fallback_failure_and_chain_identity() -> None:
    primary = StaticBackend(
        "prime:openrouter", "glm", error=BackendError("PROVIDER_ERROR", "primary failed")
    )
    fallback = StaticBackend(
        "prime:openai-codex", "terra", error=BackendError("PROVIDER_ERROR", "fallback failed")
    )
    backend = FailoverBackend(primary, fallback)
    request = frame_request()

    with pytest.raises(BackendError, match="fallback failed"):
        await backend.invoke(request)

    identity = backend.take_invocation_identity(request)
    assert (primary.calls, fallback.calls) == (2, 1)
    assert identity.backend == "prime:openai-codex"
    assert identity.model == "terra"
    assert identity.used_fallback


class FrameUnavailableBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(name="prime:openrouter", model="z-ai/glm-5.3-flash")

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        if request.phase == "frame":
            raise BackendError("PROVIDER_ERROR", "primary unavailable")
        return await super().invoke(request)


class TerraBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(name="prime:openai-codex", model="gpt-5.6-terra")


@pytest.mark.asyncio
async def test_engine_persists_ordered_physical_fallback_calls_without_a_schema_change(
    tmp_path,
) -> None:
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = FailoverBackend(FrameUnavailableBackend(), TerraBackend())
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    try:
        job_id, _ = await engine.submit(
            ResearchRequest(
                question="Should the durable provider audit identify the fallback route?",
                branch_count=2,
            )
        )
        row = None
        for _ in range(300):
            row = repo.get_job(job_id)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "succeeded"
        calls = (
            repo._db()
            .execute(
                "SELECT phase,backend,model,output_status FROM provider_calls "
                "WHERE job_id=? ORDER BY id",
                (job_id,),
            )
            .fetchall()
        )
        frame_calls = [call for call in calls if call["phase"] == "frame"]
        assert [call["backend"] for call in frame_calls] == [
            "prime:openrouter",
            "prime:openrouter",
            "prime:openai-codex",
        ]
        assert [call["model"] for call in frame_calls] == [
            "z-ai/glm-5.3-flash",
            "z-ai/glm-5.3-flash",
            "gpt-5.6-terra",
        ]
        assert [call["output_status"] for call in frame_calls] == [
            "PROVIDER_ERROR",
            "PROVIDER_ERROR",
            "validated",
        ]
        assert all("->" not in call["backend"] for call in calls)
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_engine_cancellation_closes_started_call_without_invoking_fallback(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    primary = BlockingBackend()
    fallback = StaticBackend("prime:openai-codex", "gpt-5.6-terra")
    engine = ResearchEngine(
        repo,
        FailoverBackend(primary, fallback),
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job_id, _ = await engine.submit(
            ResearchRequest(
                question="Must cancellation close physical audit before terminal state?"
            )
        )
        await asyncio.wait_for(primary.started.wait(), timeout=2)
        assert await engine.cancel(job_id)
        row = None
        for _ in range(200):
            row = repo.get_job(job_id)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "cancelled"
        calls = (
            repo._db()
            .execute(
                "SELECT backend,model,output_status,ended_at FROM provider_calls "
                "WHERE job_id=? ORDER BY id",
                (job_id,),
            )
            .fetchall()
        )
        assert len(calls) == 1
        assert calls[0]["backend"] == "prime:openrouter"
        assert calls[0]["model"] == "z-ai/glm-5.3-flash"
        assert calls[0]["output_status"] == "cancelled"
        assert calls[0]["ended_at"] is not None
        assert fallback.calls == 0
    finally:
        await engine.stop()
        repo.close()


def test_failover_configuration_reserves_time_for_the_fallback() -> None:
    settings = Settings(
        backend="prime_agent_failover",
        job_soft_timeout_seconds=900,
        job_timeout_seconds=1800,
        backend_timeout_seconds=600,
        failover_primary_timeout_seconds=300,
    )
    settings.validate()
    with pytest.raises(ValueError, match="reserve time"):
        Settings(
            backend="prime_agent_failover",
            job_soft_timeout_seconds=900,
            job_timeout_seconds=1800,
            backend_timeout_seconds=600,
            failover_primary_timeout_seconds=600,
        ).validate()


def test_service_builds_two_explicit_process_isolated_prime_routes(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "prime-agent"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_EXECUTABLE", str(executable))
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_THINKING", "high")
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_FALLBACK_PROVIDER", "openai-codex")
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_FALLBACK_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_FALLBACK_THINKING", "high")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        backend="prime_agent_failover",
        backend_timeout_seconds=600,
        failover_primary_timeout_seconds=300,
    )

    backend = ThinkroomService(settings)._selected_backend()

    assert isinstance(backend, FailoverBackend)
    assert isinstance(backend.primary, ProcessIsolatedBackend)
    assert isinstance(backend.fallback, ProcessIsolatedBackend)
    assert backend.name == "prime_agent:openrouter->prime_agent:openai-codex"
    assert backend.model == "z-ai/glm-5.3-flash->gpt-5.6-terra"


def test_startup_recovery_fails_interrupted_failover_attempt_closed(tmp_path) -> None:
    path = tmp_path / "db.sqlite"
    repo = SQLiteRepository(str(path))
    repo.open()
    request = ResearchRequest(question="Must an interrupted physical provider call be replayed?")
    job_id, _ = repo.create_job(
        request,
        "interrupted-failover",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    claimed = repo.claim_next_job(
        2,
        "worker-1",
        "prime:openrouter->prime:openai-codex",
        "z-ai/glm-5.3-flash->gpt-5.6-terra",
    )
    assert claimed and claimed[0] == job_id
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "phase": "frame",
            "branch_id": None,
            "prompt_version": "test-v1",
            "backend": "prime:openrouter",
            "model": "z-ai/glm-5.3-flash",
            "started_at": datetime.now(UTC).isoformat(),
            "retry_index": 0,
            "output_status": "started",
            "output_size": 0,
        }
    )

    repo.recover_startup(2)

    row = repo.get_job(job_id)
    assert row and row["state"] == "failed"
    assert json.loads(row["terminal_error"])["code"] == "PROVIDER_ATTEMPT_INTERRUPTED"
    attempt = repo.attempts(job_id)[0]
    assert attempt["state"] == "abandoned"
    assert attempt["outcome"] == "provider_attempt_interrupted"
    call = repo._db().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert call["output_status"] == "uncertain"
    assert call["ended_at"] is not None
    assert repo.claim_next_job(2, "worker-2") is None
    repo.close()


def test_requeued_failover_job_rejects_provider_policy_drift(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="May a recovered retry silently change provider policy?")
    job_id, _ = repo.create_job(
        request,
        "route-drift",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    claimed = repo.claim_next_job(
        2,
        "worker-1",
        "prime:openrouter->prime:openai-codex",
        "z-ai/glm-5.3-flash->gpt-5.6-terra",
    )
    assert claimed and claimed[0] == job_id
    repo.recover_startup(2)
    assert repo.get_job(job_id)["state"] == "queued"

    assert (
        repo.claim_next_job(
            2,
            "worker-2",
            "prime:openrouter->prime:openai-codex",
            "z-ai/glm-5.3-flash->gpt-5.6-terra-pro",
        )
        is None
    )
    row = repo.get_job(job_id)
    assert row and row["state"] == "failed"
    assert json.loads(row["terminal_error"])["code"] == "PROVIDER_POLICY_DRIFT"
    assert len(repo.attempts(job_id)) == 1
    repo.close()


def test_startup_reconciles_open_call_for_already_cancelled_job(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Must startup close a legacy cancelled audit row?")
    job_id, _ = repo.create_job(
        request,
        "cancelled-open-call",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    claimed = repo.claim_next_job(2, "worker", "prime:openrouter", "glm")
    assert claimed and claimed[0] == job_id
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "phase": "frame",
            "started_at": datetime.now(UTC).isoformat(),
            "output_status": "started",
            "output_size": 0,
        }
    )
    assert repo.request_cancel(job_id)
    assert repo.settle_terminal(
        job_id,
        JobState.CANCELLED,
        attempt_id,
        "cancelled",
        "test cancellation",
        "test",
        "CANCELLED",
        "job cancelled",
    )

    repo.recover_startup(2)

    row = repo._db().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert row["output_status"] == "cancelled"
    assert row["ended_at"] is not None
    repo.close()
