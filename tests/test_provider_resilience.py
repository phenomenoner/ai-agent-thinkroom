from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thinkroom.backends import FailoverBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.ports import BackendError
from thinkroom.progress import derive_research_progress
from thinkroom.repository import _SCHEMA_SQL_V024, SQLiteRepository
from thinkroom.schemas import (
    BackendRequestV1,
    FrameInputV1,
    ProgressClassification,
    ProgressSubstate,
    ResearchRequest,
)


class SequenceBackend:
    def __init__(self, name: str, model: str, outcomes: list[object]) -> None:
        self.name = name
        self.model = model
        self._outcomes: Iterator[object] = iter(outcomes)
        self.calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome


class PersistentAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def history(self, request: BackendRequestV1) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if row["attempt_id"] == request.attempt_id
            and row["phase"] == request.phase
            and row["branch_id"] == request.branch_id
        ]

    def attempt_history(self, request: BackendRequestV1) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["attempt_id"] == request.attempt_id]

    def start(
        self,
        request: BackendRequestV1,
        backend: str,
        model: str,
        *,
        route_role: str,
        effective_timeout_seconds: float,
    ) -> int:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "attempt_id": request.attempt_id,
                "phase": request.phase,
                "branch_id": request.branch_id,
                "backend": backend,
                "model": model,
                "route_role": route_role,
                "effective_timeout_seconds": effective_timeout_seconds,
                "started_at": datetime.now(UTC).isoformat(),
                "ended_at": None,
                "output_status": "started",
                "error_code": None,
            }
        )
        return len(self.rows)

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
        *,
        error_code: str | None = None,
    ) -> bool:
        row = self.rows[call_id - 1]
        row.update(
            ended_at=datetime.now(UTC).isoformat(),
            output_status=output_status,
            error_code=error_code,
            output_size=output_size,
        )
        return True


class DurationAudit(PersistentAudit):
    def __init__(self, durations: list[float]) -> None:
        super().__init__()
        self._durations: Iterator[float] = iter(durations)

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
        *,
        error_code: str | None = None,
    ) -> bool:
        admitted = super().finish(
            call_id,
            request,
            output_status,
            output_size,
            error_code=error_code,
        )
        row = self.rows[call_id - 1]
        ended = datetime.fromisoformat(row["ended_at"])
        row["started_at"] = (ended - timedelta(seconds=next(self._durations))).isoformat()
        return admitted


def request(*, phase: str = "frame", branch_id: str | None = None) -> BackendRequestV1:
    assert phase == "frame"
    return BackendRequestV1(
        phase="frame",
        job_id="job-1",
        attempt_id="attempt-1",
        branch_id=branch_id,
        prompt_version="test-v1",
        input=FrameInputV1(
            question="How should bounded provider resilience behave?",
            context="",
            domain="coding",
            strategy="orthogonal",
            guidance="Review the explicit behavior.",
            safety="Advisory only.",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        correlation_id="correlation-1",
    )


@pytest.mark.asyncio
async def test_timeout_falls_back_without_retry_and_opens_attempt_circuit() -> None:
    primary = SequenceBackend("primary", "p", [BackendError("BACKEND_TIMEOUT", "slow")])
    fallback = SequenceBackend("fallback", "f", [{"route": "fallback-1"}, {"route": "fallback-2"}])
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=90,
        fallback_timeout_seconds=180,
        retry_delay_seconds=(0, 0),
    )
    audit = PersistentAudit()

    assert await backend.invoke_with_audit(request(), audit) == {"route": "fallback-1"}
    next_request = request(branch_id="next")
    assert await backend.invoke_with_audit(next_request, audit) == {"route": "fallback-2"}

    assert primary.calls == 1
    assert fallback.calls == 2
    assert [row["route_role"] for row in audit.rows] == ["primary", "fallback", "fallback"]


@pytest.mark.asyncio
async def test_fast_transient_retries_once_then_falls_back_with_three_call_cap() -> None:
    primary = SequenceBackend(
        "primary",
        "p",
        [BackendError("PROVIDER_ERROR", "reset"), BackendError("PROVIDER_ERROR", "reset")],
    )
    fallback = SequenceBackend("fallback", "f", [{"route": "fallback"}])
    audit = PersistentAudit()
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=90,
        fallback_timeout_seconds=180,
        retry_delay_seconds=(0, 0),
    )

    assert await backend.invoke_with_audit(request(), audit) == {"route": "fallback"}
    assert (primary.calls, fallback.calls, len(audit.rows)) == (2, 1, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duration", "expected_primary_calls"),
    [(29.999, 2), (30.0, 2), (30.001, 1)],
)
async def test_provider_error_retries_only_within_fast_transient_boundary(
    duration: float, expected_primary_calls: int
) -> None:
    primary = SequenceBackend(
        "primary",
        "p",
        [BackendError("PROVIDER_ERROR", "reset"), BackendError("PROVIDER_ERROR", "reset")],
    )
    fallback = SequenceBackend("fallback", "f", [{"route": "fallback"}])
    audit = DurationAudit([duration, 0.0])
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=90,
        fallback_timeout_seconds=180,
        retry_delay_seconds=(0, 0),
        fast_transient_seconds=30,
    )

    assert await backend.invoke_with_audit(request(), audit) == {"route": "fallback"}
    assert primary.calls == expected_primary_calls
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_spent_three_call_budget_does_not_offer_schema_repair() -> None:
    primary = SequenceBackend(
        "primary",
        "p",
        [BackendError("PROVIDER_ERROR", "reset"), BackendError("PROVIDER_ERROR", "reset")],
    )
    fallback = SequenceBackend("fallback", "f", [{"invalid": True}])
    audit = PersistentAudit()
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=90,
        fallback_timeout_seconds=180,
        retry_delay_seconds=(0, 0),
    )
    call = request()

    assert await backend.invoke_with_audit(call, audit) == {"invalid": True}
    audit.finish(3, call, "invalid", error_code="MALFORMED_PROVIDER_OUTPUT")
    with pytest.raises(BackendError, match="physical call budget") as raised:
        await backend.invoke_with_audit(call, audit)

    assert raised.value.code == "CALL_BUDGET_EXHAUSTED"
    assert (primary.calls, fallback.calls) == (2, 1)


@pytest.mark.asyncio
async def test_fallback_schema_repair_stays_on_fallback_and_is_third_call() -> None:
    primary = SequenceBackend("primary", "p", [BackendError("BACKEND_TIMEOUT", "slow")])
    fallback = SequenceBackend("fallback", "f", [{"invalid": True}, {"valid": True}])
    audit = PersistentAudit()
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=90,
        fallback_timeout_seconds=180,
        retry_delay_seconds=(0, 0),
    )
    call = request()

    assert await backend.invoke_with_audit(call, audit) == {"invalid": True}
    audit.finish(2, call, "invalid", error_code="MALFORMED_PROVIDER_OUTPUT")
    assert await backend.invoke_with_audit(call, audit) == {"valid": True}

    assert [row["route_role"] for row in audit.rows] == ["primary", "fallback", "fallback"]


@pytest.mark.asyncio
async def test_output_limit_never_retries_or_falls_back() -> None:
    primary = SequenceBackend("primary", "p", [BackendError("OUTPUT_LIMIT_EXCEEDED", "too large")])
    fallback = SequenceBackend("fallback", "f", [{"unexpected": True}])
    audit = PersistentAudit()
    backend = FailoverBackend(primary, fallback, retry_delay_seconds=(0, 0))

    with pytest.raises(BackendError) as raised:
        await backend.invoke_with_audit(request(), audit)

    assert raised.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.asyncio
async def test_rate_limit_retries_but_does_not_open_primary_circuit() -> None:
    primary = SequenceBackend(
        "primary",
        "p",
        [
            BackendError("RATE_LIMITED", "429"),
            BackendError("RATE_LIMITED", "429"),
            {"route": "primary-next"},
        ],
    )
    fallback = SequenceBackend("fallback", "f", [{"route": "fallback"}])
    audit = PersistentAudit()
    backend = FailoverBackend(primary, fallback, retry_delay_seconds=(0, 0))

    assert await backend.invoke_with_audit(request(), audit) == {"route": "fallback"}
    assert await backend.invoke_with_audit(request(branch_id="next"), audit) == {
        "route": "primary-next"
    }
    assert (primary.calls, fallback.calls) == (3, 1)


@pytest.mark.asyncio
async def test_unclassified_primary_error_falls_back_once_without_retry() -> None:
    primary = SequenceBackend("primary", "p", [BackendError("NEW_PROVIDER_CODE", "new")])
    fallback = SequenceBackend("fallback", "f", [{"route": "fallback"}])
    audit = PersistentAudit()
    backend = FailoverBackend(primary, fallback, retry_delay_seconds=(0, 0))

    assert await backend.invoke_with_audit(request(), audit) == {"route": "fallback"}
    assert (primary.calls, fallback.calls) == (1, 1)
    assert audit.rows[0]["error_code"] == "UNCLASSIFIED_ERROR"


def test_v025_deadline_and_concurrency_defaults_are_bounded() -> None:
    settings = Settings()

    assert settings.max_concurrency == 1
    assert settings.job_soft_timeout_seconds == 900
    assert settings.job_timeout_seconds == 1200
    assert settings.rollout_provider_concurrency == 1
    assert settings.rollout_provider_concurrency <= 2


def test_soft_deadline_must_leave_one_fallback_timeout_of_reserve() -> None:
    with pytest.raises(ValueError, match="soft timeout must reserve"):
        Settings(
            job_soft_timeout_seconds=1100,
            job_timeout_seconds=1200,
            backend_timeout_seconds=180,
        ).validate()


def test_v024_database_is_migrated_to_recovery_correct_provider_evidence(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA_SQL_V024)
    db.close()

    repo = SQLiteRepository(str(path))
    repo.open()
    try:
        columns = {row["name"] for row in repo._db().execute("PRAGMA table_info(provider_calls)")}
        assert {
            "route_role",
            "effective_timeout_seconds",
            "error_code",
        } <= columns
    finally:
        repo.close()


def _progress_job(now: datetime, state: str = "rolling_out") -> dict[str, Any]:
    return {"state": state, "updated_at": (now - timedelta(seconds=10)).isoformat()}


def test_progress_marks_expired_unfinished_call_presumed_dead() -> None:
    observed = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    call = {
        "id": 7,
        "phase": "rollout",
        "branch_id": "branch-a",
        "route_role": "primary",
        "backend": "primary",
        "model": "p",
        "started_at": (observed - timedelta(seconds=100)).isoformat(),
        "ended_at": None,
        "effective_timeout_seconds": 90,
        "error_code": None,
    }

    progress = derive_research_progress(
        _progress_job(observed),
        branch_count=3,
        provider_calls=[call],
        artifacts=[],
        transitions=[],
        observed_at=observed,
    )

    assert progress.classification is ProgressClassification.PRESUMED_DEAD
    assert progress.substate is ProgressSubstate.PRESUMED_DEAD
    assert progress.evidence_watermark == 7
    assert progress.active == 0


def test_progress_distinguishes_live_slow_fallback_from_stall() -> None:
    observed = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    call = {
        "id": 8,
        "phase": "rollout",
        "branch_id": "branch-a",
        "route_role": "fallback",
        "backend": "fallback",
        "model": "f",
        "started_at": (observed - timedelta(seconds=130)).isoformat(),
        "ended_at": None,
        "effective_timeout_seconds": 180,
        "error_code": None,
    }

    progress = derive_research_progress(
        _progress_job(observed),
        branch_count=3,
        provider_calls=[call],
        artifacts=[],
        transitions=[],
        observed_at=observed,
        slow_warning_seconds=120,
    )

    assert progress.classification is ProgressClassification.DEGRADED
    assert progress.substate is ProgressSubstate.FALLBACK_ACTIVE
    assert progress.active == 1
    assert progress.phases[0].timeout_remaining_seconds == pytest.approx(50)


def test_terminal_progress_has_no_queued_work() -> None:
    observed = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    progress = derive_research_progress(
        _progress_job(observed, "succeeded"),
        branch_count=3,
        provider_calls=[],
        artifacts=[],
        transitions=[],
        observed_at=observed,
    )

    assert progress.classification is ProgressClassification.TERMINAL
    assert progress.queued == 0


class SlowFirstRolloutBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.rollout_calls = 0

    async def invoke(self, call: BackendRequestV1) -> dict[str, Any]:
        if call.phase == "rollout":
            self.rollout_calls += 1
            if self.rollout_calls == 1:
                await asyncio.sleep(1.1)
        return await super().invoke(call)


@pytest.mark.asyncio
async def test_soft_deadline_preserves_partial_artifact_and_skips_queued_rollout(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "partial.sqlite"))
    repo.open()
    backend = SlowFirstRolloutBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'partial.sqlite'}",
            max_concurrency=1,
            rollout_provider_concurrency=1,
            job_soft_timeout_seconds=1,
            job_timeout_seconds=10,
            backend_timeout_seconds=5,
        ),
    )
    try:
        job_id, _ = await engine.submit(
            ResearchRequest(
                question="Should queued rollout work stop at the soft deadline?",
                branch_count=2,
            )
        )
        for _ in range(400):
            row = repo.get_job(job_id)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "succeeded"
        partial = [row for row in repo.get_artifacts(job_id) if row["kind"] == "partial"]
        assert len(partial) == 1
        payload = json.loads(partial[0]["payload"])
        assert payload["reason"] == "SOFT_DEADLINE_REACHED"
        assert payload["skipped_branch_ids"]
        assert backend.rollout_calls == 1
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_expired_soft_deadline_settles_partial_without_starting_frame(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "expired-soft.sqlite"))
    repo.open()
    backend = SlowFirstRolloutBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'expired-soft.sqlite'}",
            max_concurrency=1,
            job_soft_timeout_seconds=-1,
            job_timeout_seconds=10,
            backend_timeout_seconds=5,
        ),
    )
    try:
        job_id, _ = await engine.submit(
            ResearchRequest(question="Should expired admission preserve a partial receipt?")
        )
        for _ in range(200):
            row = repo.get_job(job_id)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "succeeded"
        partial = [row for row in repo.get_artifacts(job_id) if row["kind"] == "partial"]
        assert len(partial) == 1
        assert repo.provider_calls(job_id) == []
    finally:
        await engine.stop()
        repo.close()


class MeasuredConcurrencyBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.active_rollouts = 0
        self.max_active_rollouts = 0
        self.two_started = asyncio.Event()

    async def invoke(self, call: BackendRequestV1) -> dict[str, Any]:
        if call.phase != "rollout":
            return await super().invoke(call)
        self.active_rollouts += 1
        self.max_active_rollouts = max(self.max_active_rollouts, self.active_rollouts)
        if self.active_rollouts == 2:
            self.two_started.set()
        try:
            await asyncio.wait_for(self.two_started.wait(), timeout=1)
            return await super().invoke(call)
        finally:
            self.active_rollouts -= 1


@pytest.mark.asyncio
async def test_rollout_canary_allows_two_physical_calls_but_not_three(tmp_path) -> None:
    repo = SQLiteRepository(str(tmp_path / "concurrency.sqlite"))
    repo.open()
    backend = MeasuredConcurrencyBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'concurrency.sqlite'}",
            max_concurrency=1,
            rollout_provider_concurrency=2,
            job_soft_timeout_seconds=20,
            job_timeout_seconds=30,
            backend_timeout_seconds=5,
        ),
    )
    try:
        job_id, _ = await engine.submit(
            ResearchRequest(
                question="Should the rollout canary admit exactly two physical calls?",
                branch_count=3,
            )
        )
        for _ in range(400):
            row = repo.get_job(job_id)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "succeeded"
        assert backend.max_active_rollouts == 2
    finally:
        await engine.stop()
        repo.close()
