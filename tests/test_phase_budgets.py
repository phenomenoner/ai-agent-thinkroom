from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_provider_failover import BlockingBackend, StaticBackend, frame_request

from thinkroom.backends import FailoverBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import ResearchRequest


async def test_route_timeout_includes_wrapper_and_reaches_fallback():
    primary = BlockingBackend()
    fallback = StaticBackend("fallback", "fake")
    backend = FailoverBackend(
        primary, fallback, primary_timeout_seconds=0.03, fallback_timeout_seconds=0.1
    )
    result = await asyncio.wait_for(backend.invoke(frame_request()), timeout=0.5)
    assert result == {"ok": True}
    assert fallback.calls == 1


async def test_completed_rollouts_can_use_reserved_final_phases(tmp_path):
    class CrossSoftBoundary(ScriptedBackend):
        async def invoke(self, request):
            result = await super().invoke(request)
            if request.phase == "rollout":
                await asyncio.sleep(0.65)
            return result

    repo = SQLiteRepository(str(tmp_path / "budget.sqlite"))
    repo.open()
    settings = Settings(
        job_soft_timeout_seconds=0.5,
        job_timeout_seconds=10,
        backend_timeout_seconds=1,
        rollout_provider_concurrency=2,
    )
    engine = ResearchEngine(repo, CrossSoftBoundary(), settings)
    try:
        job_id, _ = repo.create_job(
            ResearchRequest(question="Can final phases use reserved time?", branch_count=2),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=10),
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        await engine.run(*claim)
        artifacts = repo.get_artifacts(job_id)
        assert any(row["kind"] == "synthesis" for row in artifacts), [
            dict(row) for row in artifacts
        ]
    finally:
        await engine.stop()
        repo.close()


async def test_retry_does_not_consume_fallback_reserve():
    from thinkroom.ports import BackendError

    primary = StaticBackend("primary", "fake", error=BackendError("RATE_LIMITED", "busy"))
    fallback = StaticBackend("fallback", "fake")
    backend = FailoverBackend(
        primary,
        fallback,
        primary_timeout_seconds=0.1,
        fallback_timeout_seconds=0.2,
        retry_delay_seconds=(0.2, 0.2),
    )
    request = frame_request().model_copy(
        update={"deadline": datetime.now(UTC) + timedelta(seconds=0.3)}
    )
    assert await backend.invoke(request) == {"ok": True}
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_admission_failure_is_durable_without_a_provider_call(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "admission.sqlite"))
    repo.open()
    engine = ResearchEngine(repo, ScriptedBackend(), Settings())
    try:
        job_id, _ = repo.create_job(
            ResearchRequest(question="Is admission separate from execution?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        request = frame_request().model_copy(update={"job_id": job_id, "attempt_id": claim[1]})
        from thinkroom.ports import BackendError

        with pytest.raises(BackendError, match="admission"):
            await engine._invoke_provider_bounded(
                request, request.deadline, 0, datetime.now(UTC) - timedelta(seconds=1)
            )
        assert repo.provider_calls(job_id) == []
        artifacts = repo.get_artifacts(job_id)
        assert any(row["kind"] == "admission" for row in artifacts)
    finally:
        await engine.stop()
        repo.close()


async def test_late_primary_result_after_cancellation_is_not_accepted():
    class LatePrimary(StaticBackend):
        async def invoke(self, request):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return {"late": True}

    backend = FailoverBackend(
        LatePrimary("primary", "fake"),
        StaticBackend("fallback", "fake"),
        primary_timeout_seconds=0.03,
        fallback_timeout_seconds=0.1,
    )
    assert await asyncio.wait_for(backend.invoke(frame_request()), timeout=0.5) == {"ok": True}


@pytest.mark.parametrize("primary,fallback,hard_seconds", [(90, 180, 1200), (300, 600, 1800)])
async def test_configured_route_envelopes_do_not_erase_early_work(
    tmp_path, primary, fallback, hard_seconds
):
    repo = SQLiteRepository(str(tmp_path / "reserves.sqlite"))
    repo.open()
    calls = []
    backend = FailoverBackend(
        ScriptedBackend(calls=calls),
        ScriptedBackend(calls=calls),
        primary_timeout_seconds=primary,
        fallback_timeout_seconds=fallback,
    )
    engine = ResearchEngine(
        repo, backend, Settings(backend_timeout_seconds=fallback, job_timeout_seconds=hard_seconds)
    )
    hard = datetime.now(UTC) + timedelta(seconds=hard_seconds)
    try:
        job, _ = repo.create_job(
            ResearchRequest(question="Do real configured budgets leave useful work?"),
            "hash",
            None,
            100,
            hard,
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        await engine.run(*claim)
        assert any(row["kind"] == "synthesis" for row in repo.get_artifacts(job))
        assert {call.phase for call in calls} == {
            "frame",
            "fork",
            "rollout",
            "critique",
            "synthesis",
        }
        assert all(call.deadline == hard for call in calls)
    finally:
        await engine.stop()
        repo.close()


async def test_initial_deadline_insufficient_remains_failed_without_provider_start(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "insufficient.sqlite"))
    repo.open()
    calls = []
    engine = ResearchEngine(repo, ScriptedBackend(calls=calls), Settings())
    try:
        job_id, _ = repo.create_job(
            ResearchRequest(question="Does an impossible initial deadline stay an error?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=1),
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        await engine.run(*claim)
        row = repo.get_job(job_id)
        assert row["state"] == "failed"
        assert json.loads(row["terminal_error"])["code"] == "DEADLINE_INSUFFICIENT"
        assert calls == []
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.skipif(os.name != "posix", reason="production process custody requires POSIX")
async def test_route_timeout_drains_real_wrapper_before_fake_fallback():
    from thinkroom.process_backend import ProcessIsolatedBackend

    primary = ProcessIsolatedBackend(BlockingBackend())

    class CustodyCheckingFallback(StaticBackend):
        async def invoke(self, request):
            assert primary.active_process_count == 0
            return await super().invoke(request)

    backend = FailoverBackend(
        primary,
        CustodyCheckingFallback("fallback", "fake"),
        primary_timeout_seconds=0.5,
        fallback_timeout_seconds=1,
    )
    assert await asyncio.wait_for(backend.invoke(frame_request()), timeout=8) == {"ok": True}
    assert primary.active_process_count == 0


async def test_diagnostics_endpoint_separates_unstarted_admissions(tmp_path):
    from types import SimpleNamespace

    import httpx

    from thinkroom.api import create_app

    repo = SQLiteRepository(str(tmp_path / "diagnostics.sqlite"))
    repo.open()
    try:
        job, _ = repo.create_job(
            ResearchRequest(question="Can diagnostics avoid breaking legacy detail?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        now = datetime.now(UTC).isoformat()
        payload = {
            "phase": "rollout",
            "branch_id": "branch-a",
            "retry_index": 0,
            "reason": "SOFT_DEADLINE_REACHED",
            "wait_seconds": 1.5,
            "admission_deadline": now,
            "execution_deadline": now,
            "provider_started": False,
        }
        repo.put_artifact(job, claim[1], "admission", payload)
        app = create_app(SimpleNamespace(settings=Settings(), repo=repo))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(f"/api/v1/research/{job}/diagnostics")
            assert response.status_code == 200
            body = response.json()
            assert body["attempt_id"] == claim[1]
            assert body["admission_failures"][0]["provider_started"] is False
            assert body["admission_failures"][0]["wait_seconds"] == 1.5
            assert (await client.get("/api/v1/research/unknown/diagnostics")).status_code == 404
        assert repo.provider_calls(job) == []
    finally:
        repo.close()
