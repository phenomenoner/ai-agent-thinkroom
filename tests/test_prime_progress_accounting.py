"""No-provider regressions for repeated Prime RPC progress projections.

These are constructed wire fixtures, not captured provider traffic. Lifecycle
validation and raw/event/count budgets remain owned by the real adapter.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from thinkroom import backends
from thinkroom.backends import PrimeAgentBackend
from thinkroom.ports import BackendError
from thinkroom.schemas import BackendRequestV1, FrameInputV1


def wire_bytes(event):
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) + 1


def progress(kind, payload="x" * 4096):
    if kind == "tool_execution_update":
        return {
            "type": kind,
            "toolCallId": "work-1",
            "toolName": "ipython",
            "args": {"code": payload},
            "partialResult": {"content": [{"type": "text", "text": payload}]},
        }
    return {
        "type": kind,
        "child": {
            "id": "child-1",
            "sessionName": "thinkroom-frame-worker",
            "status": "running",
            "repliedSinceTask": False,
            "answerPreview": payload,
            "recap": payload,
        },
    }


@pytest.mark.parametrize("kind", ["tool_execution_update", "rlm_child_update"])
def test_progress_snapshot_size_does_not_amplify_accounted_bytes(kind):
    small, large = progress(kind, "small"), progress(kind)
    untouched = copy.deepcopy(large)
    small_size = backends._prime_rpc_accounted_event_bytes(small, wire_bytes(small))
    large_size = backends._prime_rpc_accounted_event_bytes(large, wire_bytes(large))
    assert large_size == small_size
    assert large_size >= backends._PRIME_RPC_MIN_ACCOUNTED_EVENT_BYTES
    assert large == untouched


@pytest.mark.parametrize("kind", ["tool_execution_update", "rlm_child_update"])
def test_unknown_progress_fields_are_not_discounted(kind):
    event = progress(kind)
    event["unrecognized"] = "y" * 2048
    assert backends._prime_rpc_accounted_event_bytes(event, wire_bytes(event)) > 2048


@pytest.mark.parametrize(
    "kind", ["tool_execution_start", "tool_execution_end", "agent_end", "message_end", "unknown"]
)
def test_authoritative_and_unknown_events_remain_fully_accounted(kind):
    event = {"type": kind, "args": "a" * 2048, "partialResult": "b" * 2048}
    assert backends._prime_rpc_accounted_event_bytes(event, wire_bytes(event)) == wire_bytes(event)


def request():
    return BackendRequestV1(
        phase="frame",
        job_id="j",
        attempt_id="a",
        prompt_version="v",
        input=FrameInputV1(
            question="A sufficiently important question", domain="generic", guidance="g", safety="s"
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(seconds=15),
        correlation_id="c",
    )


def fixture_cli(tmp_path, kind, scenario):
    executable = tmp_path / "prime-progress-fixture"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "def send(e): print(json.dumps(e), flush=True)\n"
        "command = json.loads(sys.stdin.readline())\n"
        "cleanup = command['message'].split('```python\\n', 1)[1].split('\\n```', 1)[0]\n"
        "send({'id':command['id'], 'type':'response', 'command':'prompt', 'success':True})\n"
        f"event = {progress(kind)!r}\n"
        "for _ in range(12): send(event)\n"
        f"scenario = {scenario!r}\n"
        "if scenario == 'regression':\n"
        "    child = dict(event['child'])\n"
        "    child.update(status='done', repliedSinceTask=True)\n"
        "    send({'type':'rlm_child_update','child':child})\n"
        "    child.update(status='running')\n"
        "    send({'type':'rlm_child_update','child':child})\n"
        "if scenario == 'identity':\n"
        "    child = dict(event['child'])\n"
        "    child.update(id='other-child')\n"
        "    send({'type':'rlm_child_update','child':child})\n"
        "child_message = {'role':'custom','customType':'agent_message','details':"
        "{'message':'done','fromRelationship':'child','from':{'sessionName':'thinkroom-frame-worker'}}}\n"
        "send({'type':'message_end','message':child_message})\n"
        "if scenario != 'missing-cleanup':\n"
        "    send({'type':'tool_execution_start','toolName':'ipython','toolCallId':'cleanup-1','args':{'code':cleanup}})\n"
        "    send({'type':'tool_execution_end','toolName':'ipython','toolCallId':'cleanup-1','isError':False,"
        "'result':'THINKROOM_CHILD_CLEANED:thinkroom-frame-worker\\nTHINKROOM_CHILD_ID:child-1'})\n"
        "result = {'schema_version':1,'decision':'d','scope':'s','constraints':['c'],"
        "'success_criteria':['s'],'ambiguities':['a'],'research_questions':['q']}\n"
        "if scenario == 'oversize-final': result['decision'] = 'z' * 20000\n"
        "terminal = {'role':'assistant','content':[{'type':'text','text':json.dumps(result)}],'stopReason':'stop'}\n"
        "send({'type':'message_end','message':terminal})\n"
        "send({'type':'agent_end','messages':[child_message, terminal]})\n"
        "sys.stdin.read()\n"
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize("kind", ["tool_execution_update", "rlm_child_update"])
@pytest.mark.asyncio
async def test_progress_flood_with_small_final_completes_under_accounted_budget(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(backends, "_PRIME_RPC_ACCOUNTED_BYTE_LIMIT", 16000)
    executable = fixture_cli(tmp_path, kind, "valid")
    result = await PrimeAgentBackend(
        str(executable), "", "", "off", max_response_bytes=10000
    ).invoke(request())
    assert result["decision"] == "d"
    assert result.transport_metrics.raw_transport_bytes > 90000
    assert result.transport_metrics.accounted_transport_bytes < 16000


@pytest.mark.parametrize(
    "kind,scenario,override,expected_audit,expected_message",
    [
        (
            "tool_execution_update",
            "valid",
            ("_PRIME_RPC_ABSOLUTE_RAW_BYTE_LIMIT", 16000),
            "OUTPUT_LIMIT_RAW_TRANSPORT",
            "raw transport",
        ),
        (
            "rlm_child_update",
            "valid",
            ("_PRIME_RPC_ABSOLUTE_RAW_BYTE_LIMIT", 16000),
            "OUTPUT_LIMIT_RAW_TRANSPORT",
            "raw transport",
        ),
        (
            "tool_execution_update",
            "valid",
            ("_PRIME_RPC_ACCOUNTED_BYTE_LIMIT", 256),
            "OUTPUT_LIMIT_ACCOUNTED_TRANSPORT",
            "accounted transport",
        ),
        (
            "tool_execution_update",
            "valid",
            ("_PRIME_RPC_EVENT_COUNT_LIMIT", 3),
            "OUTPUT_LIMIT_SEMANTIC_EVENTS",
            "semantic event-count",
        ),
        ("rlm_child_update", "regression", None, None, "status regressed"),
        ("rlm_child_update", "identity", None, None, "replaced the expected"),
        ("tool_execution_update", "missing-cleanup", None, None, "before RLM child cleanup"),
        ("tool_execution_update", "oversize-final", None, None, "exceeded byte limit"),
    ],
)
@pytest.mark.asyncio
async def test_projection_preserves_transport_final_and_lifecycle_guards(
    tmp_path, monkeypatch, kind, scenario, override, expected_audit, expected_message
):
    if override:
        monkeypatch.setattr(backends, *override)
    executable = fixture_cli(tmp_path, kind, scenario)
    with pytest.raises(BackendError) as caught:
        await PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000).invoke(
            request()
        )
    assert expected_message in str(caught.value)
    if expected_audit:
        assert caught.value.audit_status == expected_audit


@pytest.mark.asyncio
async def test_each_invocation_owns_socket_without_replacing_auth_home(tmp_path, monkeypatch):
    import asyncio
    import os
    from pathlib import Path

    agent_home = str(tmp_path / "operator-auth-home")
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", agent_home)
    original = asyncio.create_subprocess_exec
    launches = []

    async def capture(*args, **kwargs):
        launches.append((args, kwargs.get("env")))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    executable = fixture_cli(tmp_path, "tool_execution_update", "valid")
    backend = PrimeAgentBackend(str(executable), "", "", "off", max_response_bytes=10000)
    for _ in range(2):
        assert (await backend.invoke(request()))["decision"] == "d"
    assert len(launches) == 2
    sockets = []
    for args, env in launches:
        assert "--daemon-socket" in args
        socket = Path(args[args.index("--daemon-socket") + 1])
        session = Path(args[args.index("--session-dir") + 1])
        assert socket == session / "daemon.sock"
        assert args[args.index("--cwd") + 1] == str(session)
        assert not session.exists()
        assert env is None or env.get("PRIME_AGENT_CODING_AGENT_DIR") == agent_home
        sockets.append(socket)
    assert sockets[0] != sockets[1]
    assert os.environ["PRIME_AGENT_CODING_AGENT_DIR"] == agent_home
