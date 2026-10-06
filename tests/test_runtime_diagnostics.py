from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from test_prime_progress_accounting import fixture_cli, request

from thinkroom.backends import FailoverBackend, PrimeAgentBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine, _RepositoryProviderInvocationAudit
from thinkroom.ports import BackendError, BackendResult, BackendTransportMetrics
from thinkroom.process_backend import ProcessIsolatedBackend
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import ResearchRequest
from thinkroom.service import JsonFormatter


def test_history_report_keeps_partials_and_never_emits_research_text(tmp_path):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "analyze_runtime_history.py"
    spec = importlib.util.spec_from_file_location("history_report", script)
    assert spec and spec.loader
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    path = tmp_path / "history.sqlite"
    repo = SQLiteRepository(str(path))
    repo.open()
    try:
        repo.create_job(
            ResearchRequest(question="PRIVATE_RESEARCH_TEXT", context="PRIVATE_CONTEXT"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=1200),
        )
        value = report.summarize(path)
        assert value["states"] == {"queued": 1}
        assert value["snapshot"] == "one_read_only_transaction"
        assert "PRIVATE_" not in json.dumps(value)
        assert report.latency_summary([10, 30, 20])["p95_seconds"] == 30
    finally:
        repo.close()


def test_prime_prompt_uses_explicit_spawn_and_preserves_handle():
    prompt, *_ = PrimeAgentBackend("/not/run", "openrouter", "glm", "high")._render_request(
        request()
    )
    assert "rlm.spawn" in prompt
    assert "_thinkroom_child" in prompt
    assert "name='thinkroom-frame-worker'" in prompt


async def test_success_has_ordered_runtime_milestones(tmp_path):
    executable = fixture_cli(tmp_path, "rlm_child_update", "valid")
    backend = PrimeAgentBackend(str(executable), "", "", "off")
    result = await backend.invoke(request())
    metrics = result.transport_metrics
    assert metrics.runtime_stage == 6
    assert 0 <= metrics.prompt_accepted_ms <= metrics.child_reply_ms
    assert metrics.child_reply_ms <= metrics.cleanup_completed_ms <= metrics.elapsed_ms
    assert metrics.child_updates == 12
    assert metrics.tool_calls == 1
    assert metrics.assistant_turns == 1


async def test_timeout_retains_last_stage_and_idle_gap(tmp_path):
    executable = tmp_path / "prime-stalled"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys,time\n"
        "command=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':command['id'],'type':'response','command':'prompt','success':True}),flush=True)\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    backend = PrimeAgentBackend(str(executable), "", "", "off", timeout=0.12)
    with pytest.raises(BackendError) as caught:
        await backend.invoke(request())
    assert caught.value.code == "BACKEND_TIMEOUT"
    metrics = caught.value.transport_metrics
    assert metrics.runtime_stage == 2
    assert metrics.max_idle_ms >= 50
    assert metrics.elapsed_ms >= 100


def test_formatter_exposes_only_numeric_runtime_diagnostics():
    record = logging.makeLogRecord(
        {
            "msg": "provider_runtime_progress",
            "levelname": "INFO",
            "phase": "frame",
            "branch_id": "branch-1",
            "runtime_metrics": BackendTransportMetrics(runtime_stage=3, elapsed_ms=120).as_dict(),
            "exception_type": "ValidationError",
            "secret": "DO_NOT_LOG",
        }
    )
    value = json.loads(JsonFormatter().format(record))
    assert value["runtime_metrics"]["runtime_stage"] == 3
    assert value["branch_id"] == "branch-1"
    assert value["exception_type"] == "ValidationError"
    assert "DO_NOT_LOG" not in json.dumps(value)


async def test_runtime_diagnostics_are_durable_without_schema_change(tmp_path):
    class MeasuredScripted(ScriptedBackend):
        async def invoke(self, req):
            return BackendResult(
                await super().invoke(req),
                transport_metrics=BackendTransportMetrics(runtime_stage=6, elapsed_ms=25),
            )

    repo = SQLiteRepository(str(tmp_path / "history.sqlite"))
    repo.open()
    engine = ResearchEngine(repo, MeasuredScripted(), Settings())
    try:
        job, _ = repo.create_job(
            ResearchRequest(
                question="Can phase timing survive the next inspection?", branch_count=2
            ),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=1200),
        )
        claim = repo.claim_next_job(2, "test", "scripted", "scripted")
        assert claim
        await engine.run(*claim)
        calls = repo.provider_calls(job)
        diagnostics = [
            json.loads(row["payload"])
            for row in repo.get_artifacts(job)
            if row["kind"] == "provider_diagnostics"
        ]
        assert len(diagnostics) == len(calls) == 6
        assert {item["call_id"] for item in diagnostics} == {row["id"] for row in calls}
        assert all(item["runtime_metrics"]["elapsed_ms"] == 25 for item in diagnostics)
        assert "phase timing survive" not in json.dumps(diagnostics)
    finally:
        await engine.stop()
        repo.close()


async def test_native_process_boundary_preserves_runtime_metrics(tmp_path):
    executable = fixture_cli(tmp_path, "rlm_child_update", "valid")
    backend = ProcessIsolatedBackend(PrimeAgentBackend(str(executable), "", "", "off"))
    result = await backend.invoke(request())
    assert result.transport_metrics.runtime_stage == 6
    assert result.transport_metrics.child_updates == 12
    assert backend.active_process_count == 0


async def test_schema_failure_retains_transport_metrics_without_rejected_values(tmp_path, caplog):
    from pydantic import ValidationError
    from test_provider_failover import StaticBackend

    from thinkroom.schemas import FrameInputV1

    backend = StaticBackend(
        "measured",
        "fake",
        result=BackendResult(
            {"invalid": "PRIVATE_REJECTED_VALUE"},
            transport_metrics=BackendTransportMetrics(runtime_stage=6, elapsed_ms=42),
        ),
    )
    repo = SQLiteRepository(str(tmp_path / "schema.sqlite"))
    repo.open()
    engine = ResearchEngine(repo, backend, Settings())
    try:
        job, _ = repo.create_job(
            ResearchRequest(question="Can schema failures preserve diagnostic timing?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=1200),
        )
        claim = repo.claim_next_job(60, "test", backend.name, backend.model)
        assert claim
        with caplog.at_level(logging.WARNING), pytest.raises(ValidationError):
            await engine._phase(
                "frame",
                job,
                claim[1],
                None,
                FrameInputV1(
                    question="Can schema failures preserve diagnostic timing?",
                    domain="generic",
                    guidance="g",
                    safety="s",
                ).model_dump(),
                datetime.now(UTC) + timedelta(seconds=1200),
                "test",
                "test",
                repair_budget=[0],
            )
        diagnostics = [
            json.loads(row["payload"])
            for row in repo.get_artifacts(job)
            if row["kind"] == "provider_diagnostics"
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0]["runtime_metrics"]["runtime_stage"] == 6
        assert diagnostics[0]["output_status"] != "validated"
        logs = "\n".join(JsonFormatter().format(record) for record in caplog.records)
        assert "PRIVATE_REJECTED_VALUE" not in logs
        assert "validation_error_types" in logs
    finally:
        await engine.stop()
        repo.close()


async def test_outer_route_timeout_preserves_observation_before_process_cleanup(tmp_path):
    from test_provider_failover import StaticBackend

    executable = tmp_path / "prime-stalled-native"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys,time\n"
        "command=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':command['id'],'type':'response','command':'prompt','success':True}),flush=True)\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    primary = ProcessIsolatedBackend(PrimeAgentBackend(str(executable), "", "", "off", timeout=5))
    backend = FailoverBackend(
        primary,
        StaticBackend("fallback", "fake"),
        # Allow forkserver startup before testing the stalled, accepted RPC.
        primary_timeout_seconds=2,
        fallback_timeout_seconds=1,
    )
    repo = SQLiteRepository(str(tmp_path / "timeout.sqlite"))
    repo.open()
    try:
        job, _ = repo.create_job(
            ResearchRequest(question="Can a hard timeout preserve the last observation?"),
            "hash",
            None,
            100,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        claim = repo.claim_next_job(60, "test", backend.name, backend.model)
        assert claim
        req = request().model_copy(update={"job_id": job, "attempt_id": claim[1]})
        assert await backend.invoke_with_audit(
            req, _RepositoryProviderInvocationAudit(repo, 0)
        ) == {"ok": True}
        rows = [
            json.loads(row["payload"])
            for row in repo.get_artifacts(job)
            if row["kind"] == "provider_diagnostics"
        ]
        assert len(rows) == 1
        assert rows[0]["output_status"] == "BACKEND_TIMEOUT_ROUTE"
        assert rows[0]["runtime_metrics"]["runtime_stage"] == 2
        assert primary.active_process_count == 0
    finally:
        repo.close()


def test_progress_cannot_be_mistaken_for_terminal_output():
    from thinkroom.process_backend import _receive_terminal

    class Pipe:
        def recv_bytes(self, limit):
            return b'{"kind":"progress","transport_metrics":{"runtime_stage":"secret"}}'

    with pytest.raises(BackendError, match="invalid provider progress metrics"):
        _receive_terminal(Pipe(), request(), "primary", "fake")
