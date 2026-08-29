import ast
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Never

import httpx
import pytest
from pydantic import ValidationError

from thinkroom.api import create_app
from thinkroom.backends import BackendError, OpenAIBackend, PrimeAgentBackend, ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import (
    BackendRequestV1,
    BranchOutputV1,
    CritiqueInputV1,
    CritiqueOutputV1,
    EvidenceStatus,
    EvidenceV1,
    FrameInputV1,
    JobState,
    ResearchDetail,
    ResearchRequest,
    SynthesisOutputV1,
    TerminalErrorV1,
)
from thinkroom.service import ServiceLock, ThinkroomService
from thinkroom.skills import install, plan
from thinkroom.skills import status as skill_status


def test_skills_fsyncs_every_parent_after_directory_creation(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    created_parent_identities: list[tuple[int, int]] = []
    synced_directory_identities: list[tuple[int, int]] = []
    real_mkdir = skill_module.os.mkdir
    real_fsync = skill_module.os.fsync

    def tracking_mkdir(name, mode=0o777, *, dir_fd=None):
        assert dir_fd is not None
        info = skill_module.os.fstat(dir_fd)
        created_parent_identities.append((info.st_dev, info.st_ino))
        return real_mkdir(name, mode=mode, dir_fd=dir_fd)

    def tracking_fsync(fd):
        info = skill_module.os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced_directory_identities.append((info.st_dev, info.st_ino))
        return real_fsync(fd)

    monkeypatch.setattr(skill_module.os, "mkdir", tracking_mkdir)
    monkeypatch.setattr(skill_module.os, "fsync", tracking_fsync)
    install(tmp_path / "skills")
    assert created_parent_identities
    assert set(created_parent_identities) <= set(synced_directory_identities)


def test_skills_rolls_back_directory_when_parent_fsync_fails(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    parent_info = tmp_path.stat()
    parent_identity = (parent_info.st_dev, parent_info.st_ino)
    real_fsync = skill_module.os.fsync
    failed = False

    def fail_target_parent_once(fd):
        nonlocal failed
        info = skill_module.os.fstat(fd)
        if (
            not failed
            and stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino) == parent_identity
        ):
            failed = True
            raise OSError("simulated directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(skill_module.os, "fsync", fail_target_parent_once)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        install(target)
    assert failed
    assert not target.exists()


@pytest.mark.asyncio
async def test_rest_rejects_unknown_strategy_before_job_acceptance(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/v1/research",
                json={
                    "question": "Should this unknown strategy be accepted durably?",
                    "strategy": "not-registered",
                },
            )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_REQUEST"
        assert service.repo.list_jobs(1, None) == []
    finally:
        await service.stop()


def test_critique_rejects_duplicate_branch_assessments():
    payload = {
        "agreements": ["agreement"],
        "contradictions": ["contradiction"],
        "unsupported_claims": ["unsupported"],
        "blind_spots": ["blind spot"],
        "discriminating_evidence": ["evidence"],
        "branch_assessments": [
            {
                "branch_id": "branch-a",
                "strengths": ["strength"],
                "weaknesses": ["weakness"],
                "support_level": "mixed",
            },
            {
                "branch_id": "branch-a",
                "strengths": ["strength"],
                "weaknesses": ["weakness"],
                "support_level": "weak",
            },
        ],
        "consumed_branch_ids": ["branch-a", "branch-b"],
    }
    with pytest.raises(ValidationError, match="duplicate branch assessment"):
        CritiqueOutputV1.model_validate(payload)


class BlockingBackend:
    name = "blocking"
    model = "blocking-v1"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def invoke(self, request: BackendRequestV1) -> Never:
        self.started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("blocking backend unexpectedly resumed")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class SemanticRepairBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.critique_calls = 0
        self.synthesis_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        result = await super().invoke(request)
        if request.phase == "critique":
            self.critique_calls += 1
            if self.critique_calls == 1:
                result["consumed_branch_ids"] = ["wrong-branch"]
                for assessment in result["branch_assessments"]:
                    assessment["branch_id"] = "wrong-branch"
        if request.phase == "synthesis":
            self.synthesis_calls += 1
            if self.synthesis_calls == 1:
                result["source_attempt_id"] = "wrong-attempt"
                result["consumed_branch_ids"] = ["wrong-branch"]
                result["consumed_critique_id"] = "wrong-critique"
        return result


class DuplicatePerspectiveIdBackend(ScriptedBackend):
    def __init__(self, duplicate_attempts: int = 1) -> None:
        super().__init__(calls=[])
        self.fork_calls = 0
        self.duplicate_attempts = duplicate_attempts

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        result = await super().invoke(request)
        if request.phase == "fork":
            self.fork_calls += 1
            if self.fork_calls <= self.duplicate_attempts:
                result["perspectives"][1]["id"] = result["perspectives"][0]["id"]
        return result


class MalformedForkBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.fork_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "fork":
            self.fork_calls += 1
            raise BackendError(
                "MALFORMED_PROVIDER_OUTPUT", "provider output was not one JSON object"
            )
        return await super().invoke(request)


class DiversityRetryProviderFailureBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.fork_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "fork":
            self.fork_calls += 1
            if self.fork_calls > 1:
                raise BackendError("PROVIDER_ERROR", "regeneration transport failed")
            result = await super().invoke(request)
            first = result["perspectives"][0]
            second = result["perspectives"][1]
            second["title"] = first["title"]
            second["hypothesis"] = first["hypothesis"]
            second["approach"] = first["approach"]
            return result
        return await super().invoke(request)


class DeadlineOverrunBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "synthesis":
            remaining = (request.deadline - datetime.now(UTC)).total_seconds()
            await asyncio.sleep(max(0.0, remaining) + 0.02)
        return await super().invoke(request)


class BackendTimeoutBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "frame":
            await asyncio.sleep(2)
        return await super().invoke(request)


class EmptyExceptionBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise RuntimeError()


class HostileException(Exception):
    @property
    def code(self) -> str:
        raise RuntimeError("code property failed")

    def __str__(self) -> str:
        raise RuntimeError("string conversion failed")


class HostileExceptionBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise HostileException()


class HostileString(str):
    def __len__(self) -> int:
        raise RuntimeError("length failed")

    def __eq__(self, other) -> bool:
        raise RuntimeError("comparison failed")


class HostileCodeException(Exception):
    @property
    def code(self) -> str:
        return HostileString("PROVIDER_ERROR")


class HostileCodeBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise HostileCodeException("provider failed")


class SecretFailureBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise RuntimeError("secret-provider-value-must-not-escape")


class LedgerForgeryBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.synthesis_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        result = await super().invoke(request)
        if request.phase == "synthesis":
            self.synthesis_calls += 1
            if self.synthesis_calls == 1:
                result["evidence_ledger"][0]["statement"] = "fabricated evidence statement"
                result["evidence_ledger"][0]["evidence_id"] = "provider-invented-evidence"
        return result


class HighEvidenceBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        result = await super().invoke(request)
        if request.phase != "rollout":
            return result
        branch_id = request.branch_id or "branch"
        evidence = [
            {
                "id": f"evidence-{index}",
                "statement": f"Evidence {index} from {branch_id}",
                "relationship": "supports",
                "verification_status": "unverified",
            }
            for index in range(10)
        ]
        result["supporting_evidence"] = evidence
        result["claims"][0]["evidence_ids"] = [evidence[0]["id"]]
        return result


class ProviderFailureBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.frame_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "frame":
            self.frame_calls += 1
            raise BackendError("PROVIDER_ERROR", "provider unavailable")
        return await super().invoke(request)


class LongMalformedBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.frame_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "frame":
            self.frame_calls += 1
            if self.frame_calls == 1:
                raise BackendError("MALFORMED_PROVIDER_OUTPUT", "x" * 10000)
        return await super().invoke(request)


class SemanticThenMalformedBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.critique_calls = 0

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase != "critique":
            return await super().invoke(request)
        self.critique_calls += 1
        if self.critique_calls == 2:
            raise BackendError("MALFORMED_PROVIDER_OUTPUT", "critique malformed")
        result = await super().invoke(request)
        if self.critique_calls == 1:
            result["consumed_branch_ids"] = ["wrong-branch"]
            result["branch_assessments"][0]["branch_id"] = "wrong-branch"
        return result


@pytest.mark.asyncio
async def test_worker_pool_and_persisted_deadline(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", max_concurrency=1
    )
    engine = ResearchEngine(repo, ScriptedBackend(), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", deadline_seconds=30)
    )
    row = repo.get_job(job)
    assert row and row["deadline"]
    assert datetime.fromisoformat(row["deadline"]) > datetime.now(UTC)
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_queue_bound_idempotency_first_and_active_cancel(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = BlockingBackend()
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        max_concurrency=1,
        max_queued_jobs=1,
    )
    engine = ResearchEngine(repo, backend, settings)
    await engine.start()
    request = ResearchRequest(question="Should we choose this important option?")
    active, _ = await engine.submit(request)
    await asyncio.wait_for(backend.started.wait(), timeout=2)
    queued, existing = await engine.submit(request, "same-key")
    assert not existing
    replayed, existing = await engine.submit(request, "same-key")
    assert existing and replayed == queued
    with pytest.raises(RuntimeError, match="QUEUE_FULL"):
        await engine.submit(ResearchRequest(question="Should we choose another important option?"))
    assert await engine.cancel(active)
    await asyncio.wait_for(backend.cancelled.wait(), timeout=2)
    for _ in range(100):
        row = repo.get_job(active)
        if row and row["state"] == JobState.CANCELLED.value:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(active)
    assert row and row["state"] == JobState.CANCELLED.value
    await engine.cancel(queued)
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_semantic_provenance_gets_one_bounded_repair(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = SemanticRepairBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {
            JobState.SUCCEEDED.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == JobState.SUCCEEDED.value
    assert backend.critique_calls == 2
    assert backend.synthesis_calls == 2
    artifacts = repo.get_artifacts(job, row["attempt_id"])
    critique = next(a for a in artifacts if a["kind"] == "critique")
    synthesis = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "synthesis"))
    assert synthesis["consumed_critique_id"] == f"critique-{critique['id']}"
    calls = (
        repo._db()
        .execute(
            "SELECT phase, retry_index FROM provider_calls "
            "WHERE job_id=? AND phase IN ('critique','synthesis') ORDER BY id",
            (job,),
        )
        .fetchall()
    )
    assert [(call["phase"], call["retry_index"]) for call in calls] == [
        ("critique", 0),
        ("critique", 1),
        ("synthesis", 0),
        ("synthesis", 1),
    ]
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_semantic_repair_cannot_nest_a_malformed_retry(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = SemanticThenMalformedBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    row = None
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert row and row["state"] == "failed"
    assert backend.critique_calls == 2
    assert json.loads(row["terminal_error"])["code"] == "MALFORMED_PROVIDER_OUTPUT"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_duplicate_perspective_ids_regenerate_before_persistence(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = DuplicatePerspectiveIdBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "succeeded"
    artifacts = repo.get_artifacts(job, row["attempt_id"])
    branches = [artifact["branch_id"] for artifact in artifacts if artifact["kind"] == "branch"]
    assert len(branches) == len(set(branches)) == 2
    critique = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "critique"))
    synthesis = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "synthesis"))
    assert set(critique["consumed_branch_ids"]) == set(branches)
    assert set(synthesis["consumed_branch_ids"]) == set(branches)
    assert backend.fork_calls == 2
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_repeated_duplicate_perspective_ids_use_unique_fallbacks(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = DuplicatePerspectiveIdBackend(duplicate_attempts=2)
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "succeeded"
    artifacts = repo.get_artifacts(job, row["attempt_id"])
    fork = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "fork"))
    branches = [artifact["branch_id"] for artifact in artifacts if artifact["kind"] == "branch"]
    critique = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "critique"))
    synthesis = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "synthesis"))
    assert fork["provenance_warning"] == "provider fork invalid; deterministic fallback used"
    assert len(branches) == len(set(branches)) == 2
    assert set(critique["consumed_branch_ids"]) == set(branches)
    assert set(synthesis["consumed_branch_ids"]) == set(branches)
    assert backend.fork_calls == 2
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_repeated_malformed_fork_output_uses_deterministic_fallback(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = MalformedForkBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "succeeded"
        artifacts = repo.get_artifacts(job, row["attempt_id"])
        fork = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "fork"))
        perspectives = fork["perspectives"]
        branches = [a["branch_id"] for a in artifacts if a["kind"] == "branch"]
        assert fork["provenance_warning"] == "provider fork invalid; deterministic fallback used"
        assert len({p["id"] for p in perspectives}) == len(perspectives) == 2
        assert len(set(branches)) == len(branches) == 2
        assert backend.fork_calls == 2
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_diversity_regeneration_provider_failure_is_fatal(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = DiversityRetryProviderFailureBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "failed"
        assert json.loads(row["terminal_error"])["code"] == "PROVIDER_ERROR"
        assert not any(a["kind"] == "fork" for a in repo.get_artifacts(job, row["attempt_id"]))
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_provider_failure_is_not_retried(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = ProviderFailureBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        row = None
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "failed"
        assert json.loads(row["terminal_error"])["code"] == "PROVIDER_ERROR"
        assert backend.frame_calls == 1
        retries = (
            repo._db()
            .execute("SELECT retry_index FROM provider_calls WHERE job_id=? ORDER BY id", (job,))
            .fetchall()
        )
        assert [call["retry_index"] for call in retries] == [0]
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_long_malformed_feedback_is_bounded_and_retried_once(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = LongMalformedBackend()
    engine = ResearchEngine(
        repo,
        backend,
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        row = None
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert row and row["state"] == "succeeded"
        assert backend.frame_calls == 2
        retries = (
            repo._db()
            .execute(
                "SELECT retry_index FROM provider_calls WHERE job_id=? AND phase='frame' ORDER BY id",
                (job,),
            )
            .fetchall()
        )
        assert [call["retry_index"] for call in retries] == [0, 1]
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_final_deadline_overrun_cannot_succeed(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        job_timeout_seconds=1,
        backend_timeout_seconds=1,
    )
    engine = ResearchEngine(repo, DeadlineOverrunBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(300):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    terminal_error = json.loads(row["terminal_error"])
    assert terminal_error["code"] == "DEADLINE_EXCEEDED"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_backend_timeout_persists_a_valid_nonempty_terminal_error(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        job_timeout_seconds=30,
        backend_timeout_seconds=1,
    )
    engine = ResearchEngine(repo, BackendTimeoutBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(300):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    terminal_error = TerminalErrorV1.model_validate_json(row["terminal_error"])
    assert terminal_error.code == "BACKEND_TIMEOUT"
    assert terminal_error.message == "backend invocation timed out"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_unexpected_empty_exception_persists_valid_terminal_fallback(tmp_path, caplog):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, EmptyExceptionBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    terminal_error = TerminalErrorV1.model_validate_json(row["terminal_error"])
    assert terminal_error.code == "INTERNAL_ERROR"
    assert terminal_error.message == "job failed"
    failure = next(record for record in caplog.records if record.getMessage() == "job failed")
    assert failure.exception_type == "RuntimeError"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_hostile_exception_description_cannot_break_terminal_settlement(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, HostileExceptionBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    terminal_error = TerminalErrorV1.model_validate_json(row["terminal_error"])
    assert terminal_error.code == "INTERNAL_ERROR"
    assert terminal_error.message == "job failed"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_hostile_string_subclass_code_cannot_break_provider_audit_settlement(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, HostileCodeBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    TerminalErrorV1.model_validate_json(row["terminal_error"])
    calls = (
        repo._db()
        .execute("SELECT output_status FROM provider_calls WHERE job_id=? ORDER BY id", (job,))
        .fetchall()
    )
    assert calls and calls[0]["output_status"] == "invalid"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_provider_capacity_wait_is_bounded_by_absolute_deadline(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = ScriptedBackend(calls=[])
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        max_concurrency=1,
        job_timeout_seconds=1,
        backend_timeout_seconds=10,
    )
    engine = ResearchEngine(repo, backend, settings)
    await engine._semaphore.acquire()
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        for _ in range(300):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "failed"
        assert json.loads(row["terminal_error"])["code"] == "DEADLINE_EXCEEDED"
        assert backend.calls == []
    finally:
        engine._semaphore.release()
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_prestart_active_task_cancellation_is_durably_settled(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, ScriptedBackend(calls=[]), settings)

    class CancelBeforeFirstSlice(dict):
        def __setitem__(self, job_id, task):
            super().__setitem__(job_id, task)
            assert repo.request_cancel(job_id)
            task.cancel()

    engine._active = CancelBeforeFirstSlice()
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "cancelled"
    assert json.loads(row["terminal_error"])["code"] == "CANCELLED"
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_synthesis_cannot_falsify_evidence_ledger_statement(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = LedgerForgeryBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "succeeded"
    artifacts = repo.get_artifacts(job, row["attempt_id"])
    synthesis = json.loads(next(a["payload"] for a in artifacts if a["kind"] == "synthesis"))
    assert all(
        item["statement"] != "fabricated evidence statement"
        for item in synthesis["evidence_ledger"]
    )
    assert backend.synthesis_calls == 1
    await engine.stop()
    repo.close()


def test_evidence_verifier_output_is_revalidated_and_bounded(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(
        repo, ScriptedBackend(), settings, evidence_verifier=lambda _: "x" * 5000
    )
    branch = BranchOutputV1.model_validate(
        {
            "summary": "summary",
            "claims": [{"statement": "claim", "evidence_ids": ["evidence-1"]}],
            "supporting_evidence": [
                {
                    "id": "evidence-1",
                    "statement": "statement",
                    "relationship": "supports",
                    "source_reference": "https://example.invalid/source",
                    "verification_status": EvidenceStatus.VERIFIED,
                    "verification_basis": "provider assertion",
                }
            ],
            "contradicting_evidence": [],
            "assumptions": ["assumption"],
            "uncertainties": ["uncertainty"],
            "falsifiers": ["falsifier"],
            "next_checks": ["check"],
        }
    )
    normalized = engine._normalize_evidence(branch)
    validated = BranchOutputV1.model_validate(normalized.model_dump(mode="python"))
    assert len(validated.supporting_evidence[0].verification_basis or "") == 4000
    repo.close()


@pytest.mark.asyncio
async def test_synthesis_ledger_is_bounded_valid_and_readable_via_detail_api(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    assert service.engine is not None
    await service.engine.stop()
    service.engine = ResearchEngine(service.repo, HighEvidenceBackend(), settings)
    await service.engine.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            created = await client.post(
                "/api/v1/research",
                json={
                    "question": "Should we choose this important bounded option?",
                    "branch_count": 6,
                },
            )
            assert created.status_code == 202
            job_id = created.json()["job_id"]
            row = None
            for _ in range(300):
                row = service.repo.get_job(job_id)
                if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert row and row["state"] == "succeeded"
            response = await client.get(f"/api/v1/research/{job_id}")
        assert response.status_code == 200
        synthesis = SynthesisOutputV1.model_validate(response.json()["synthesis"])
        assert len(synthesis.evidence_ledger) == 50
        assert {item.branch_id for item in synthesis.evidence_ledger} == {
            f"branch-perspective-{index}" for index in range(1, 7)
        }
        assert any("projects 50 of 60" in item for item in synthesis.uncertainties)
    finally:
        await service.stop()


@pytest.mark.parametrize(
    "terminal_state", [JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED]
)
def test_recovery_reconciles_active_attempts_on_terminal_jobs(tmp_path, terminal_state):
    path = tmp_path / f"{terminal_state.value}.sqlite"
    repo = SQLiteRepository(str(path))
    repo.open()
    request = ResearchRequest(question="Should we choose this important option?")
    job, _ = repo.create_job(request, "hash", deadline=datetime.now(UTC) + timedelta(minutes=5))
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    aid = claimed[1]
    if terminal_state is JobState.SUCCEEDED:
        for state in (JobState.ROLLING_OUT, JobState.CRITIQUING, JobState.SYNTHESIZING):
            assert repo.transition(job, state, aid, "advance", "test")
    assert repo.transition(job, terminal_state, aid, "simulated crash window", "test")
    assert repo.attempts(job)[0]["state"] == "active"
    repo.close()

    recovered = SQLiteRepository(str(path))
    recovered.open()
    recovered.recover_startup(2)
    attempt = recovered.attempts(job)[0]
    assert attempt["state"] == "terminal"
    assert attempt["ended_at"] is not None
    assert attempt["outcome"] == terminal_state.value
    assert attempt["recovery_reason"] == "terminal attempt reconciliation"
    recovered.close()


def test_restart_requeues_abandoned_attempt(tmp_path):
    path = str(tmp_path / "db.sqlite")
    request = ResearchRequest(question="Should we choose this important option?")
    first = SQLiteRepository(path)
    first.open()
    job, _ = first.create_job(
        request, "restart-hash", max_queued=1, deadline=datetime.now(UTC) + timedelta(minutes=1)
    )
    claimed = first.claim_next_job(2, "worker-1")
    assert claimed is not None and claimed[0] == job
    first.close()
    second = SQLiteRepository(path)
    second.open()
    second.recover_startup(2)
    assert second.get_job(job)["state"] == JobState.QUEUED.value
    assert second.claim_next_job(2, "worker-2") is not None
    second.close()


@pytest.mark.asyncio
async def test_requeued_job_detail_hides_abandoned_attempt_artifacts(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    service.repo.open()
    try:
        request = ResearchRequest(question="Should we choose this important option?")
        job, _ = service.repo.create_job(
            request,
            "recovery-artifact-hash",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
        claimed = service.repo.claim_next_job(2, "worker-1", "scripted", "scripted-v1")
        assert claimed and claimed[0] == job
        aid = claimed[1]
        service.repo.put_artifact(
            job,
            aid,
            "frame",
            {
                "schema_version": 1,
                "decision": "abandoned decision",
                "constraints": ["abandoned constraint"],
                "success_criteria": ["abandoned criterion"],
                "research_questions": ["abandoned question"],
                "assumptions": ["abandoned assumption"],
                "uncertainties": ["abandoned uncertainty"],
            },
        )
        assert service.repo.transition(job, JobState.ROLLING_OUT, aid, "advance", "test")
        service.repo.recover_startup(2)
        row = service.repo.get_job(job)
        assert row and row["state"] == "queued" and row["attempt_id"] is None

        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(f"/api/v1/research/{job}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["frame"] is None
        assert detail["branches"] == []
        assert detail["perspectives"] == []
    finally:
        service.repo.close()


def test_atomic_queued_cancel_never_claims(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should we choose this important option?")
    job, _ = repo.create_job(
        request, "hash", max_queued=1, deadline=datetime.now(UTC) + timedelta(minutes=1)
    )
    assert repo.request_cancel(job)
    assert repo.claim_next_job(2, "test") is None
    assert repo.get_job(job)["state"] == JobState.CANCELLED.value
    assert repo.transitions(job)[0]["from_state"] == JobState.QUEUED.value
    repo.close()


def test_state_graph_rejects_skip(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should we choose this important option?")
    job, _ = repo.create_job(request, "hash", max_queued=1)
    with pytest.raises(ValueError):
        repo.transition(job, JobState.SYNTHESIZING, None)
    repo.close()


@pytest.mark.asyncio
async def test_rest_typed_openapi_and_oversize(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/api/v1/research", json={"question": "x" * 10001})
            assert response.status_code == 413
            openapi = (await client.get("/openapi.json")).json()
            assert "JobResource" in openapi["components"]["schemas"]
            assert "413" in openapi["paths"]["/api/v1/research"]["post"]["responses"]
    finally:
        await service.stop()


def test_phase_input_is_strict_and_discriminated():
    base = {
        "question": "A sufficiently important question",
        "domain": "generic",
        "guidance": "g",
        "safety": "s",
    }
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=base,
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC),
        correlation_id="c",
    )
    assert isinstance(request.input, FrameInputV1)
    retried = BackendRequestV1.model_validate(
        {**request.model_dump(mode="json"), "input": {**base, "validation_feedback": "fix"}}
    )
    assert retried.input.validation_feedback == "fix"
    with pytest.raises(ValidationError):
        BackendRequestV1(
            phase="frame",
            job_id="j",
            attempt_id="a",
            prompt_version="v",
            input={**base, "unknown": 1},
            expected_output_schema="FrameOutputV1",
            deadline=datetime.now(UTC),
            correlation_id="c",
        )


def test_lock_is_exclusive_before_repository_open(tmp_path):
    lock1, lock2 = (
        ServiceLock(str(tmp_path / "service.lock")),
        ServiceLock(str(tmp_path / "service.lock")),
    )
    lock1.acquire()
    try:
        with pytest.raises(RuntimeError):
            lock2.acquire()
    finally:
        lock1.release()
        lock2.release()


@pytest.mark.asyncio
async def test_service_rejects_symlink_alias_before_lock_or_database_open(tmp_path):
    real_db = tmp_path / "real.db"
    real_db.touch()
    alias_db = tmp_path / "alias.db"
    alias_db.symlink_to(real_db)
    real_settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{real_db}", max_concurrency=1
    )
    alias_settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{alias_db}", max_concurrency=1
    )
    real_service = ThinkroomService(real_settings)
    await real_service.start()
    try:
        with pytest.raises(ValueError, match="symlink"):
            ThinkroomService(alias_settings)
        assert not Path(f"{alias_db}.lock").exists()
    finally:
        await real_service.stop()


@pytest.mark.asyncio
async def test_service_rejects_symlinked_database_parent(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{alias_parent / 'thinkroom.db'}"
    )
    with pytest.raises(ValueError, match="symlink"):
        ThinkroomService(settings)
    assert not (real_parent / "thinkroom.db.lock").exists()


def test_service_rejects_hardlinked_database_identity(tmp_path):
    real_db = tmp_path / "real.db"
    real_db.touch()
    alias_db = tmp_path / "alias.db"
    alias_db.hardlink_to(real_db)
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{alias_db}")
    with pytest.raises(ValueError, match="multiple filesystem names"):
        ThinkroomService(settings)
    assert not Path(f"{alias_db}.lock").exists()


@pytest.mark.asyncio
async def test_service_revalidates_database_identity_immediately_before_lock(tmp_path):
    database = tmp_path / "database.db"
    target = tmp_path / "target.db"
    target.touch()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    service = ThinkroomService(settings)
    database.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        await service.start()
    assert service.repo.db is None
    assert not Path(f"{database}.lock").exists()


@pytest.mark.asyncio
async def test_service_revalidates_database_identity_after_lock_acquisition(tmp_path, monkeypatch):
    database = tmp_path / "database.db"
    target = tmp_path / "target.db"
    target.touch()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    service = ThinkroomService(settings)
    real_acquire = service.lock.acquire

    def swap_then_acquire():
        database.symlink_to(target)
        real_acquire()

    monkeypatch.setattr(service.lock, "acquire", swap_then_acquire)
    with pytest.raises(ValueError, match="symlink"):
        await service.start()
    assert service.repo.db is None
    assert service.lock.handle is None


@pytest.mark.asyncio
async def test_service_rejects_database_parent_writable_by_untrusted_principals(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    database = shared / "database.db"
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    service = ThinkroomService(settings)
    with pytest.raises(ValueError, match="ancestor is writable by untrusted principals"):
        await service.start()
    assert service.repo.db is None
    assert not Path(f"{database}.lock").exists()


@pytest.mark.asyncio
async def test_service_rejects_database_file_writable_by_untrusted_principals(tmp_path):
    database = tmp_path / "database.db"
    database.touch(mode=0o666)
    database.chmod(0o666)
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    service = ThinkroomService(settings)
    with pytest.raises(ValueError, match="database is writable by untrusted principals"):
        await service.start()
    assert service.repo.db is None
    assert not Path(f"{database}.lock").exists()


def test_skills_refuse_unmanaged_and_symlink(tmp_path):
    target = tmp_path / "skills"
    target.mkdir()
    (target / "thinkroom-install").mkdir()
    (target / "thinkroom-install" / "SKILL.md").write_text("unmanaged")
    assert plan(target)[0]["classification"] == "DIVERGED"
    with pytest.raises(ValueError):
        install(target)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_target = tmp_path / "symlink"
    symlink_target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        install(symlink_target)


def test_skills_install_resists_parent_swap_during_atomic_replace(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    detached = tmp_path / "detached-trigger"
    real_publish = skill_module._link_fd_noreplace
    attacked = False

    def swap_parent(parent_fd, source_fd, target_name):
        nonlocal attacked
        parent = target / "thinkroom-trigger"
        if target_name == "SKILL.md" and parent.is_dir() and not attacked:
            parent.rename(detached)
            parent.symlink_to(outside, target_is_directory=True)
            attacked = True
        return real_publish(parent_fd, source_fd, target_name)

    monkeypatch.setattr(skill_module, "_link_fd_noreplace", swap_parent)
    with pytest.raises(RuntimeError, match="failed to roll back Skills install"):
        skill_module.install(target)
    assert attacked
    assert not (outside / "SKILL.md").exists()
    assert detached.is_dir()
    assert not (detached / "SKILL.md").exists()
    assert not (target / ".thinkroom" / "skills-receipt-v1.json").exists()


def test_skills_mutations_are_serialized_by_target_root_lock(tmp_path):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    owner = skill_module._SecureTree(target, create=True)
    try:
        with pytest.raises(RuntimeError, match="owns the target root"):
            skill_module.install(target)
    finally:
        owner.close()


def test_skills_mutation_rejects_target_writable_by_other_principals(tmp_path):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    target.mkdir(mode=0o770)
    target.chmod(0o770)
    with pytest.raises(ValueError, match="untrusted principals"):
        skill_module.install(target)
    assert not list(target.glob("**/SKILL.md"))


def test_skills_mutation_rejects_writable_nonsticky_ancestor(tmp_path):
    from thinkroom import skills as skill_module

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    target = shared / "skills"
    with pytest.raises(ValueError, match="ancestor is writable"):
        skill_module.install(target)
    assert not target.exists()


def test_skills_mutation_rejects_sticky_ancestor_owned_by_another_principal(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o1777)
    attacker.chmod(0o1777)
    attacker_uid = attacker.stat().st_uid
    monkeypatch.setattr(skill_module.os, "geteuid", lambda: attacker_uid + 1)
    with pytest.raises(ValueError, match="ancestor is writable"):
        skill_module._assert_protected_parent(attacker.stat())


def test_skills_plan_is_read_only_and_rejects_dangling_ancestor(tmp_path):
    target = tmp_path / "fresh"
    assert all(item["classification"] == "ADD" for item in plan(target))
    assert not target.exists()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError):
        plan(dangling / "skills")


def test_skills_manifest_required_fields_and_manifest_symlink_fail_before_mutation(
    tmp_path, monkeypatch
):
    import thinkroom.skills as skill_module

    source = skill_module.BUNDLE
    malformed = tmp_path / "malformed-bundle"
    shutil.copytree(source, malformed)
    manifest_path = malformed / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("product_version")
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(skill_module, "BUNDLE", malformed)
    target = tmp_path / "target"
    for operation in (plan, skill_status, install):
        with pytest.raises(ValueError):
            operation(target)
        assert not target.exists()

    symlinked = tmp_path / "symlinked-bundle"
    shutil.copytree(source, symlinked)
    real_manifest = tmp_path / "real-manifest.json"
    real_manifest.write_bytes((symlinked / "manifest.json").read_bytes())
    (symlinked / "manifest.json").unlink()
    (symlinked / "manifest.json").symlink_to(real_manifest)
    monkeypatch.setattr(skill_module, "BUNDLE", symlinked)
    for operation in (plan, skill_status, install):
        with pytest.raises(ValueError):
            operation(target)
        assert not target.exists()


def test_backend_model_names_fit_attempt_contract():
    OpenAIBackend("https://example.invalid/v1", "secret", "x" * 256)
    PrimeAgentBackend("prime-agent", "provider", "x" * 256, "off")
    with pytest.raises(ValueError, match="model"):
        OpenAIBackend("https://example.invalid/v1", "secret", "x" * 257)
    with pytest.raises(ValueError, match="model"):
        PrimeAgentBackend("prime-agent", "provider", "x" * 257, "off")


@pytest.mark.asyncio
async def test_selected_provider_configuration_fails_before_repository_open(tmp_path, monkeypatch):
    database = tmp_path / "db.sqlite"
    monkeypatch.setenv("THINKROOM_OPENAI_API_KEY", "redacted-test-value")
    monkeypatch.setenv("THINKROOM_OPENAI_BASE_URL", "not-a-url")
    openai = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}", backend="openai")
    )
    with pytest.raises(ValueError, match="base URL"):
        await openai.start()
    assert openai.repo.db is None

    missing = tmp_path / "missing-prime-agent"
    monkeypatch.setenv("THINKROOM_PRIME_AGENT_EXECUTABLE", str(missing))
    prime = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}", backend="prime_agent")
    )
    with pytest.raises(ValueError, match="executable"):
        await prime.start()
    assert prime.repo.db is None


def test_prime_backend_uses_one_documented_executable_variable(tmp_path, monkeypatch):
    executable = tmp_path / "prime-agent"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.delenv("THINKROOM_PRIME_AGENT_EXECUTABLE", raising=False)
    monkeypatch.setenv("THINKROOM_PRIME_EXECUTABLE", str(executable))

    with pytest.raises(ValueError, match="THINKROOM_PRIME_AGENT_EXECUTABLE"):
        PrimeAgentBackend.from_env(timeout=1)

    monkeypatch.setenv("THINKROOM_PRIME_AGENT_EXECUTABLE", str(executable))
    backend = PrimeAgentBackend.from_env(timeout=1)
    assert backend.executable == str(executable.resolve())


def test_openai_backend_uses_only_namespaced_api_key(monkeypatch):
    from thinkroom.backends import OpenAIBackend

    monkeypatch.delenv("THINKROOM_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-consumed")
    with pytest.raises(ValueError, match="THINKROOM_OPENAI_API_KEY"):
        OpenAIBackend.from_env(timeout=1)

    monkeypatch.setenv("THINKROOM_OPENAI_API_KEY", "namespaced-key")
    backend = OpenAIBackend.from_env(timeout=1)
    assert backend.api_key == "namespaced-key"


def test_persisted_budget_includes_attempt_audit_metadata(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"), max_persisted_bytes=20_000)
    repo.open()
    try:
        request = ResearchRequest(
            question="Should we choose this important option?", branch_count=2
        )
        job, _ = repo.create_job(request, "hash", deadline=datetime.now(UTC) + timedelta(minutes=5))
        before = repo._persisted_bytes_locked(job)
        with pytest.raises(RuntimeError, match="ARTIFACT_LIMIT_EXCEEDED"):
            repo.claim_next_job(2, "correlation", "x" * 12_000, "model")
        assert repo._persisted_bytes_locked(job) == before
        row = repo.get_job(job)
        assert row is not None and row["state"] == "queued"
        assert repo.attempts(job) == []
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_container_supervisor_propagates_child_exit_code(monkeypatch):
    import importlib.util

    entrypoint = Path(__file__).parents[1] / "scripts" / "container_entrypoint.py"
    spec = importlib.util.spec_from_file_location("container_entrypoint_exit_test", entrypoint)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeServer:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    class FakeProcess:
        returncode = 17

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            return None

    async def fake_start_server(*_args, **_kwargs):
        return FakeServer()

    monkeypatch.setattr(module, "default_gateway_ipv4", lambda: "172.18.0.1")
    monkeypatch.setattr(module.asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    assert await module.main() == 17


def test_prime_backend_bounds_every_configured_argv_value():
    PrimeAgentBackend("x" * 4096, "界" * 256, "model", "界" * 64)
    cases = [
        ("executable", "x" * 4097, "provider", "model", "off"),
        ("provider", "prime-agent", "界" * 257, "model", "off"),
        ("thinking", "prime-agent", "provider", "model", "界" * 65),
    ]
    for label, executable, provider, model, thinking in cases:
        with pytest.raises(ValueError, match=label):
            PrimeAgentBackend(executable, provider, model, thinking)

    class StringSubclass(str):
        pass

    with pytest.raises(ValueError, match="provider"):
        PrimeAgentBackend("prime-agent", StringSubclass("provider"), "model", "off")
    with pytest.raises(ValueError, match="thinking"):
        PrimeAgentBackend("prime-agent", "provider", "model", "off\x00unsafe")


@pytest.mark.asyncio
async def test_prime_backend_rejects_prompt_above_safe_argv_limit_before_spawn():
    backend = PrimeAgentBackend("/definitely/not/executed", "", "", "off")
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="Should we choose this important option?",
            context="界" * 30000,
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    with pytest.raises(BackendError) as caught:
        await backend.invoke(request)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_prime_backend_rejects_rpc_prompt_above_safe_limit(monkeypatch):
    from thinkroom import backends as backend_module

    backend = PrimeAgentBackend("x" * 4096, "provider", "model", "off")
    monkeypatch.setattr(
        backend_module,
        "provider_payload",
        lambda request: {"instruction": "Return JSON.", "context": "界" * 22000},
    )
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="Should we choose this important option?",
            context="context",
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    with pytest.raises(BackendError) as caught:
        await backend.invoke(request)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_prime_backend_uses_supported_flags_schema_and_stream_limit(tmp_path, monkeypatch):
    capture = tmp_path / "args.json"
    executable = tmp_path / "prime-fake"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "command = json.loads(sys.stdin.readline())\n"
        "open(os.environ['CAPTURE'], 'w').write(json.dumps({'argv': sys.argv, 'command': command}))\n"
        "print(json.dumps({'id': command.get('id'), 'type': 'response', 'command': 'prompt', "
        "'success': True}), flush=True)\n"
        "for _ in range(20):\n"
        "    print(json.dumps({'type': 'message_update', 'delta': 'x' * 1000}), flush=True)\n"
        "result = json.dumps({'schema_version':1,'decision':'d','scope':'s','constraints':['c'],"
        "'success_criteria':['s'],'ambiguities':['a'],'research_questions':['q']})\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'custom', 'customType': 'agent_message', 'content': 'child done', "
        "'details': {'message': 'child done', 'fromRelationship': 'child', "
        "'from': {'sessionName': 'thinkroom-frame-worker'}}},"
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': result}], "
        "'stopReason': 'stop'}]}), flush=True)\n"
        "sys.stdin.read()\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("CAPTURE", str(capture))
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question",
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    backend = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000)
    result = await backend.invoke(request)
    assert result["decision"] == "d"
    captured = json.loads(capture.read_text())
    args = captured["argv"]
    assert "--json" not in args and "--max-output-tokens" not in args
    assert "--print" not in args
    assert "--no-tools" not in args
    assert "--no-session" not in args
    assert "--no-skills" not in args
    assert args[args.index("--mode") + 1] == "rpc"
    assert args[args.index("--tools") + 1] == "ipython"
    assert "--session-dir" in args
    assert args[args.index("--cwd") + 1] == args[args.index("--session-dir") + 1]
    assert backend.model == "configured-default"
    assert "--model" not in args
    prompt = captured["command"]["message"]
    assert '"title": "FrameOutputV1"' in prompt
    assert "7000 UTF-8 bytes" in prompt
    assert "thinkroom-frame-worker" in prompt
    assert "agent_message" in prompt
    limited = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=32)
    with pytest.raises(BackendError) as caught:
        await limited.invoke(request)
    assert caught.value.code == "OUTPUT_LIMIT_EXCEEDED"
    token_limited = PrimeAgentBackend(
        str(executable), "", "", "off", max_output_tokens=32, max_response_bytes=4096
    )
    with pytest.raises(BackendError) as token_caught:
        await token_limited.invoke(request)
    assert token_caught.value.code == "OUTPUT_LIMIT_EXCEEDED"

    invalid_utf8 = tmp_path / "prime-invalid-utf8"
    invalid_utf8.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b'\\xff')\n"
    )
    invalid_utf8.chmod(0o755)
    invalid_backend = PrimeAgentBackend(str(invalid_utf8), "", "", "off", max_response_bytes=4096)
    with pytest.raises(BackendError) as invalid_caught:
        await invalid_backend.invoke(request)
    assert invalid_caught.value.code == "MALFORMED_PROVIDER_OUTPUT"


@pytest.mark.asyncio
async def test_prime_backend_discards_stderr_without_overriding_valid_result(tmp_path):
    executable = tmp_path / "prime-stderr-overflow"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "command = json.loads(sys.stdin.readline())\n"
        "sys.stderr.write('e' * 10001)\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'id': command.get('id'), 'type': 'response', 'command': 'prompt', "
        "'success': True}), flush=True)\n"
        "result = json.dumps({'schema_version':1,'decision':'d','scope':'s','constraints':['c'],"
        "'success_criteria':['s'],'ambiguities':['a'],'research_questions':['q']})\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'custom', 'customType': 'agent_message', 'content': 'child done', "
        "'details': {'message': 'child done', 'fromRelationship': 'child', "
        "'from': {'sessionName': 'thinkroom-frame-worker'}}},"
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': result}], "
        "'stopReason': 'stop'}]}), flush=True)\n"
    )
    executable.chmod(0o755)
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question",
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    backend = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000)
    result = await backend.invoke(request)
    assert result["decision"] == "d"


@pytest.mark.asyncio
async def test_prime_backend_rejects_json_without_matching_rlm_child_message(tmp_path):
    executable = tmp_path / "prime-without-child"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "command = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id': command.get('id'), 'type': 'response', 'command': 'prompt', "
        "'success': True}), flush=True)\n"
        "result = json.dumps({'schema_version':1,'decision':'d','scope':'s','constraints':['c'],"
        "'success_criteria':['s'],'ambiguities':['a'],'research_questions':['q']})\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'custom', 'customType': 'agent_message', 'content': 'wrong child', "
        "'details': {'message': 'wrong child', 'fromRelationship': 'child', "
        "'from': {'sessionName': 'thinkroom-other-worker'}}},"
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': result}], "
        "'stopReason': 'stop'}]}), flush=True)\n"
    )
    executable.chmod(0o755)
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question",
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    backend = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000)
    with pytest.raises(BackendError) as caught:
        await backend.invoke(request)
    assert caught.value.code == "MALFORMED_PROVIDER_OUTPUT"


@pytest.mark.asyncio
async def test_prime_backend_uses_strict_lf_jsonl_and_only_accepts_answer_after_child(tmp_path):
    executable = tmp_path / "prime-unicode-jsonl"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "command = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id': command.get('id'), 'type': 'response', 'command': 'prompt', "
        "'success': True}), flush=True)\n"
        "early = json.dumps({'schema_version':1,'decision':'early','scope':'s',"
        "'constraints':['c'],'success_criteria':['s'],'ambiguities':['a'],"
        "'research_questions':['q']}, ensure_ascii=False)\n"
        "final = json.dumps({'schema_version':1,'decision':'after\\u2028child','scope':'s',"
        "'constraints':['c'],'success_criteria':['s'],'ambiguities':['a'],"
        "'research_questions':['q']}, ensure_ascii=False)\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': early}], "
        "'stopReason': 'stop'},"
        "{'role': 'custom', 'customType': 'agent_message', 'content': 'child done', "
        "'details': {'message': 'child done', 'fromRelationship': 'child', "
        "'from': {'sessionName': 'thinkroom-frame-worker'}}},"
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': final}], "
        "'stopReason': 'stop'}]}, ensure_ascii=False), flush=True)\n"
    )
    executable.chmod(0o755)
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question",
            domain="generic",
            guidance="g",
            safety="s",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        correlation_id="c",
    )
    backend = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000)
    result = await backend.invoke(request)
    assert result["decision"] == "after\u2028child"


@pytest.mark.asyncio
async def test_openai_non_string_content_is_malformed_output():
    class NonStringContentHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"choices": [{"message": {"content": {"not": "a string"}}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), NonStringContentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        backend = OpenAIBackend(
            f"http://127.0.0.1:{server.server_port}", "test-key", "test-model", timeout=2
        )
        request = BackendRequestV1(
            phase="frame",
            job_id="j",
            attempt_id="a",
            prompt_version="v",
            input=FrameInputV1(
                question="A sufficiently important question",
                domain="generic",
                guidance="g",
                safety="s",
            ),
            expected_output_schema="FrameOutputV1",
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            correlation_id="c",
        )
        with pytest.raises(BackendError) as caught:
            await backend.invoke(request)
        assert caught.value.code == "MALFORMED_PROVIDER_OUTPUT"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_container_proxy_allows_only_loopback_and_default_gateway():
    import importlib.util

    entrypoint = Path(__file__).parents[1] / "scripts" / "container_entrypoint.py"
    spec = importlib.util.spec_from_file_location("container_entrypoint_test", entrypoint)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.client_allowed(("127.0.0.1", 1234), "172.18.0.1")
    assert module.client_allowed(("172.18.0.1", 1234), "172.18.0.1")
    assert not module.client_allowed(("172.18.0.22", 1234), "172.18.0.1")


def test_bind_host_must_be_a_literal_loopback_address():
    for host in ("localhost", "public-alias.example", "0.0.0.0", "::"):
        with pytest.raises(ValueError):
            Settings.from_env(host=host)
    assert Settings.from_env(host="127.0.0.1").host == "127.0.0.1"
    assert Settings.from_env(host="::1").host == "::1"


@pytest.mark.asyncio
async def test_api_rejects_oversized_context_and_unknown_body_before_admission(tmp_path):
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        max_context_bytes=16384,
    )
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            for payload in (
                {
                    "question": "Should we choose this important option?",
                    "context": "界" * 6000,
                },
                {
                    "question": "Should we choose this important option?",
                    "unknown": "界" * 6000,
                },
            ):
                response = await client.post("/api/v1/research", json=payload)
                assert response.status_code == 413
                assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
        assert service.repo.list_jobs(1, None) == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_embedded_submit_rejects_oversized_context_before_job_creation(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        max_context_bytes=16384,
    )
    engine = ResearchEngine(repo, ScriptedBackend(calls=[]), settings)
    try:
        with pytest.raises(BackendError) as caught:
            await engine.submit(
                ResearchRequest(
                    question="Should we choose this important option?", context="界" * 6000
                )
            )
        assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
        assert repo.list_jobs(1, None) == []
        assert engine._workers == []
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_rest_location_and_terminal_idempotent_replay(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            payload = {"question": "Should we choose this important option?"}
            headers = {"Idempotency-Key": "replay-key"}
            created = await client.post("/api/v1/research", json=payload, headers=headers)
            assert created.status_code == 202
            assert created.headers["location"].endswith(created.json()["job_id"])
            job_id = created.json()["job_id"]
            for _ in range(100):
                detail = (await client.get(f"/api/v1/research/{job_id}")).json()
                if detail["state"] == "succeeded":
                    break
                await asyncio.sleep(0.02)
            replay = await client.post("/api/v1/research", json=payload, headers=headers)
            assert replay.status_code == 200
            assert replay.headers["location"].endswith(job_id)
    finally:
        await service.stop()


def test_container_context_is_allowlisted_nonroot_and_healthchecked():
    from pathlib import Path

    root = Path(__file__).parents[1]
    ignore = (root / ".dockerignore").read_text().splitlines()
    dockerfile = (root / "Dockerfile").read_text()
    assert ignore[0] == "**"
    assert "!uv.lock" in ignore
    assert "!.env" not in ignore and "!.git" not in ignore
    final_allow = max(index for index, item in enumerate(ignore) if item.startswith("!"))
    for excluded in ("**/.env", "**/.env.*", "**/__pycache__/", "**/*.pyc", "**/*.pyo"):
        assert ignore.index(excluded) > final_allow
    assert "ARG PYTHON_IMAGE=python:3.12-slim@sha256:" in dockerfile
    assert "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3@sha256:" in dockerfile
    assert "COPY --from=uv /uv /usr/local/bin/uv" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "--write-manifest /app/runtime-lock-manifest.json" in dockerfile
    assert "COPY --from=builder /app/runtime-lock-manifest.json" in dockerfile
    assert (
        "--manifest /app/runtime-lock-manifest.json"
        in (root / "scripts" / "smoke_docker.ps1").read_text()
    )
    assert "pip install --no-cache-dir ." not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "THINKROOM_DATABASE_URL=sqlite+aiosqlite:////data/thinkroom.db" in dockerfile
    windows_smoke = (root / "scripts" / "smoke_docker.ps1").read_text()
    linux_smoke = (root / "scripts" / "smoke_docker.py").read_text()
    for smoke in (windows_smoke, linux_smoke):
        assert "uid=10001,gid=10001,mode=0700" in smoke
    assert "headers={'Host':'127.0.0.1'}" in windows_smoke


def test_runtime_lock_manifest_rejects_missing_and_duplicate_distributions(tmp_path):
    import importlib.util
    from collections import Counter
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts" / "verify_locked_runtime.py"
    spec = importlib.util.spec_from_file_location("verify_locked_runtime", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    installed = module.installed_counter()
    write_manifest = module.write_manifest
    read_manifest = module.read_manifest
    verify_manifest = module.verify_manifest
    manifest = tmp_path / "runtime-lock-manifest.json"
    write_manifest(manifest, installed)
    expected = read_manifest(manifest)
    assert expected == installed
    starlette = next(key for key in installed if key[0] == "starlette")
    with pytest.raises(RuntimeError, match="missing_distributions"):
        verify_manifest(installed - Counter({starlette: 1}), expected)
    with pytest.raises(RuntimeError, match="unexpected_distributions"):
        verify_manifest(installed + Counter({starlette: 1}), expected)
    verify_lock_membership = module.verify_lock_membership
    with pytest.raises(RuntimeError, match="ambiguous_distributions"):
        verify_lock_membership(
            Counter({("example", "1.0"): 1, ("example", "2.0"): 1}),
            {"example": {"1.0", "2.0"}},
            set(),
        )


@pytest.mark.asyncio
async def test_global_404_and_405_use_typed_error_body(tmp_path):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        missing = await client.get("/missing")
        method = await client.put("/api/v1/version")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "NOT_FOUND",
        "message": "Not Found",
        "details": {},
    }
    assert method.status_code == 405
    assert method.json() == {
        "code": "METHOD_NOT_ALLOWED",
        "message": "Method Not Allowed",
        "details": {},
    }


class OneOversizedRolloutErrorBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.failed = False

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "rollout" and not self.failed:
            self.failed = True
            raise RuntimeError("x" * 10_000)
        return await super().invoke(request)


@pytest.mark.asyncio
async def test_oversized_rollout_error_preserves_partial_branch_success(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    engine = ResearchEngine(
        repo,
        OneOversizedRolloutErrorBackend(),
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        for _ in range(300):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "succeeded"
        failed = [
            artifact
            for artifact in repo.get_artifacts(job, row["attempt_id"])
            if artifact["kind"] == "branch" and artifact["state"] == "failed"
        ]
        assert len(failed) == 1
        assert 1 <= len(failed[0]["error"].encode("utf-8")) <= 4000
        assert any(
            artifact["kind"] == "critique"
            for artifact in repo.get_artifacts(job, row["attempt_id"])
        )
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_artifact_limit_failure_cannot_be_downgraded_to_branch_failure(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    original = repo.put_artifact
    raised = False

    def fail_one_branch(*args, **kwargs):
        nonlocal raised
        kind = args[2] if len(args) > 2 else kwargs.get("kind")
        if kind == "branch" and not raised:
            raised = True
            raise RuntimeError("ARTIFACT_LIMIT_EXCEEDED")
        return original(*args, **kwargs)

    repo.put_artifact = fail_one_branch
    engine = ResearchEngine(
        repo,
        ScriptedBackend(calls=[]),
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    try:
        job, _ = await engine.submit(
            ResearchRequest(question="Should we choose this important option?", branch_count=2)
        )
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "failed"
        assert json.loads(row["terminal_error"])["code"] == "ARTIFACT_LIMIT_EXCEEDED"
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_oversized_rollout_error_respects_total_persisted_budget_and_typed_detail(tmp_path):
    import thinkroom.repository as repository_module

    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        max_persisted_bytes_per_job=1_000_000,
    )
    service = ThinkroomService(settings)
    service.repo.open()
    try:
        request = ResearchRequest(
            question="Should we choose this important option?", branch_count=2
        )
        job, _ = service.repo.create_job(
            request, "hash", deadline=datetime.now(UTC) + timedelta(minutes=5)
        )
        claimed = service.repo.claim_next_job(2, "test", "scripted", "scripted-v1")
        assert claimed and claimed[0] == job
        aid = claimed[1]
        remaining = settings.max_persisted_bytes_per_job - service.repo._persisted_bytes_locked(job)
        service.repo.put_artifact(
            job,
            aid,
            "padding",
            {"filler": "x" * (remaining - repository_module._TERMINAL_SETTLEMENT_RESERVE - 3000)},
        )
        with pytest.raises(RuntimeError, match="ARTIFACT_LIMIT_EXCEEDED"):
            service.repo.put_artifact(job, aid, "branch", {}, "branch-test", "failed", "界" * 10000)
        assert service.repo.settle_terminal(
            job,
            JobState.FAILED,
            aid,
            "failed",
            "persisted byte limit",
            "test",
            "ARTIFACT_LIMIT_EXCEEDED",
            "job persisted-byte limit exceeded",
        )
        assert service.repo._persisted_bytes_locked(job) <= settings.max_persisted_bytes_per_job
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(f"/api/v1/research/{job}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["terminal_error"]["code"] == "ARTIFACT_LIMIT_EXCEEDED"
        assert detail["branches"] == []
    finally:
        service.repo.close()


def test_research_detail_openapi_is_fully_typed():
    schema = create_app().openapi()["components"]["schemas"]
    detail = schema["ResearchDetail"]["properties"]
    assert detail["branches"]["items"]["$ref"].endswith("/ResearchBranchV1")
    assert detail["perspectives"]["items"]["$ref"].endswith("/PerspectiveV1")
    assert detail["transitions"]["items"]["$ref"].endswith("/TransitionRecordV1")
    assert detail["attempts"]["items"]["$ref"].endswith("/AttemptRecordV1")
    terminal_refs = [part.get("$ref", "") for part in detail["terminal_error"]["anyOf"]]
    assert any(ref.endswith("/TerminalErrorV1") for ref in terminal_refs)


def test_cli_serve_injects_explicit_settings_without_import_time_app(monkeypatch):
    from typer.testing import CliRunner

    import thinkroom.api as api_module
    import thinkroom.cli as cli_module

    captured = {}

    def fake_create_app(service):
        captured["settings"] = service.settings
        return object()

    def fake_run(application, *, host, port):
        captured.update(application=application, host=host, port=port)

    monkeypatch.setenv("THINKROOM_HOST", "0.0.0.0")
    monkeypatch.setattr(cli_module, "create_app", fake_create_app)
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)
    result = CliRunner().invoke(cli_module.app, ["serve", "--host", "127.0.0.1", "--port", "18789"])
    assert result.exit_code == 0
    assert captured["settings"].host == captured["host"] == "127.0.0.1"
    assert captured["settings"].port == captured["port"] == 18789
    assert not hasattr(api_module, "app")


def test_skills_cli_uses_documented_target_option():
    from typer.main import get_command

    from thinkroom.cli import app

    root = get_command(app)
    root_commands = getattr(root, "commands", None)
    assert isinstance(root_commands, dict)
    skills = root_commands["skills"]
    skill_commands = getattr(skills, "commands", None)
    assert isinstance(skill_commands, dict)
    for command_name in ("install", "status", "uninstall"):
        command = skill_commands[command_name]
        target = next(parameter for parameter in command.params if parameter.name == "target")
        assert "--target" in target.opts


@pytest.mark.asyncio
async def test_web_ui_never_injects_dynamic_html(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/")
        assert response.status_code == 200
        assert "innerHTML" not in response.text
        assert "textContent" in response.text
        assert "document.createTextNode" in response.text
        assert "object-src 'none'" in response.headers["content-security-policy"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_official_mcp_client_interoperability():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(command=sys.executable, args=["-m", "thinkroom", "mcp"])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} == {
        "thinkroom_research",
        "thinkroom_get_research",
        "thinkroom_list_research",
        "thinkroom_cancel_research",
    }


@pytest.mark.asyncio
async def test_mcp_client_maps_oversized_request_to_invalid_argument():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    class OversizedHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps(
                {
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": "request body exceeds byte limit",
                    "details": {"max_bytes": 10000},
                }
            ).encode()
            self.send_response(413)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), OversizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "thinkroom", "mcp"],
            env={
                **os.environ,
                "THINKROOM_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            },
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("thinkroom_research", {"question": "x" * 10001})
        assert result.isError is True
        text = getattr(result.content[0], "text", "")
        assert '"code": "INVALID_ARGUMENT"' in text
        assert '"service_code": "PAYLOAD_TOO_LARGE"' in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ForgedEvidenceBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        result = await super().invoke(request)
        if request.phase == "rollout":
            result["supporting_evidence"][0].update(
                {
                    "verification_status": "verified",
                    "source_reference": "https://provider.example/forged",
                    "verification_basis": "the model says it checked this",
                }
            )
        return result


@pytest.mark.asyncio
async def test_provider_verified_evidence_is_normalized_and_audited(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = ForgedEvidenceBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=2)
    )
    for _ in range(200):
        if repo.get_job(job)["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert repo.get_job(job)["state"] == "succeeded"
    branch = next(
        a for a in repo.get_artifacts(job, repo.get_job(job)["attempt_id"]) if a["kind"] == "branch"
    )
    evidence = json.loads(branch["payload"])["supporting_evidence"][0]
    assert evidence["verification_status"] == "unverified"
    assert evidence["verification_warning"]
    assert "provider verification assertion rejected" in evidence["verification_basis"]
    await engine.stop()
    repo.close()


def test_cancel_requested_jobs_are_terminal_during_restart(tmp_path):
    path = str(tmp_path / "db.sqlite")
    request = ResearchRequest(question="Should we choose this important option?")
    repo = SQLiteRepository(path)
    repo.open()
    active, _ = repo.create_job(request, "active", max_queued=10)
    claimed = repo.claim_next_job(2, "worker", "scripted", "scripted-v1")
    assert claimed and claimed[0] == active
    queued, _ = repo.create_job(request, "queued", max_queued=10)
    # Model a crash after cancellation was requested but before either request settled.
    repo._db().execute(
        "UPDATE research_jobs SET cancellation_requested=1 WHERE job_id IN (?,?)", (queued, active)
    )
    repo._db().commit()
    repo.close()
    restarted = SQLiteRepository(path)
    restarted.open()
    restarted.recover_startup(2)
    for job in (queued, active):
        row = restarted.get_job(job)
        assert row["state"] == "cancelled"
        assert json.loads(row["terminal_error"])["code"] == "CANCELLED"
        assert restarted.claim_next_job(2, "worker") is None
        assert any(t["to_state"] == "cancelled" for t in restarted.transitions(job))
    restarted.close()


@pytest.mark.asyncio
async def test_serialized_provider_input_limit_is_stable_and_pre_invocation(tmp_path):
    class NeverCalled(ScriptedBackend):
        async def invoke(self, request):
            raise AssertionError("provider invoked despite input limit")

    path = tmp_path / "db.sqlite"
    repo = SQLiteRepository(str(path))
    repo.open()
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{path}", max_context_bytes=16384
    )
    engine = ResearchEngine(repo, NeverCalled(), settings)
    job, _ = repo.create_job(
        ResearchRequest(question="Should we choose this important option?"), "h", max_queued=2
    )
    claimed = repo.claim_next_job(2, "worker")
    assert claimed and claimed[0] == job
    with pytest.raises(BackendError, match="serialized provider input exceeds") as caught:
        await engine._phase(
            "frame",
            job,
            claimed[1],
            None,
            {
                "question": "Should we choose this important option?",
                "context": "x" * 100000,
                "domain": "generic",
                "guidance": "g",
                "safety": "s",
            },
            datetime.now(UTC) + timedelta(minutes=1),
            "corr",
            "v",
        )
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert repo._db().execute("SELECT 1").fetchone() is not None
    repo.close()


def test_nested_phase_payloads_are_typed_and_bounded():
    with pytest.raises(ValidationError):
        CritiqueInputV1(
            successful_branches=[{"branch_id": "b", "output": {"unexpected": 1}}],
            successful_branch_ids=["b"],
            failed_branches=[],
        )
    evidence = EvidenceV1(
        id="e", statement="s", relationship="supports", verification_status="unverified"
    )
    branch = BranchOutputV1(
        summary="s",
        claims=[{"statement": "c", "evidence_ids": ["e"]}],
        supporting_evidence=[evidence],
        contradicting_evidence=[],
        assumptions=["a"],
        uncertainties=["u"],
        falsifiers=["f"],
        next_checks=["n"],
    )
    with pytest.raises(ValidationError):
        CritiqueInputV1(
            successful_branches=[{"branch_id": "b", "output": branch.model_dump()}],
            successful_branch_ids=["b"],
            failed_branches=[],
            validation_feedback="x" * 4001,
        )


@pytest.mark.asyncio
async def test_prime_cancellation_terminates_before_forced_kill(tmp_path):
    marker = tmp_path / "signals.txt"
    executable = tmp_path / "ignore-term"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import signal,time\n"
        f"signal.signal(signal.SIGTERM, lambda *_: open({str(marker)!r}, 'a').write('terminate\\n'))\n"
        "print('\\n', flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    executable.chmod(0o755)
    request = BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question", domain="generic", guidance="g", safety="s"
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC),
        correlation_id="c",
    )
    backend = PrimeAgentBackend(str(executable), "", "", "off", timeout=5)
    task = asyncio.create_task(backend.invoke(request))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert marker.read_text().startswith("terminate")


def test_skills_install_uses_the_exact_validated_source_bytes(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    bundle = tmp_path / "bundle"
    shutil.copytree(skill_module.BUNDLE, bundle)
    source = bundle / "thinkroom-install" / "SKILL.md"
    original = source.read_bytes()
    real_read_bytes = Path.read_bytes
    swapped = False

    def swap_after_first_read(path):
        nonlocal swapped
        data = real_read_bytes(path)
        if Path(path) == source and not swapped:
            swapped = True
            with open(source, "wb") as handle:
                handle.write(b"unvalidated replacement\n")
        return data

    monkeypatch.setattr(skill_module, "BUNDLE", bundle)
    monkeypatch.setattr(Path, "read_bytes", swap_after_first_read)
    target = tmp_path / "target"
    skill_module.install(target)
    assert swapped
    assert (target / "thinkroom-install" / "SKILL.md").read_bytes() == original


def test_skills_install_rejects_receipt_owned_missing_payload(tmp_path):
    from thinkroom import skills as skill_module

    target = tmp_path / "target"
    skill_module.install(target)
    missing = target / "thinkroom-install" / "SKILL.md"
    missing.unlink()
    assert any(
        item["path"] == "thinkroom-install/SKILL.md" and item["classification"] == "DIVERGED"
        for item in skill_module.status(target)
    )
    with pytest.raises(ValueError, match="DIVERGED"):
        skill_module.install(target)
    assert not missing.exists()


def test_skills_add_never_overwrites_a_concurrent_filename_replacement(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    parent = tmp_path / "target"
    parent.mkdir()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    expected = skill_module._Snapshot(None, None)
    real_snapshot = skill_module._snapshot_at
    replaced = False

    def replace_after_absent_snapshot(fd, name):
        nonlocal replaced
        snapshot = real_snapshot(fd, name)
        if name == "SKILL.md" and snapshot.data is None and not replaced:
            replaced = True
            (parent / name).write_bytes(b"concurrent replacement")
        return snapshot

    monkeypatch.setattr(skill_module, "_snapshot_at", replace_after_absent_snapshot)
    try:
        with pytest.raises(ValueError, match="changed during mutation"):
            skill_module._atomic_write_at(parent_fd, "SKILL.md", b"managed payload", expected)
        assert (parent / "SKILL.md").read_bytes() == b"concurrent replacement"
    finally:
        os.close(parent_fd)


def test_skills_unlink_never_deletes_a_concurrent_filename_replacement(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    parent = tmp_path / "target"
    parent.mkdir()
    managed = parent / "SKILL.md"
    managed.write_bytes(b"managed payload")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    expected = skill_module._snapshot_at(parent_fd, "SKILL.md")
    real_snapshot = skill_module._snapshot_at
    replaced = False

    def replace_after_expected_snapshot(fd, name):
        nonlocal replaced
        snapshot = real_snapshot(fd, name)
        if name == "SKILL.md" and snapshot.identity == expected.identity and not replaced:
            replaced = True
            managed.unlink()
            managed.write_bytes(b"concurrent replacement")
        return snapshot

    monkeypatch.setattr(skill_module, "_snapshot_at", replace_after_expected_snapshot)
    try:
        with pytest.raises(ValueError, match="changed during mutation"):
            skill_module._unlink_at(parent_fd, "SKILL.md", expected)
        assert managed.read_bytes() == b"concurrent replacement"
    finally:
        os.close(parent_fd)


def test_skills_unlink_restores_quarantine_when_final_delete_fails(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    parent = tmp_path / "target"
    parent.mkdir()
    managed = parent / "SKILL.md"
    managed.write_bytes(b"managed payload")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    expected = skill_module._snapshot_at(parent_fd, "SKILL.md")
    real_unlink = skill_module.os.unlink

    def fail_quarantine_unlink(name, *args, **kwargs):
        if ".thinkroom-delete-" in str(name):
            raise OSError("forced quarantine delete failure")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(skill_module.os, "unlink", fail_quarantine_unlink)
    try:
        with pytest.raises(OSError, match="quarantine delete failure"):
            skill_module._unlink_at(parent_fd, "SKILL.md", expected)
        assert managed.read_bytes() == b"managed payload"
        assert not list(parent.glob(".*.thinkroom-delete-*"))
    finally:
        os.close(parent_fd)


def test_skills_unlink_uses_backup_if_validated_quarantine_changes(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    parent = tmp_path / "target"
    parent.mkdir()
    managed = parent / "SKILL.md"
    managed.write_bytes(b"managed payload")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    expected = skill_module._snapshot_at(parent_fd, "SKILL.md")
    real_unlink = skill_module.os.unlink

    def replace_quarantine_then_fail(name, *args, **kwargs):
        if ".thinkroom-delete-" not in str(name):
            return real_unlink(name, *args, **kwargs)
        real_unlink(name, *args, **kwargs)
        replacement_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(replacement_fd, b"changed quarantine")
        finally:
            os.close(replacement_fd)
        raise OSError("forced post-validation quarantine replacement")

    monkeypatch.setattr(skill_module.os, "unlink", replace_quarantine_then_fail)
    try:
        with pytest.raises(OSError, match="post-validation quarantine replacement"):
            skill_module._unlink_at(parent_fd, "SKILL.md", expected)
        assert managed.read_bytes() == b"managed payload"
        quarantines = list(parent.glob(".*.thinkroom-delete-*"))
        assert len(quarantines) == 1
        assert quarantines[0].read_bytes() == b"changed quarantine"
    finally:
        os.close(parent_fd)


def test_skills_install_rollback_preserves_replacement_after_successful_add(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    real_atomic_write = skill_module._atomic_write_at
    writes = 0
    replaced_path = target / "thinkroom-install" / "SKILL.md"

    def replace_then_fail(parent_fd, name, data, expected):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("forced later write failure")
        installed = real_atomic_write(parent_fd, name, data, expected)
        if writes == 1:
            replaced_path.unlink()
            replaced_path.write_bytes(b"concurrent replacement")
        return installed

    monkeypatch.setattr(skill_module, "_atomic_write_at", replace_then_fail)
    with pytest.raises(RuntimeError, match="failed to roll back Skills install") as exc_info:
        skill_module.install(target)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert replaced_path.read_bytes() == b"concurrent replacement"


def test_skills_uninstall_surfaces_rollback_failure(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    skill_module.install(target)
    real_unlink = skill_module._unlink_at
    unlinks = 0

    def fail_second_unlink(parent_fd, name, expected):
        nonlocal unlinks
        unlinks += 1
        if unlinks == 2:
            raise OSError("forced uninstall failure")
        return real_unlink(parent_fd, name, expected)

    def fail_restore(*_args, **_kwargs):
        raise ValueError("forced restore failure")

    monkeypatch.setattr(skill_module, "_unlink_at", fail_second_unlink)
    monkeypatch.setattr(skill_module, "_restore_at", fail_restore)
    with pytest.raises(RuntimeError, match="failed to roll back Skills uninstall") as exc_info:
        skill_module.uninstall(target)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_skills_atomic_write_cleanup_preserves_replaced_temporary_name(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    observed: list[str] = []
    real_publish = skill_module._link_fd_noreplace

    def inspect_before_publish(parent_fd, source_fd, name):
        observed.extend(os.listdir(parent_fd))
        return real_publish(parent_fd, source_fd, name)

    monkeypatch.setattr(skill_module, "_link_fd_noreplace", inspect_before_publish)
    skill_module.install(target)
    assert not any(".thinkroom-" in name for name in observed)
    assert not list(target.glob("**/.*.thinkroom-*"))


def test_skills_anonymous_staging_fails_closed_when_filesystem_lacks_tmpfile(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    real_open = skill_module.os.open

    def reject_tmpfile(path, flags, *args, **kwargs):
        if flags & skill_module._TMPFILE == skill_module._TMPFILE:
            raise OSError(95, "operation not supported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(skill_module.os, "open", reject_tmpfile)
    with pytest.raises(ValueError, match="anonymous Skills staging"):
        skill_module.install(target)
    assert not list(target.glob("**/SKILL.md"))
    assert not (target / ".thinkroom" / "skills-receipt-v1.json").exists()


def test_skills_atomic_write_failure_removes_temporary_payload(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    real_write = skill_module.os.write
    failed = False

    def fail_once(fd, data):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected temporary write failure")
        return real_write(fd, data)

    monkeypatch.setattr(skill_module.os, "write", fail_once)
    with pytest.raises(OSError, match="temporary write failure"):
        skill_module.install(target)
    assert failed
    assert not list(target.glob("**/.*.thinkroom-*"))
    assert not list(target.glob("**/SKILL.md"))
    assert not (target / ".thinkroom" / "skills-receipt-v1.json").exists()


def test_skills_parent_fsync_failure_rolls_back_published_payload(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    real_fsync = skill_module.os.fsync
    failed = False

    def fail_first_directory_fsync(fd):
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected parent fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(skill_module.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="parent fsync failure"):
        skill_module.install(target)
    assert failed
    assert not list(target.glob("**/SKILL.md"))
    assert not (target / ".thinkroom" / "skills-receipt-v1.json").exists()


def test_skills_uninstall_parent_fsync_failure_restores_exact_projection(tmp_path, monkeypatch):
    from thinkroom import skills as skill_module

    target = tmp_path / "skills"
    skill_module.install(target)
    real_fsync = skill_module.os.fsync
    failed = False

    def fail_first_directory_fsync(fd):
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected uninstall parent fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(skill_module.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="uninstall parent fsync failure"):
        skill_module.uninstall(target)
    assert failed
    assert (target / ".thinkroom" / "skills-receipt-v1.json").is_file()
    assert all(item["classification"] == "EXACT" for item in skill_module.status(target))
    assert not list(target.glob("**/.*.thinkroom-*"))


def test_skills_projection_rolls_back_write_and_delete_failures(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    from thinkroom import skills as skill_module

    real_write = skill_module._atomic_write_at
    failed = {"write": False, "skill_writes": 0}

    def fail_write(parent_fd, name, data, expected):
        if name == "SKILL.md":
            failed["skill_writes"] += 1
        if name == "SKILL.md" and failed["skill_writes"] == 2 and not failed["write"]:
            failed["write"] = True
            raise OSError("injected write failure")
        return real_write(parent_fd, name, data, expected)

    monkeypatch.setattr(skill_module, "_atomic_write_at", fail_write)
    with pytest.raises(OSError):
        skill_module.install(target)
    assert not (target / ".thinkroom" / "skills-receipt-v1.json").exists()
    assert not list(target.glob("**/SKILL.md"))
    monkeypatch.setattr(skill_module, "_atomic_write_at", real_write)
    skill_module.install(target)
    real_unlink = skill_module._unlink_at
    deleted = {"count": 0}

    def fail_unlink(parent_fd, name, expected):
        if name == "SKILL.md":
            deleted["count"] += 1
        if name == "SKILL.md" and deleted["count"] == 3:
            raise OSError("injected delete failure")
        return real_unlink(parent_fd, name, expected)

    monkeypatch.setattr(skill_module, "_unlink_at", fail_unlink)
    with pytest.raises(OSError):
        skill_module.uninstall(target)
    assert all(item["classification"] == "EXACT" for item in skill_module.status(target))


def test_sdk_typed_error_and_mcp_error_mapping(monkeypatch):
    from thinkroom.mcp import thinkroom_get_research
    from thinkroom.sdk import ThinkroomClient, ThinkroomError

    request = httpx.Request("GET", "http://test/api/v1/research/missing")
    response = httpx.Response(
        429,
        json={"code": "RESOURCE_EXHAUSTED", "message": "full", "details": {"limit": 1}},
        request=request,
    )
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: response)
    with pytest.raises(ThinkroomError) as caught:
        ThinkroomClient("http://test").get("missing")
    assert (caught.value.status, caught.value.code, caught.value.details["limit"]) == (
        429,
        "RESOURCE_EXHAUSTED",
        1,
    )
    with pytest.raises(Exception) as mcp_error:
        thinkroom_get_research("missing")
    assert "RESOURCE_EXHAUSTED" in str(mcp_error.value)


def test_sdk_and_cli_scrub_hostile_remote_errors(monkeypatch):
    from typer.testing import CliRunner

    from thinkroom.cli import app
    from thinkroom.sdk import ThinkroomClient, ThinkroomError

    secret = "sk-hostile-secret-value"
    response = httpx.Response(
        500,
        json={
            "code": "\x1b[31mHOSTILE_CODE",
            "message": f"Authorization: Bearer {secret}\r\n\x1b[2J",
            "details": {"password": secret, "free_text": secret},
        },
        request=httpx.Request("GET", "http://hostile/api/v1/research/job"),
    )
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: response)

    with pytest.raises(ThinkroomError) as caught:
        ThinkroomClient("http://hostile").get("job")
    assert caught.value.code == "HTTP_ERROR"
    assert caught.value.message == "Thinkroom request failed"
    assert caught.value.details == {}
    assert str(caught.value) == "HTTP_ERROR: Thinkroom request failed"

    result = CliRunner().invoke(app, ["get", "job", "--endpoint", "http://hostile"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "HTTP_ERROR: Thinkroom request failed" in result.stderr
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert "\x1b" not in combined and "\r" not in combined

    known_response = httpx.Response(
        429,
        json={
            "code": "RESOURCE_EXHAUSTED",
            "message": f"capacity {secret}\x1b[2J",
            "details": {"limit": 7, "free_text": secret},
        },
        request=httpx.Request("GET", "http://hostile/api/v1/research/job"),
    )
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: known_response)
    with pytest.raises(ThinkroomError) as known:
        ThinkroomClient("http://hostile").get("job")
    assert known.value.code == "RESOURCE_EXHAUSTED"
    assert known.value.message == "Thinkroom capacity exhausted"
    assert known.value.details == {"limit": 7}
    assert secret not in str(known.value)


@pytest.mark.asyncio
async def test_validation_error_matches_typed_contract_and_sdk_preserves_details(
    tmp_path, monkeypatch
):
    from thinkroom.schemas import ErrorBody
    from thinkroom.sdk import ThinkroomClient, ThinkroomError

    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    await service.start()
    try:
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post("/api/v1/research", json={"question": "x"})
            schema = (await client.get("/openapi.json")).json()
        assert response.status_code == 422
        body = ErrorBody.model_validate(response.json())
        assert body.code == "INVALID_ARGUMENT"
        assert body.details["errors"]
        response_schema = schema["paths"]["/api/v1/research"]["post"]["responses"]["422"][
            "content"
        ]["application/json"]["schema"]
        assert response_schema["$ref"].endswith("/ErrorBody")
        response_400 = schema["paths"]["/api/v1/research"]["post"]["responses"]["400"]["content"][
            "application/json"
        ]["schema"]
        assert response_400["$ref"].endswith("/ErrorBody")

        remote_response = httpx.Response(
            422,
            json=response.json(),
            request=httpx.Request("POST", "http://test/api/v1/research"),
        )
        monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: remote_response)
        with pytest.raises(ThinkroomError) as caught:
            ThinkroomClient("http://test").research("x")
        assert caught.value.details["errors"] == [
            {
                "type": body.details["errors"][0]["type"],
                "loc": body.details["errors"][0]["loc"],
                "msg": "input validation failed",
            }
        ]
        assert "input" not in caught.value.details["errors"][0]
        assert "ctx" not in caught.value.details["errors"][0]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_attempt_identity_logging_and_cancel_openapi(tmp_path):
    settings = Settings.from_env(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", log_level="DEBUG"
    )
    service = ThinkroomService(settings)
    await service.start()
    try:
        assert service.repo._db().execute("PRAGMA table_info(attempts)").fetchall()[-2][1] in {
            "backend",
            "model",
        }
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            schema = (await client.get("/openapi.json")).json()
            responses = schema["paths"]["/api/v1/research/{job_id}"]["delete"]["responses"]
            assert "200" in responses and "202" in responses
        job, _ = await service.engine.submit(
            ResearchRequest(question="Should we choose this important option?")
        )
        for _ in range(200):
            if service.repo.get_job(job)["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        row = (
            service.repo._db()
            .execute("SELECT backend,model FROM attempts WHERE job_id=?", (job,))
            .fetchone()
        )
        assert row["backend"] == "scripted" and row["model"] == "scripted-v1"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            detail = (await client.get(f"/api/v1/research/{job}")).json()
        assert detail["attempts"] == [
            {
                "attempt_id": detail["attempt_id"],
                "job_id": job,
                "number": 1,
                "state": "terminal",
                "started_at": detail["attempts"][0]["started_at"],
                "ended_at": detail["attempts"][0]["ended_at"],
                "outcome": "succeeded",
                "recovery_reason": None,
                "backend": "scripted",
                "model": "scripted-v1",
            }
        ]
        assert detail["attempts"][0]["ended_at"] is not None
        assert detail["critique_id"] == detail["synthesis"]["consumed_critique_id"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_prime_default_model_has_typed_attempt_provenance(tmp_path):
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    service = ThinkroomService(settings)
    service.repo.open()
    backend = PrimeAgentBackend("unused", "", "", "off")
    scripted = ScriptedBackend(calls=[])
    backend.invoke = scripted.invoke
    service.engine = ResearchEngine(service.repo, backend, settings)
    await service.engine.start()
    try:
        job, _ = await service.engine.submit(
            ResearchRequest(question="Should we choose this important option?")
        )
        for _ in range(200):
            if service.repo.get_job(job)["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(f"/api/v1/research/{job}")
        assert response.status_code == 200
        assert response.json()["attempts"][0]["model"] == "configured-default"
    finally:
        await service.stop()


def test_cancelled_attempt_cannot_persist_artifacts_or_succeed(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should we choose this important option?")
    job, _ = repo.create_job(
        request,
        "cancel-race",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    assert repo.request_cancel(job)

    with pytest.raises(RuntimeError, match="CANCELLED_OR_STALE_ATTEMPT"):
        repo.put_artifact(job, attempt_id, "frame", {"decision": "late"})
    with pytest.raises(RuntimeError, match="CANCELLED_OR_STALE_ATTEMPT"):
        repo.add_provider_call(
            {
                "job_id": job,
                "attempt_id": attempt_id,
                "phase": "frame",
                "branch_id": None,
                "prompt_version": "v1",
                "backend": "scripted",
                "model": "scripted-v1",
                "started_at": datetime.now(UTC).isoformat(),
                "retry_index": 0,
                "output_status": "started",
            }
        )
    assert not repo.settle_terminal(
        job,
        JobState.SUCCEEDED,
        attempt_id,
        "succeeded",
        "late success",
        "test",
    )
    row = repo.get_job(job)
    assert row and row["state"] == JobState.FRAMING.value
    assert row["cancellation_requested"] == 1
    assert repo.get_artifacts(job, attempt_id) == []
    repo.close()


def test_cancelled_attempt_cannot_finish_provider_call(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should stale provider provenance be rejected?")
    job, _ = repo.create_job(
        request,
        "cancel-provider-call-race",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job,
            "attempt_id": attempt_id,
            "phase": "frame",
            "branch_id": None,
            "prompt_version": "v1",
            "backend": "scripted",
            "model": "scripted-v1",
            "started_at": datetime.now(UTC).isoformat(),
            "retry_index": 0,
            "output_status": "started",
            "output_size": 0,
        }
    )
    assert repo.request_cancel(job)

    assert not repo.finish_provider_call(
        call_id,
        attempt_id,
        ended_at=datetime.now(UTC).isoformat(),
        output_status="validated",
        output_size=123,
    )
    row = repo._db().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert row is not None
    assert row["ended_at"] is None
    assert row["output_status"] == "started"
    assert row["output_size"] == 0
    repo.close()


def test_current_attempt_can_finish_provider_call(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should current provider provenance be admitted?")
    job, _ = repo.create_job(
        request,
        "current-provider-call",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job,
            "attempt_id": attempt_id,
            "phase": "frame",
            "started_at": datetime.now(UTC).isoformat(),
            "output_status": "started",
            "output_size": 0,
        }
    )

    ended_at = datetime.now(UTC).isoformat()
    assert repo.finish_provider_call(
        call_id,
        attempt_id,
        ended_at=ended_at,
        output_status="validated",
        output_size=123,
    )
    row = repo._db().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert row is not None
    assert row["ended_at"] == ended_at
    assert row["output_status"] == "validated"
    assert row["output_size"] == 123
    repo.close()


@pytest.mark.parametrize("invalidation", ["deadline", "recovery", "terminal"])
def test_invalidated_attempt_cannot_finish_provider_call(tmp_path, invalidation):
    repo = SQLiteRepository(str(tmp_path / f"{invalidation}.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should invalidated provider provenance be rejected?")
    job, _ = repo.create_job(
        request,
        f"{invalidation}-provider-call",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job,
            "attempt_id": attempt_id,
            "phase": "frame",
            "started_at": datetime.now(UTC).isoformat(),
            "output_status": "started",
            "output_size": 0,
        }
    )
    if invalidation == "deadline":
        repo.update_job(job, deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    elif invalidation == "recovery":
        repo.recover_startup(2)
        replacement = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
        assert replacement and replacement[1] != attempt_id
    else:
        assert repo.settle_terminal(
            job,
            JobState.FAILED,
            attempt_id,
            "failed",
            "terminal before late provider completion",
            "test",
        )

    assert not repo.finish_provider_call(
        call_id,
        attempt_id,
        ended_at=datetime.now(UTC).isoformat(),
        output_status="validated",
        output_size=123,
    )
    row = repo._db().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    assert row is not None
    assert row["ended_at"] is None
    assert row["output_status"] == "started"
    assert row["output_size"] == 0
    repo.close()


def test_provider_call_budget_rejection_still_allows_terminal_settlement(tmp_path):
    import thinkroom.repository as repository_module

    maximum = 1_000_000
    repo = SQLiteRepository(str(tmp_path / "budget.sqlite"), max_persisted_bytes=maximum)
    repo.open()
    request = ResearchRequest(question="Should the durable byte ledger preserve terminal liveness?")
    job, _ = repo.create_job(
        request,
        "terminal-liveness",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    call_id = repo.add_provider_call(
        {
            "job_id": job,
            "attempt_id": attempt_id,
            "phase": "frame",
            "started_at": datetime.now(UTC).isoformat(),
            "output_status": "started",
            "output_size": 0,
        }
    )

    db = repo._db()
    with repo.lock, db:
        artifact_id = db.execute(
            "INSERT INTO artifacts(job_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
            (job, attempt_id, "boundary-padding", ""),
        ).lastrowid
        current = repo._persisted_bytes_locked(job)
        padding = maximum - repository_module._TERMINAL_SETTLEMENT_RESERVE - current
        assert padding >= 0
        db.execute("UPDATE artifacts SET payload=? WHERE id=?", ("x" * padding, artifact_id))
    assert (
        repo._persisted_bytes_locked(job)
        == maximum - repository_module._TERMINAL_SETTLEMENT_RESERVE
    )

    with pytest.raises(RuntimeError, match="ARTIFACT_LIMIT_EXCEEDED"):
        repo.finish_provider_call(
            call_id,
            attempt_id,
            ended_at=datetime.now(UTC).isoformat(),
            output_status="validated",
            output_size=123,
        )
    assert repo.settle_terminal(
        job,
        JobState.FAILED,
        attempt_id,
        "failed",
        "provider-call byte limit",
        "test",
        "ARTIFACT_LIMIT_EXCEEDED",
        "job persisted-byte limit exceeded",
    )
    row = repo.get_job(job)
    assert row is not None and row["state"] == JobState.FAILED.value
    assert repo.attempts(job)[0]["state"] == "terminal"
    assert repo._persisted_bytes_locked(job) <= maximum
    repo.close()


def test_expired_attempt_cannot_persist_artifacts_or_succeed(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    request = ResearchRequest(question="Should we choose this important option?")
    job, _ = repo.create_job(
        request,
        "deadline-race",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    claimed = repo.claim_next_job(2, "test", "scripted", "scripted-v1")
    assert claimed and claimed[0] == job
    attempt_id = claimed[1]
    repo.update_job(job, deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())

    with pytest.raises(RuntimeError, match="CANCELLED_OR_STALE_ATTEMPT"):
        repo.put_artifact(job, attempt_id, "frame", {"decision": "late"})
    assert not repo.settle_terminal(
        job,
        JobState.SUCCEEDED,
        attempt_id,
        "succeeded",
        "late success",
        "test",
    )
    assert repo.get_artifacts(job, attempt_id) == []
    repo.close()


@pytest.mark.asyncio
async def test_api_rejects_non_loopback_host_header(tmp_path):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://attacker-controlled.example"
    ) as client:
        rejected = await client.get("/health/live")
    assert rejected.status_code == 400
    assert rejected.json() == {
        "code": "INVALID_HOST",
        "message": "Host header must be a literal loopback IP address",
        "details": {},
    }


def test_branch_evidence_lists_match_relationship_semantics():
    payload = {
        "summary": "summary",
        "claims": [{"statement": "claim", "evidence_ids": ["evidence-1"]}],
        "supporting_evidence": [
            {
                "id": "evidence-1",
                "statement": "contrary evidence",
                "relationship": "contradicts",
                "verification_status": "unverified",
            }
        ],
        "contradicting_evidence": [],
        "assumptions": ["assumption"],
        "uncertainties": ["uncertainty"],
        "falsifiers": ["falsifier"],
        "next_checks": ["next check"],
    }
    with pytest.raises(ValidationError, match="supporting evidence must support"):
        BranchOutputV1.model_validate(payload)


def test_docker_smokes_use_argument_safe_owned_resource_cleanup():
    root = Path(__file__).parents[1]
    powershell = (root / "scripts" / "smoke_docker.ps1").read_text()
    python = (root / "scripts" / "smoke_docker.py").read_text()

    assert "cmd.exe" not in powershell
    assert "Assert-OwnedResource" in powershell
    assert powershell.count("$OwnershipLabel") >= 4
    assert "ConvertFrom-Json" in powershell
    assert ".Config.Labels" in powershell
    assert ".Labels" in powershell
    assert "assert_owned_resource" in python
    assert python.count("OWNERSHIP_LABEL") >= 4
    assert 'label_path = ".Labels"' in python


@pytest.mark.asyncio
async def test_unexpected_backend_exception_message_is_not_returned(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, SecretFailureBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?")
    )
    for _ in range(200):
        row = repo.get_job(job)
        if row and row["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    row = repo.get_job(job)
    assert row and row["state"] == "failed"
    terminal_error = TerminalErrorV1.model_validate_json(row["terminal_error"])
    assert terminal_error.code == "INTERNAL_ERROR"
    assert terminal_error.message == "job failed"
    assert "secret-provider-value" not in row["terminal_error"]
    await engine.stop()
    repo.close()


@pytest.mark.asyncio
async def test_api_lifespan_always_releases_service_after_runtime_failure(tmp_path):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)
    with pytest.raises(RuntimeError, match="runtime failed"):
        async with app.router.lifespan_context(app):
            assert service.ready
            assert service.engine is not None
            assert service.lock.handle is not None
            raise RuntimeError("runtime failed")
    assert not service.ready
    assert service.engine is None
    assert service.lock.handle is None
    assert service.repo.db is None


class CanaryCodeException(RuntimeError):
    code = "credential-canary-must-not-persist"


class CanaryCodeBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise CanaryCodeException("provider failed")


@pytest.mark.asyncio
async def test_arbitrary_exception_code_is_scrubbed_from_durable_state(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, CanaryCodeBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?")
    )
    try:
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "failed"
        assert "credential-canary" not in (row["terminal_error"] or "")
        statuses = [
            item["output_status"]
            for item in repo._db().execute(
                "SELECT output_status FROM provider_calls WHERE job_id=?", (job,)
            )
        ]
        assert statuses
        assert all("credential-canary" not in (status or "") for status in statuses)
        assert statuses[-1] == "invalid"
    finally:
        await engine.stop()
        repo.close()


def test_service_lock_rejects_aliases_and_path_replacement(tmp_path, monkeypatch):
    from thinkroom import service as service_module

    target = tmp_path / "target.lock"
    target.write_text("target")
    alias = tmp_path / "alias.lock"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="lock"):
        ServiceLock(str(alias)).acquire()

    hardlink = tmp_path / "hardlink.lock"
    os.link(target, hardlink)
    with pytest.raises(ValueError, match="lock"):
        ServiceLock(str(hardlink)).acquire()

    replaceable = tmp_path / "replaceable.lock"
    lock = ServiceLock(str(replaceable))
    real_flock = service_module.fcntl.flock
    replaced = False

    def replace_during_flock(fd, operation):
        nonlocal replaced
        if operation & service_module.fcntl.LOCK_EX and not replaced:
            replaced = True
            replaceable.unlink()
            replaceable.write_text("replacement")
        return real_flock(fd, operation)

    monkeypatch.setattr(service_module.fcntl, "flock", replace_during_flock)
    try:
        with pytest.raises(ValueError, match="lock"):
            lock.acquire()
    finally:
        lock.release()
    assert replaced


@pytest.mark.asyncio
async def test_service_start_rolls_back_workers_if_retention_start_fails(tmp_path, monkeypatch):
    from thinkroom import service as service_module

    service = ThinkroomService(
        Settings.from_env(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", max_concurrency=3
        )
    )
    real_create_task = service_module.asyncio.create_task

    def fail_retention(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "_retention_loop":
            coro.close()
            raise RuntimeError("retention task creation failed")
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(service_module.asyncio, "create_task", fail_retention)
    engine = None
    try:
        with pytest.raises(RuntimeError, match="retention task creation failed"):
            await service.start()
        engine = service.engine
        assert not service.ready
        assert service.engine is None
        assert service.repo.db is None
        assert service.lock.handle is None
        assert engine is None or all(task.done() for task in engine._workers)
    finally:
        if service.engine is not None:
            await service.stop()


class CancellationSuppressingBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.started = asyncio.Event()

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        if request.phase == "frame":
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise BackendError("PROVIDER_ERROR", "backend suppressed cancellation") from None
        return await super().invoke(request)


@pytest.mark.asyncio
async def test_suppressed_backend_cancellation_still_settles_cancelled(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    backend = CancellationSuppressingBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?")
    )
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        assert await engine.cancel(job)
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "cancelled"
        assert TerminalErrorV1.model_validate_json(row["terminal_error"]).code == "CANCELLED"
    finally:
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_service_rejects_malformed_existing_sqlite_schema(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE provider_calls (id INTEGER PRIMARY KEY, job_id TEXT)")
    db.commit()
    db.close()
    service = ThinkroomService(Settings.from_env(database_url=f"sqlite+aiosqlite:///{path}"))
    try:
        with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
            await service.start()
        assert not service.ready
        assert service.repo.db is None
        assert service.lock.handle is None
    finally:
        if service.ready or service.engine is not None:
            await service.stop()


@pytest.mark.asyncio
async def test_api_sdk_and_mcp_scrub_unexpected_transport_errors(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    from thinkroom import mcp as mcp_module
    from thinkroom import sdk as sdk_module
    from thinkroom.sdk import ThinkroomClient, ThinkroomError

    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)

    @app.get("/unexpected-error")
    async def unexpected_error():
        raise RuntimeError("credential-canary-api")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/unexpected-error")
    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_ERROR",
        "message": "internal server error",
        "details": {},
    }
    assert "credential-canary" not in response.text

    def transport_failure(*args, **kwargs):
        raise httpx.ConnectError("credential-canary-sdk")

    monkeypatch.setattr(sdk_module.httpx, "request", transport_failure)
    with pytest.raises(ThinkroomError) as sdk_error:
        ThinkroomClient().get("job")
    assert sdk_error.value.code == "TRANSPORT_ERROR"
    assert "credential-canary" not in str(sdk_error.value)

    with pytest.raises(ToolError) as mcp_error:
        mcp_module._mcp_call(lambda: (_ for _ in ()).throw(RuntimeError("credential-canary-mcp")))
    assert "INTERNAL_ERROR" in str(mcp_error.value)
    assert "credential-canary" not in str(mcp_error.value)


def test_runtime_lock_requires_full_selected_transitive_closure():
    import importlib.util
    from collections import Counter

    root = Path(__file__).parents[1]
    script = root / "scripts" / "verify_locked_runtime.py"
    spec = importlib.util.spec_from_file_location("verify_locked_runtime_closure", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    locked, required = module.lock_contract(root / "uv.lock")
    assert "starlette" in required
    direct = {"aiosqlite", "fastapi", "httpx", "mcp", "pydantic", "typer", "uvicorn"}
    direct_only = Counter({(name, next(iter(locked[name]))): 1 for name in direct})
    with pytest.raises(RuntimeError, match="missing_required"):
        module.verify_lock_membership(direct_only, locked, required)


@pytest.mark.asyncio
async def test_retention_loop_recovers_after_transient_cleanup_failure(tmp_path, monkeypatch):
    from thinkroom import service as service_module

    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    service.repo.open()
    service.ready = True
    real_sleep = asyncio.sleep
    calls = 0
    recovered = asyncio.Event()

    async def fast_sleep(_: float):
        await real_sleep(0)

    def flaky_cleanup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient cleanup failure")
        recovered.set()
        return 0

    monkeypatch.setattr(service_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(service.repo, "cleanup_retention", flaky_cleanup)
    task = asyncio.create_task(service._retention_loop())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert calls >= 2
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        service.ready = False
        service.repo.close()


class ExactCanaryBackendErrorBackend(ScriptedBackend):
    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        raise BackendError("credential-canary-exact-code", "credential-canary-exact-message")


@pytest.mark.asyncio
async def test_exact_backend_error_is_normalized_at_provider_boundary(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    repo.open()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    engine = ResearchEngine(repo, ExactCanaryBackendErrorBackend(calls=[]), settings)
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?")
    )
    try:
        for _ in range(200):
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        row = repo.get_job(job)
        assert row and row["state"] == "failed"
        statuses = repo._db().execute(
            "SELECT output_status FROM provider_calls WHERE job_id=? ORDER BY id", (job,)
        )
        durable = (
            row["terminal_error"]
            + "\n"
            + "\n".join(str(item["output_status"]) for item in statuses.fetchall())
        )
        assert "credential-canary" not in durable
        assert TerminalErrorV1.model_validate_json(row["terminal_error"]).code == "INTERNAL_ERROR"
    finally:
        await engine.stop()
        repo.close()


def test_database_and_skills_reject_foreign_owned_nonsticky_ancestor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from thinkroom import service as service_module
    from thinkroom import skills as skills_module

    ancestor = tmp_path
    real_lstat = Path.lstat
    foreign_uid = os.geteuid() + 1

    def foreign_lstat(path: Path):
        value = real_lstat(path)
        if path == ancestor:
            return SimpleNamespace(
                st_mode=value.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH | stat.S_ISVTX),
                st_uid=foreign_uid,
            )
        return value

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(ValueError, match="owned by an untrusted principal"):
        service_module._assert_database_path_custody(ancestor / "db.sqlite")
    with pytest.raises(ValueError, match="owned by an untrusted principal"):
        skills_module._assert_protected_parent(foreign_lstat(ancestor))


@pytest.mark.asyncio
async def test_api_and_mcp_scrub_unexpected_typed_exception_text(tmp_path):
    from types import SimpleNamespace

    from mcp.server.fastmcp.exceptions import ToolError

    from thinkroom.mcp import _mcp_call
    from thinkroom.sdk import ThinkroomError

    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )

    async def reject_submit(*args, **kwargs):
        raise ValueError("credential-canary-api")

    service.engine = SimpleNamespace(submit=reject_submit)
    transport = httpx.ASGITransport(app=create_app(service), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/v1/research",
            json={"question": "Should we choose this important option?"},
        )
    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_ERROR",
        "message": "internal server error",
        "details": {},
    }

    def remote_canary():
        raise ThinkroomError(
            422,
            "credential-canary-mcp-code",
            "credential-canary-mcp-message",
            {"credential-canary-mcp-detail": "value"},
        )

    with pytest.raises(ToolError) as raised:
        _mcp_call(remote_canary)
    assert "credential-canary" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


class IndefinitelyCancellationSuppressingBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__(calls=[])
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def invoke(self, request: BackendRequestV1) -> dict[str, object]:
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            return await super().invoke(request)
        finally:
            self.closed = True


async def _wait_terminal(repo: SQLiteRepository, job: str, timeout: float = 0.5):
    async def poll():
        while True:
            row = repo.get_job(job)
            if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                return row
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_cancellation_settles_without_provider_cooperation(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "cancel.sqlite"))
    repo.open()
    backend = IndefinitelyCancellationSuppressingBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    job, _ = await engine.submit(ResearchRequest(question="Should this operation be cancelled?"))
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=1)
        assert await engine.cancel(job)
        row = await _wait_terminal(repo, job)
        assert row["state"] == "cancelled"
        assert backend.release.is_set() is False
    finally:
        backend.release.set()
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_deadline_settles_without_provider_cooperation(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "deadline.sqlite"))
    repo.open()
    backend = IndefinitelyCancellationSuppressingBackend()
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'deadline.sqlite'}")
    engine = ResearchEngine(repo, backend, settings)
    await engine.start()
    job, _ = await engine.submit(
        ResearchRequest(
            question="Should this operation reach a bounded deadline?",
            deadline=datetime.now(UTC) + timedelta(seconds=1),
        )
    )
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=0.5)
        row = await _wait_terminal(repo, job, timeout=2)
        assert row["state"] == "failed"
        assert (
            TerminalErrorV1.model_validate_json(row["terminal_error"]).code == "DEADLINE_EXCEEDED"
        )
        assert backend.release.is_set() is False
    finally:
        backend.release.set()
        await engine.stop()
        repo.close()


@pytest.mark.asyncio
async def test_schema_attestation_rejects_changed_default_and_unexpected_index(tmp_path):
    default_path = tmp_path / "default.sqlite"
    repo = SQLiteRepository(str(default_path))
    repo.open()
    repo.close()
    db = sqlite3.connect(default_path)
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_jobs'"
    ).fetchone()[0]
    changed = sql.replace(
        "cancellation_requested INTEGER NOT NULL DEFAULT 0",
        "cancellation_requested INTEGER NOT NULL DEFAULT 1",
    )
    assert changed != sql
    db.execute("PRAGMA writable_schema=ON")
    db.execute(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='research_jobs'",
        (changed,),
    )
    db.execute("PRAGMA schema_version = 1001")
    db.commit()
    db.close()
    malformed = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{default_path}")
    )
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        await malformed.start()

    index_path = tmp_path / "index.sqlite"
    repo = SQLiteRepository(str(index_path))
    repo.open()
    repo.close()
    db = sqlite3.connect(index_path)
    db.execute("CREATE INDEX unexpected_research_jobs_state ON research_jobs(state)")
    db.commit()
    db.close()
    malformed = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{index_path}")
    )
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        await malformed.start()


@pytest.mark.asyncio
async def test_schema_attestation_rejects_ddl_only_constraint_tampering(tmp_path):
    database_path = tmp_path / "constraint.sqlite"
    repo = SQLiteRepository(str(database_path))
    repo.open()
    repo.close()
    db = sqlite3.connect(database_path)
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_jobs'"
    ).fetchone()[0]
    changed = f"{sql[:-1]}, CHECK(state != 'queued'))"
    assert changed != sql
    db.execute("PRAGMA writable_schema=ON")
    db.execute(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='research_jobs'",
        (changed,),
    )
    db.execute("PRAGMA schema_version = 1002")
    db.commit()
    db.close()

    malformed = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{database_path}")
    )
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        await malformed.start()


def test_prerelease_legacy_schema_is_rejected_without_upgrade(tmp_path):
    import thinkroom.repository as repository_module

    database_path = tmp_path / "legacy.sqlite"
    legacy_sql = repository_module._SCHEMA_SQL.replace("deadline TEXT NOT NULL, ", "").replace(
        ", backend TEXT, model TEXT)", ")"
    )
    assert legacy_sql != repository_module._SCHEMA_SQL
    db = sqlite3.connect(database_path)
    db.executescript(legacy_sql)
    before = db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    db.close()

    repo = SQLiteRepository(str(database_path))
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        repo.open()
    repo.close()

    db = sqlite3.connect(database_path)
    after = db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    db.close()
    assert after == before


@pytest.mark.asyncio
async def test_unexpected_retention_task_exit_fails_readiness(tmp_path, monkeypatch):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )

    async def stopped_retention_loop():
        return None

    monkeypatch.setattr(service, "_retention_loop", stopped_retention_loop)
    await service.start()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert service._retention_task is not None and service._retention_task.done()
        assert not service.ready
    finally:
        await service.stop()


def test_runtime_verifier_rejects_locked_but_unselected_distribution():
    import importlib.util
    from collections import Counter

    root = Path(__file__).parents[1]
    script = root / "scripts" / "verify_locked_runtime.py"
    spec = importlib.util.spec_from_file_location("verify_locked_runtime_selected", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    locked, selected = module.lock_contract(root / "uv.lock")
    extra = next(name for name in locked if name not in selected)
    installed = Counter({(name, next(iter(locked[name]))): 1 for name in selected | {extra}})
    with pytest.raises(RuntimeError, match="unexpected_selected"):
        module.verify_lock_membership(installed, locked, selected)


@pytest.mark.asyncio
async def test_database_swap_after_reservation_is_rejected_before_schema_write(
    tmp_path, monkeypatch
):
    database = tmp_path / "database.sqlite"
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"target-canary")
    service = ThinkroomService(Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}"))
    real_open = service.repo.open

    def swap_then_open(*args, **kwargs):
        database.unlink()
        database.symlink_to(target)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(service.repo, "open", swap_then_open)
    with pytest.raises(ValueError, match="database.*(symlink|identity)"):
        await service.start()
    assert target.read_bytes() == b"target-canary"
    assert service.repo.db is None
    assert service.lock.handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
async def test_database_sidecar_symlink_is_rejected_before_database_creation(tmp_path, suffix):
    database = tmp_path / "database.sqlite"
    target = tmp_path / "sidecar-target"
    target.write_bytes(b"sidecar-canary")
    Path(f"{database}{suffix}").symlink_to(target)
    service = ThinkroomService(Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}"))
    with pytest.raises(ValueError, match="database sidecar"):
        await service.start()
    assert target.read_bytes() == b"sidecar-canary"
    assert not database.exists()


@pytest.mark.asyncio
async def test_database_rejects_shared_sticky_immediate_directory(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{shared / 'database.sqlite'}")
    )
    with pytest.raises(ValueError, match="database directory is writable"):
        await service.start()


def test_partial_existing_schema_is_rejected_without_any_file_mutation(tmp_path):
    database = tmp_path / "partial.sqlite"
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE provider_calls (id INTEGER PRIMARY KEY, hostile TEXT)")
    db.commit()
    db.close()
    before = database.read_bytes()

    repo = SQLiteRepository(str(database))
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        repo.open()
    assert repo.db is None
    assert database.read_bytes() == before


def test_engine_has_no_process_infrastructure_composition_dependency():
    source = (Path(__file__).parents[1] / "src/thinkroom/engine.py").read_text()
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "process_backend" not in imported
    assert "isolate_backend" not in source


def test_normative_migration_policy_is_single_and_fail_closed():
    root = Path(__file__).parents[1]
    specification = (root / "docs/specification.md").read_text()
    operations = (root / "docs/OPERATIONS.md").read_text()
    contract = "v0.1.0 creates the canonical schema only when the database is empty"
    assert contract in specification
    assert contract in operations
    assert "Migrations are monotonic and run before readiness succeeds" not in specification


def _skills_tree_snapshot(root: Path) -> list[tuple[str, str, bytes | str | int]]:
    if not root.exists():
        return []
    snapshot: list[tuple[str, str, bytes | str | int]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode):
            snapshot.append((relative, "symlink", os.readlink(path)))
        elif stat.S_ISDIR(value.st_mode):
            snapshot.append((relative, "directory", ""))
        elif stat.S_ISREG(value.st_mode):
            snapshot.append((relative, "file", path.read_bytes()))
        else:
            snapshot.append((relative, "special", stat.S_IFMT(value.st_mode)))
    return snapshot


def test_skills_invalid_receipt_fails_before_any_target_mutation(tmp_path):
    target = tmp_path / "skills"
    receipt = target / ".thinkroom/skills-receipt-v1.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("not-json")
    before = _skills_tree_snapshot(target)
    with pytest.raises(ValueError, match="invalid receipt"):
        install(target)
    assert _skills_tree_snapshot(target) == before


@pytest.mark.parametrize(
    "unsafe_relative",
    [
        "thinkroom-install",
        "thinkroom-operate",
        "thinkroom-trigger",
        "thinkroom-install/SKILL.md",
        "thinkroom-operate/SKILL.md",
        "thinkroom-trigger/SKILL.md",
        ".thinkroom",
        ".thinkroom/skills-receipt-v1.json",
    ],
)
def test_skills_unsafe_target_family_fails_before_any_mutation(tmp_path, unsafe_relative):
    target = tmp_path / "skills"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe = target / unsafe_relative
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.symlink_to(outside, target_is_directory=True)
    before = _skills_tree_snapshot(target)
    with pytest.raises(ValueError):
        install(target)
    assert _skills_tree_snapshot(target) == before


@pytest.mark.asyncio
async def test_database_regular_replacement_after_reservation_is_rejected(tmp_path, monkeypatch):
    database = tmp_path / "database.sqlite"
    attacker = tmp_path / "attacker.sqlite"
    attacker.write_bytes(b"")
    service = ThinkroomService(Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}"))
    real_open = service.repo.open

    def replace_then_open(*args, **kwargs):
        os.replace(attacker, database)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(service.repo, "open", replace_then_open)
    with pytest.raises(ValueError, match="database.*identity"):
        await service.start()
    assert not attacker.exists()
    assert database.read_bytes() == b""


@pytest.mark.asyncio
async def test_lock_replacement_cannot_admit_a_second_service(tmp_path, monkeypatch):
    database = tmp_path / "database.sqlite"
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("replacement")
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    first = ThinkroomService(settings)
    real_open = first.repo.open

    def replace_lock_then_open(*args, **kwargs):
        os.replace(replacement, Path(f"{database}.lock"))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(first.repo, "open", replace_lock_then_open)
    with pytest.raises(ValueError, match="service lock"):
        await first.start()
    second = ThinkroomService(settings)
    await second.start()
    await second.stop()


@pytest.mark.asyncio
async def test_database_replacement_after_open_before_migrate_blocks_readiness(
    tmp_path, monkeypatch
):
    database = tmp_path / "database.sqlite"
    settings = Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
    initial = ThinkroomService(settings)
    await initial.start()
    await initial.stop()
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"")
    service = ThinkroomService(settings)
    real_migrate = service.repo.migrate

    def replace_then_migrate():
        os.replace(replacement, database)
        return real_migrate()

    monkeypatch.setattr(service.repo, "migrate", replace_then_migrate)
    with pytest.raises(ValueError, match="database.*identity"):
        await service.start()
    current = sqlite3.connect(database)
    try:
        assert (
            current.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            == 0
        )
    finally:
        current.close()


def _sqlite_family_snapshot(database: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{database}{suffix}")
        if path.exists():
            snapshot[suffix] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return snapshot


@pytest.mark.asyncio
async def test_live_noncanonical_wal_family_is_rejected_without_any_byte_mutation(tmp_path):
    database = tmp_path / "hostile.sqlite"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL").fetchone()
        writer.execute("CREATE TABLE hostile(value TEXT)")
        writer.execute("INSERT INTO hostile VALUES ('x')")
        writer.commit()
        before = _sqlite_family_snapshot(database)
        service = ThinkroomService(
            Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}")
        )
        with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
            await service.start()
        assert _sqlite_family_snapshot(database) == before
    finally:
        writer.close()


def test_engine_import_closure_excludes_provider_and_process_infrastructure():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; import thinkroom.engine; "
                "names=['thinkroom.backends','thinkroom.process_backend','thinkroom.service',"
                "'thinkroom.sdk','thinkroom.api','httpx']; "
                "print(json.dumps([name for name in names if name in sys.modules]))"
            ),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("thinkroom-install", "file"),
        (".thinkroom", "file"),
        ("thinkroom-install", "fifo"),
        ("thinkroom-install/SKILL.md", "directory"),
    ],
)
def test_skills_plan_and_status_reject_intermediate_collisions(tmp_path, relative, kind):
    target = tmp_path / "skills"
    target.mkdir()
    collision = target / relative
    collision.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        collision.write_text("collision")
    elif kind == "fifo":
        os.mkfifo(collision)
    else:
        collision.mkdir()
    before = _skills_tree_snapshot(target)
    with pytest.raises(ValueError):
        plan(target)
    with pytest.raises(ValueError):
        skill_status(target)
    assert _skills_tree_snapshot(target) == before


def test_skills_diverged_preflight_is_mutation_free(tmp_path):
    target = tmp_path / "skills"
    first = plan(target)[0]["path"]
    collision = target / first
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("unmanaged")
    before = _skills_tree_snapshot(target)
    with pytest.raises(ValueError, match="DIVERGED"):
        install(target)
    assert _skills_tree_snapshot(target) == before


def test_skills_plan_status_and_apply_agree_for_receipt_owned_missing_payload(tmp_path):
    target = tmp_path / "skills"
    install(target)
    missing = plan(target)[0]["path"]
    (target / missing).unlink()
    planned = plan(target)
    observed = skill_status(target)
    assert planned == observed
    assert next(item for item in planned if item["path"] == missing)["classification"] == "DIVERGED"
    before = _skills_tree_snapshot(target)
    with pytest.raises(ValueError, match="DIVERGED"):
        install(target)
    assert _skills_tree_snapshot(target) == before


@pytest.mark.asyncio
async def test_missing_database_parent_is_rejected_before_any_file_creation(tmp_path):
    parent = tmp_path / "missing"
    database = parent / "db.sqlite"
    service = ThinkroomService(Settings.from_env(database_url=f"sqlite+aiosqlite:///{database}"))
    try:
        with pytest.raises(ValueError, match="database parent"):
            await service.start()
        assert not parent.exists()
    finally:
        if service.ready or service.engine is not None:
            await service.stop()


def test_repository_direct_open_cannot_create_a_missing_database_parent(tmp_path):
    parent = tmp_path / "missing-repository-parent"
    repository = SQLiteRepository(str(parent / "db.sqlite"))
    with pytest.raises(ValueError, match="database parent"):
        repository.open()
    assert not parent.exists()


def test_mcp_invalid_cursor_is_mapped_to_invalid_argument():
    from mcp.server.fastmcp.exceptions import ToolError

    from thinkroom.mcp import _mcp_call
    from thinkroom.sdk import ThinkroomError

    def invalid_cursor():
        raise ThinkroomError(422, "INVALID_CURSOR", "request is invalid", {})

    with pytest.raises(ToolError) as raised:
        _mcp_call(invalid_cursor)
    payload = json.loads(str(raised.value))
    assert payload == {
        "code": "INVALID_ARGUMENT",
        "message": "request is invalid",
        "details": {},
    }


@pytest.mark.asyncio
async def test_not_ready_response_lists_only_allowlisted_failed_predicates(tmp_path):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    failed = response.json()["details"]["failed_predicates"]
    allowlisted = {
        "configuration_validated",
        "database_ready",
        "exclusive_lock_acquired",
        "recovery_complete",
        "workers_ready",
        "backend_ready",
        "provider_capacity_available",
    }
    assert failed
    assert set(failed) <= allowlisted


def test_skills_apply_race_rolls_back_only_operation_created_directories(tmp_path, monkeypatch):
    from thinkroom import skills as skills_module

    target = tmp_path / "skills-race"
    target.mkdir()
    real_parent = skills_module._SecureTree.parent
    triggered = False

    def racing_parent(tree, relative, *, create):
        nonlocal triggered
        result = real_parent(tree, relative, create=create)
        if create and not triggered and str(relative) == "thinkroom-install/SKILL.md":
            triggered = True
            attacker = target / "thinkroom-trigger/SKILL.md"
            attacker.parent.mkdir(parents=True, exist_ok=True)
            attacker.write_text("attacker")
        return result

    monkeypatch.setattr(skills_module._SecureTree, "parent", racing_parent)
    with pytest.raises(ValueError, match="target path changed"):
        install(target)
    assert _skills_tree_snapshot(target) == [
        ("thinkroom-trigger", "directory", ""),
        ("thinkroom-trigger/SKILL.md", "file", b"attacker"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:" + "9" * 5000, "[::1]:" + "9" * 5000],
)
async def test_oversized_numeric_host_port_returns_fixed_invalid_host(tmp_path, host):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/health/live", headers={"Host": host})
    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_HOST",
        "message": "Host header must be a literal loopback IP address",
        "details": {},
    }


def test_cli_list_forwards_cursor_for_pagination(monkeypatch):
    from typer.testing import CliRunner

    import thinkroom.cli as cli_module

    captured = {}

    def fake_list(client, *, limit, cursor):
        captured.update(limit=limit, cursor=cursor)
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(cli_module.ThinkroomClient, "list", fake_list)
    result = CliRunner().invoke(
        cli_module.app,
        ["list", "--limit", "2", "--cursor", "opaque-page-two"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"limit": 2, "cursor": "opaque-page-two"}


def test_sdk_research_forwards_idempotency_key_as_header_only(monkeypatch):
    from thinkroom.sdk import ThinkroomClient

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            202,
            json={"job_id": "job-1", "state": "queued"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    result = ThinkroomClient("http://test").research(
        "question", context="context", idempotency_key="stable-key"
    )
    assert result["job_id"] == "job-1"
    assert captured["headers"] == {"Idempotency-Key": "stable-key"}
    assert captured["json"] == {"question": "question", "context": "context"}


def test_cli_research_forwards_idempotency_key(monkeypatch):
    from typer.testing import CliRunner

    import thinkroom.cli as cli_module

    captured = {}

    def fake_research(client, question, **kwargs):
        captured.update(question=question, **kwargs)
        return {"job_id": "job-1", "state": "queued"}

    monkeypatch.setattr(cli_module.ThinkroomClient, "research", fake_research)
    result = CliRunner().invoke(
        cli_module.app,
        ["research", "--question", "question", "--idempotency-key", "stable-key"],
    )
    assert result.exit_code == 0, result.output
    assert captured["idempotency_key"] == "stable-key"


def test_mcp_research_forwards_idempotency_key(monkeypatch):
    import thinkroom.mcp as mcp_module

    captured = {}

    def fake_research(client, question, **kwargs):
        captured.update(question=question, **kwargs)
        return {"job_id": "job-1", "state": "queued"}

    monkeypatch.setattr(mcp_module.ThinkroomClient, "research", fake_research)
    result = mcp_module.thinkroom_research("question", idempotency_key="stable-key")
    assert result["job_id"] == "job-1"
    assert captured["idempotency_key"] == "stable-key"


def test_noncanonical_empty_sqlite_metadata_is_rejected_without_byte_changes(tmp_path):
    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=7")
    db.commit()
    db.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    repo = SQLiteRepository(path)
    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_MISMATCH"):
        repo.open()

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_skills_uninstall_rejects_dangling_target_symlink(tmp_path):
    from thinkroom.skills import uninstall

    target = tmp_path / "skills"
    target.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        uninstall(target)
    assert target.is_symlink()


def test_stable_error_projection_and_openapi_are_complete(tmp_path):
    from mcp.server.fastmcp.exceptions import ToolError

    from thinkroom.mcp import _mcp_call
    from thinkroom.sdk import ThinkroomError, _project_remote_error

    assert _project_remote_error(
        {"code": "INTERNAL_ERROR", "message": "hostile", "details": {"secret": "x"}}
    ) == ("INTERNAL_ERROR", "internal server error", {})

    for code in ("INVALID_HOST", "METHOD_NOT_ALLOWED"):
        with pytest.raises(ToolError) as caught:
            _mcp_call(lambda code=code: (_ for _ in ()).throw(ThinkroomError(400, code, "x")))
        body = json.loads(str(caught.value))
        assert body["code"] == "INVALID_ARGUMENT"

    app = create_app(
        ThinkroomService(
            Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
        )
    )
    schema = app.openapi()
    for methods in schema["paths"].values():
        for operation in methods.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            assert operation["responses"]["400"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ErrorBody")
            assert operation["responses"]["405"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ErrorBody")
            assert operation["responses"]["413"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ErrorBody")
            assert operation["responses"]["500"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ErrorBody")


@pytest.mark.asyncio
async def test_global_405_and_413_actual_responses_match_openapi(tmp_path):
    service = ThinkroomService(
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    )
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        oversized = await client.request(
            "GET", "/health/live", content=b"x" * (service.settings.max_context_bytes + 1)
        )
        method = await client.put("/api/v1/research", json={})
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert method.status_code == 405
    assert method.json()["code"] == "METHOD_NOT_ALLOWED"
    schema = app.openapi()
    assert "413" in schema["paths"]["/health/live"]["get"]["responses"]
    assert "405" in schema["paths"]["/api/v1/research"]["post"]["responses"]


def test_bundled_skills_claim_only_the_native_secure_platform():
    root = Path(__file__).parents[1] / "src/thinkroom/bundled_skills"
    for skill in root.glob("*/SKILL.md"):
        assert "platforms: [linux]" in skill.read_text()


def test_embedded_sdk_returns_the_durable_research_detail_shape(tmp_path):
    from thinkroom.sdk import Thinkroom

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    result = Thinkroom(
        settings=Settings.from_env(
            database_url=f"sqlite+aiosqlite:///{data / 'db.sqlite'}",
        )
    ).research("Should the embedded SDK share the durable result model?", branch_count=2)
    detail = ResearchDetail.model_validate(result)
    assert set(result) == set(ResearchDetail.model_fields)
    assert detail.request.question == "Should the embedded SDK share the durable result model?"
    assert detail.attempts
    assert detail.transitions
    assert detail.critique_id == detail.synthesis.consumed_critique_id
