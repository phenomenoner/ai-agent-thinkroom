from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_provider_failover import BlockingBackend, StaticBackend, frame_request

from thinkroom.backends import FailoverBackend, PrimeAgentBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.ports import BackendError
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import BackendRequestV1, FrameInputV1, ResearchRequest


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


@pytest.mark.parametrize(
    "isolated",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(os.name != "posix", reason="native POSIX wrapper")
        ),
    ],
)
async def test_rpc_prompt_preflight_rejects_before_failover_provider_admission(
    tmp_path, monkeypatch, isolated
):
    from thinkroom.process_backend import ProcessIsolatedBackend

    repo = SQLiteRepository(str(tmp_path / "rpc-preflight.sqlite"))
    repo.open()
    primary = PrimeAgentBackend("/definitely/not/executed", "openrouter", "glm", "high")
    fallback = StaticBackend("fallback", "fake")
    route = ProcessIsolatedBackend(primary) if isolated else primary
    starts = []
    if isolated:

        def forbidden_process(*args, **kwargs):
            starts.append(True)
            raise AssertionError("preflight rejection must precede process construction")

        monkeypatch.setattr(route._context, "Process", forbidden_process)
    engine = ResearchEngine(
        repo,
        FailoverBackend(route, fallback, primary_timeout_seconds=1, fallback_timeout_seconds=1),
        Settings(),
    )
    try:
        job_id, _ = repo.create_job(
            ResearchRequest(question="Does the rendered RPC prompt fit?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        claim = repo.claim_next_job(2, "test", "primary", "model")
        assert claim
        budget_request = BackendRequestV1(
            phase="frame",
            job_id=job_id,
            attempt_id=claim[1],
            prompt_version="coding-v1",
            input=FrameInputV1(
                question="Does the rendered RPC prompt fit?",
                context="界" * 30000,
                domain="coding",
                guidance="g",
                safety="s",
            ),
            expected_output_schema="FrameOutputV1",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            correlation_id="correlation",
        )
        budget = primary.request_budget(budget_request)
        assert budget.prompt_bytes > budget.limit_bytes
        assert budget.rpc_command_bytes > budget.prompt_bytes
        assert budget.remaining_bytes == budget.limit_bytes - budget.rpc_command_bytes
        with pytest.raises(BackendError) as caught:
            await engine._phase(
                "frame",
                job_id,
                claim[1],
                None,
                {
                    "question": "Does the rendered RPC prompt fit?",
                    "context": "界" * 30000,
                    "domain": "coding",
                    "guidance": "g",
                    "safety": "s",
                },
                datetime.now(UTC) + timedelta(seconds=30),
                "correlation",
                "coding-v1",
            )
        assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
        assert repo.provider_calls(job_id) == []
        artifacts = repo.get_artifacts(job_id)
        admission = [row for row in artifacts if row["kind"] == "admission"]
        assert len(admission) == 1
        payload = json.loads(admission[0]["payload"])
        assert payload["reason"] == "CONTEXT_LIMIT_EXCEEDED"
        assert payload["provider_started"] is False
        assert fallback.calls == 0
        assert starts == []
        if isolated:
            assert route.active_process_count == 0
    finally:
        await engine.stop()
        repo.close()


async def test_exact_prime_rpc_bytes_and_inclusive_command_limit(monkeypatch):
    from thinkroom.backends import _prime_rpc_prompt_command

    assert (
        _prime_rpc_prompt_command('界\n"\\')
        == ('{"id":"thinkroom-provider","type":"prompt","message":"界\\n\\"\\\\"}\n').encode()
    )
    backend = PrimeAgentBackend("/not/executed", "", "", "off")
    request = frame_request()
    request = request.model_copy(
        update={"input": request.input.model_copy(update={"context": '界\n"\\'})}
    )
    budget = backend.request_budget(request)
    request = request.model_copy(
        update={
            "input": request.input.model_copy(
                update={"context": request.input.context + "x" * (65536 - budget.rpc_command_bytes)}
            )
        }
    )
    prompt, *_, exact = backend._render_request(request)
    assert exact.prompt_bytes == len(prompt.encode("utf-8")) < 65536
    assert exact.rpc_command_bytes == len(_prime_rpc_prompt_command(prompt)) == 65536
    assert backend.preflight(request).remaining_bytes == 0
    oversized = request.model_copy(
        update={"input": request.input.model_copy(update={"context": request.input.context + "x"})}
    )
    assert backend.request_budget(oversized).rpc_command_bytes == 65537
    assert backend.request_budget(oversized).prompt_bytes < 65536
    with pytest.raises(BackendError) as caught:
        backend.preflight(oversized)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"

    starts = []

    async def forbidden_start(*args, **kwargs):
        starts.append(args)
        raise AssertionError("over-budget command must fail before process startup")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_start)
    with pytest.raises(BackendError) as invoked:
        await backend.invoke(oversized)
    assert invoked.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert "65537 UTF-8 bytes" in str(invoked.value)
    assert starts == []


@pytest.mark.skipif(os.name != "posix", reason="native POSIX wrapper")
def test_process_preflight_keeps_legacy_backend_without_method():
    from thinkroom.process_backend import ProcessIsolatedBackend

    backend = StaticBackend("legacy", "fake")
    route = ProcessIsolatedBackend(backend)
    assert route.preflight(frame_request()) is None
    assert route.active_process_count == 0
    assert backend.calls == 0


async def test_preflighted_small_request_is_admitted_once(tmp_path):
    class PreflightedBackend(ScriptedBackend):
        name = "preflighted"
        model = "preflighted-v1"

        def __init__(self) -> None:
            super().__init__()
            self.preflight_calls = 0

        def preflight(self, request: BackendRequestV1) -> None:
            self.preflight_calls += 1

    repo = SQLiteRepository(str(tmp_path / "preflight-success.sqlite"))
    repo.open()
    primary = PreflightedBackend()
    fallback = StaticBackend("fallback", "fake")
    engine = ResearchEngine(
        repo,
        FailoverBackend(primary, fallback, primary_timeout_seconds=1, fallback_timeout_seconds=1),
        Settings(),
    )
    try:
        job_id, _ = repo.create_job(
            ResearchRequest(question="Does a small request remain admitted?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        claim = repo.claim_next_job(2, "test", "primary", "model")
        assert claim
        result = await engine._phase(
            "frame",
            job_id,
            claim[1],
            None,
            {
                "question": "Does a small request remain admitted?",
                "context": "small context",
                "domain": "coding",
                "guidance": "g",
                "safety": "s",
            },
            datetime.now(UTC) + timedelta(seconds=30),
            "correlation",
            "coding-v1",
        )
        assert result.decision == "Does a small request remain admitted?"
        assert primary.preflight_calls == 1
        assert fallback.calls == 0
        rows = repo.provider_calls(job_id)
        assert len(rows) == 1
        assert rows[0]["output_status"] == "validated"
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
