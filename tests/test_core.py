import asyncio

import pytest

from thinkroom.backends import ScriptedBackend
from thinkroom.config import Settings
from thinkroom.engine import ResearchEngine
from thinkroom.repository import SQLiteRepository
from thinkroom.schemas import ResearchRequest
from thinkroom.skills import install, plan, status, uninstall


@pytest.mark.asyncio
async def test_scripted_end_to_end(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
    calls = []
    backend = ScriptedBackend(calls=calls)
    engine = ResearchEngine(
        repo,
        backend,
        Settings.from_env(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"),
    )
    job, _ = await engine.submit(
        ResearchRequest(question="Should we choose this important option?", branch_count=3)
    )
    for _ in range(50):
        if repo.get_job(job)["state"] == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert repo.get_job(job)["state"] == "succeeded"
    assert [c.phase for c in calls] == [
        "frame",
        "fork",
        "rollout",
        "rollout",
        "rollout",
        "critique",
        "synthesis",
    ]
    branch_inputs = [c.input for c in calls if c.phase == "rollout"]
    assert all("successful_branches" not in i for i in branch_inputs)


def test_skills_install_is_idempotent(tmp_path):
    target = tmp_path / "skills"
    assert all(x["classification"] == "ADD" for x in plan(target))
    install(target)
    assert all(x["classification"] == "EXACT" for x in status(target))
    (target / "thinkroom-trigger" / "SKILL.md").write_text("drift")
    assert any(x["classification"] == "DIVERGED" for x in status(target))
    with pytest.raises(ValueError):
        install(target)
    with pytest.raises(ValueError):
        uninstall(target)


def test_verified_evidence_requires_basis_and_reference():
    from pydantic import ValidationError

    from thinkroom.schemas import EvidenceV1

    with pytest.raises(ValidationError):
        EvidenceV1(
            id="evidence",
            statement="A claim",
            relationship="supports",
            verification_status="verified",
        )
    evidence = EvidenceV1(
        id="evidence",
        statement="A claim",
        relationship="supports",
        source_reference="https://example.test/source",
        verification_status="verified",
        verification_basis="Checked against the source",
    )
    assert evidence.verification_status.value == "verified"


def test_provider_parser_rejects_trailing_prose():
    from thinkroom.backends import BackendError, parse_json_object

    with pytest.raises(BackendError):
        parse_json_object('{"ok": true} trailing prose')
