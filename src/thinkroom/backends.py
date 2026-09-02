from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import httpx

from .ports import (
    BackendError,
    BackendResult,
    BackendTransportMetrics,
    ProviderInvocationAudit,
    RolloutBackend,
    backend_input,
    provider_payload,
)
from .schemas import (
    BackendRequestV1,
    BranchOutputV1,
    CritiqueOutputV1,
    EvidenceV1,
    ForkOutputV1,
    FrameOutputV1,
    SynthesisOutputV1,
)


@dataclass
class ScriptedBackend:
    name: str = "scripted"
    model: str = "scripted-v1"
    calls: list[BackendRequestV1] | None = None

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        if self.calls is not None:
            self.calls.append(request)
        data = backend_input(request)
        if request.phase == "frame":
            return FrameOutputV1(
                decision=data["question"],
                scope="bounded decision scope",
                constraints=["time and resources"],
                success_criteria=["clear trade-offs"],
                ambiguities=["missing external evidence"],
                research_questions=["Which option best satisfies the criteria?"],
            ).model_dump()
        if request.phase == "fork":
            count = int(data["branch_count"])
            return ForkOutputV1(
                perspectives=cast(
                    Any,
                    [
                        {
                            "id": f"perspective-{i + 1}",
                            "title": f"Perspective {i + 1}",
                            "hypothesis": f"Hypothesis {i + 1}",
                            "approach": f"Approach {i + 1}",
                            "differentiator": f"Different lens {i + 1}",
                        }
                        for i in range(count)
                    ],
                )
            ).model_dump()
        if request.phase == "rollout":
            bid = request.branch_id or "branch"
            e = EvidenceV1(
                id="evidence-1",
                statement=f"Scripted evidence for {bid}",
                relationship="supports",
                verification_status=cast(Any, "unverified"),
            )
            return BranchOutputV1(
                summary=f"Scripted rollout for {bid}",
                claims=cast(
                    Any, [{"statement": "The proposal is plausible", "evidence_ids": [e.id]}]
                ),
                supporting_evidence=[e],
                contradicting_evidence=[],
                assumptions=["scripted assumptions"],
                uncertainties=["external validation pending"],
                falsifiers=["trusted contrary artifact"],
                next_checks=["Run an independent validation"],
            ).model_dump()
        if request.phase == "critique":
            ids = list(data["successful_branch_ids"])
            return CritiqueOutputV1(
                agreements=["Branches identify plausible options"],
                contradictions=["Trade-offs differ"],
                unsupported_claims=["No independently verified sources"],
                blind_spots=["Operational context"],
                discriminating_evidence=["A trusted local test"],
                branch_assessments=cast(
                    Any,
                    [
                        {
                            "branch_id": i,
                            "strengths": ["clear reasoning"],
                            "weaknesses": ["unverified evidence"],
                            "support_level": "weak",
                        }
                        for i in ids
                    ],
                ),
                consumed_branch_ids=ids,
            ).model_dump()
        if request.phase == "synthesis":
            ids = list(data["successful_branch_ids"])
            return SynthesisOutputV1(
                disposition=cast(Any, "NEED_MORE_EVIDENCE"),
                recommendation="Use the leading option only after validation.",
                rationale="All available evidence is unverified.",
                ranked_alternatives=cast(
                    Any,
                    [
                        {
                            "title": "Defer",
                            "description": "Collect trusted evidence first.",
                            "tradeoffs": ["slower decision"],
                        }
                    ],
                ),
                evidence_ledger=cast(
                    Any,
                    [
                        {
                            "evidence_id": "evidence-1",
                            "branch_id": i,
                            "statement": f"Scripted evidence for {i}",
                            "verification_status": "unverified",
                            "provenance": "rollout branch artifact",
                        }
                        for i in ids
                    ],
                ),
                disagreements=["Relative option ranking"],
                uncertainties=["External facts"],
                falsifiers=["Trusted contradictory evidence"],
                next_actions=["Collect and verify a local artifact"],
                source_attempt_id=request.attempt_id,
                consumed_branch_ids=ids,
                consumed_critique_id=str(data["critique_id"]),
            ).model_dump()
        raise BackendError("UNSUPPORTED_PHASE", request.phase)


async def _drain_unretained(stream: asyncio.StreamReader) -> None:
    """Prevent stderr backpressure without making diagnostics result evidence."""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return


async def _terminate_process(proc: asyncio.subprocess.Process, grace: float = 1.0) -> None:
    """Gracefully stop a child, escalating only after the bounded grace period."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


class OpenAIBackend:
    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 180.0,
        max_output_tokens: int = 8192,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        if type(model) is not str or not 1 <= len(model) <= 256:
            raise ValueError("openai model must contain 1 to 256 characters")
        if type(base_url) is not str:
            raise ValueError("openai base URL must be an HTTP(S) URL")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("openai base URL must be an HTTP(S) URL without userinfo")
        (
            self.base_url,
            self.api_key,
            self.model,
            self.timeout,
            self.max_output_tokens,
            self.max_response_bytes,
        ) = base_url.rstrip("/"), api_key, model, timeout, max_output_tokens, max_response_bytes

    @classmethod
    def from_env(
        cls, timeout: float, max_output_tokens: int = 8192, max_response_bytes: int = 1_000_000
    ) -> OpenAIBackend:
        key = os.getenv("THINKROOM_OPENAI_API_KEY")
        if not key:
            raise ValueError("THINKROOM_OPENAI_API_KEY is required for openai backend")
        return cls(
            os.getenv("THINKROOM_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            key,
            os.getenv("THINKROOM_OPENAI_MODEL", "gpt-4o-mini"),
            timeout,
            max_output_tokens,
            max_response_bytes,
        )

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return exactly one JSON object matching the requested schema.",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(provider_payload(request), ensure_ascii=False),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": self.max_output_tokens,
                },
            ) as response:
                if response.status_code >= 400:
                    code = "RATE_LIMITED" if response.status_code == 429 else "PROVIDER_ERROR"
                    raise BackendError(code, f"provider returned HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "provider response exceeded byte limit",
                            audit_status="OUTPUT_LIMIT_PROVIDER_RESPONSE",
                        )
                    chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("provider message content must be a string")
            return parse_json_object(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BackendError(
                "MALFORMED_PROVIDER_OUTPUT", "provider output was not one JSON object"
            ) from exc


def _validate_prime_argv_setting(
    label: str,
    value: str,
    *,
    max_characters: int,
    max_bytes: int,
    allow_empty: bool = True,
) -> str:
    if type(value) is not str or (not allow_empty and not value) or "\x00" in value:
        raise ValueError(f"prime_agent {label} must be an exact nonempty string")
    if len(value) > max_characters or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"prime_agent {label} exceeds its safe argv limit")
    return value


_PRIME_RPC_RAW_BYTE_LIMIT = 64_000_000
_PRIME_RPC_EVENT_COUNT_LIMIT = 20_000
_PRIME_RPC_TELEMETRY_EVENT_COUNT_LIMIT = 200_000
_PRIME_CHILD_CLEANUP_MARKER_PREFIX = "THINKROOM_CHILD_CLEANED:"
_PRIME_CHILD_ID_MARKER_PREFIX = "THINKROOM_CHILD_ID:"


def _prime_child_cleanup_marker(child_name: str) -> str:
    return f"{_PRIME_CHILD_CLEANUP_MARKER_PREFIX}{child_name}"


def _prime_child_id_marker(child_id: str) -> str:
    return f"{_PRIME_CHILD_ID_MARKER_PREFIX}{child_id}"


def _prime_child_cleanup_recipe(child_name: str) -> str:
    marker = _prime_child_cleanup_marker(child_name)
    return "\n".join(
        [
            "import asyncio as _asyncio",
            "_expected_child_id = getattr(_thinkroom_child, 'rlm_child_id', None)",
            "if not isinstance(_expected_child_id, str) or not _expected_child_id:",
            "    raise RuntimeError('Thinkroom RLM admission handle omitted identity')",
            "_child = None",
            "for _poll in range(30):",
            "    _children = await rlm.list_subagents()",
            f"    if any(c.session_name != {child_name!r} for c in _children):",
            "        raise RuntimeError('unexpected direct Prime RLM child')",
            f"    _matches = [c for c in _children if c.session_name == {child_name!r}]",
            "    if len(_matches) > 1:",
            "        raise RuntimeError('multiple matching Thinkroom RLM children')",
            "    if len(_matches) == 1 and _matches[0].rlm_child_id != _expected_child_id:",
            "        raise RuntimeError('Thinkroom RLM child identity changed before cleanup')",
            "    if len(_matches) == 1 and _matches[0].status == 'completed':",
            "        _child = _matches[0]",
            "        break",
            "    if len(_matches) == 1 and _matches[0].status == 'error':",
            "        raise RuntimeError('Thinkroom RLM child failed before cleanup')",
            "    await _asyncio.sleep(1)",
            "if _child is None:",
            "    raise RuntimeError('Thinkroom RLM child did not complete before cleanup deadline')",
            "_deleted_child = await rlm.delete_subagent(_child)",
            "if getattr(_deleted_child, 'rlm_child_id', None) != _expected_child_id:",
            "    raise RuntimeError('Thinkroom RLM cleanup receipt identity changed')",
            "for _poll in range(20):",
            "    _remaining = await rlm.list_subagents()",
            "    if not _remaining:",
            "        break",
            f"    if any(c.session_name != {child_name!r} for c in _remaining):",
            "        raise RuntimeError('unexpected direct Prime RLM child after cleanup')",
            "    await _asyncio.sleep(0.1)",
            "else:",
            "    raise RuntimeError('Thinkroom RLM child cleanup was not confirmed')",
            f"print({marker!r})",
            "print(f'THINKROOM_CHILD_ID:{_expected_child_id}')",
        ]
    )


def _prime_ipython_code(event: dict[str, Any]) -> str | None:
    if event.get("type") != "tool_execution_start" or event.get("toolName") != "ipython":
        return None
    args = event.get("args")
    if not isinstance(args, dict):
        return None
    code = args.get("code")
    return code if isinstance(code, str) else None


def _prime_tool_result_contains(event: dict[str, Any], expected: str, tool_call_id: str) -> bool:
    if (
        event.get("type") != "tool_execution_end"
        or event.get("toolName") != "ipython"
        or event.get("toolCallId") != tool_call_id
        or event.get("isError") is not False
    ):
        return False
    result = event.get("result")
    texts: list[str] = []
    if isinstance(result, str):
        texts.append(result)
    elif isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                item["text"]
                for item in content[:128]
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
    return any(expected in text.splitlines() for text in texts)


class PrimeAgentBackend:
    name = "prime_agent"

    def __init__(
        self,
        executable: str,
        provider: str,
        model: str,
        thinking: str,
        timeout: float = 180.0,
        max_output_tokens: int = 8192,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        executable = _validate_prime_argv_setting(
            "executable", executable, max_characters=4096, max_bytes=4096, allow_empty=False
        )
        provider = _validate_prime_argv_setting(
            "provider", provider, max_characters=256, max_bytes=1024
        )
        model = _validate_prime_argv_setting("model", model, max_characters=256, max_bytes=1024)
        thinking = _validate_prime_argv_setting(
            "thinking", thinking, max_characters=64, max_bytes=256
        )
        self.name = f"prime_agent:{provider or 'configured-default'}"
        self.configured_model = model
        (
            self.executable,
            self.provider,
            self.model,
            self.thinking,
            self.timeout,
            self.max_output_tokens,
            self.max_response_bytes,
        ) = (
            executable,
            provider,
            model or "configured-default",
            thinking,
            timeout,
            max_output_tokens,
            max_response_bytes,
        )

    @classmethod
    def from_env(
        cls, timeout: float, max_output_tokens: int = 8192, max_response_bytes: int = 1_000_000
    ) -> PrimeAgentBackend:
        executable = os.getenv("THINKROOM_PRIME_AGENT_EXECUTABLE")
        if not executable:
            raise ValueError("THINKROOM_PRIME_AGENT_EXECUTABLE is required for prime_agent backend")
        resolved = shutil.which(executable)
        if resolved is None:
            raise ValueError("prime_agent executable does not exist or is not executable")
        executable_path = Path(resolved)
        if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise ValueError("prime_agent executable does not exist or is not executable")
        return cls(
            str(executable_path.resolve()),
            os.getenv("THINKROOM_PRIME_AGENT_PROVIDER", ""),
            os.getenv("THINKROOM_PRIME_AGENT_MODEL", ""),
            os.getenv("THINKROOM_PRIME_AGENT_THINKING", "off"),
            timeout,
            max_output_tokens,
            max_response_bytes,
        )

    @classmethod
    def fallback_from_env(
        cls, timeout: float, max_output_tokens: int = 8192, max_response_bytes: int = 1_000_000
    ) -> PrimeAgentBackend:
        executable = os.getenv("THINKROOM_PRIME_AGENT_EXECUTABLE")
        if not executable:
            raise ValueError("THINKROOM_PRIME_AGENT_EXECUTABLE is required for prime_agent backend")
        resolved = shutil.which(executable)
        if resolved is None:
            raise ValueError("prime_agent executable does not exist or is not executable")
        executable_path = Path(resolved)
        if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise ValueError("prime_agent executable does not exist or is not executable")
        provider = os.getenv("THINKROOM_PRIME_AGENT_FALLBACK_PROVIDER")
        model = os.getenv("THINKROOM_PRIME_AGENT_FALLBACK_MODEL")
        thinking = os.getenv("THINKROOM_PRIME_AGENT_FALLBACK_THINKING")
        if not provider:
            raise ValueError("THINKROOM_PRIME_AGENT_FALLBACK_PROVIDER is required for failover")
        if not model:
            raise ValueError("THINKROOM_PRIME_AGENT_FALLBACK_MODEL is required for failover")
        if not thinking:
            raise ValueError("THINKROOM_PRIME_AGENT_FALLBACK_THINKING is required for failover")
        return cls(
            str(executable_path.resolve()),
            provider,
            model,
            thinking,
            timeout,
            max_output_tokens,
            max_response_bytes,
        )

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        payload = provider_payload(request)
        # Prime Agent exposes no supported output-token CLI flag. Use the token
        # budget only as four-byte-per-token prompt guidance and enforce the
        # configured response-byte ceiling independently below.
        target_output_bytes = min(self.max_response_bytes, self.max_output_tokens * 4)
        payload["instruction"] += (
            f" Keep the complete JSON under {target_output_bytes} UTF-8 bytes; "
            "be concise, emit no prose, and include only schema fields."
        )
        child_name = f"thinkroom-{request.phase}-worker"
        cleanup_recipe = _prime_child_cleanup_recipe(child_name)
        cleanup_marker = _prime_child_cleanup_marker(child_name)
        provider_request = json.dumps(payload, ensure_ascii=False)
        prompt = (
            "Act only as a structured Thinkroom research provider. Do not read or write files, "
            "run shell commands, or perform external effects. Use the persistent IPython kernel "
            "and call the preloaded rlm exactly once. Assign its returned admission handle to "
            "the exact variable `_thinkroom_child` and name the child "
            f"{child_name}. Give that child the complete PROVIDER_REQUEST_JSON below and instruct "
            "it to solve the requested research phase independently, then send its findings to "
            "the parent with agent_message.send(receiver_role='parent'). End the first turn after "
            "showing the admission handle. Do not invent or infer the child response. Only after a "
            "real child agent_message arrives, execute this exact cleanup recipe in one ipython tool "
            "call before producing the final JSON:\n"
            f"```python\n{cleanup_recipe}\n```\n"
            "Only after the cleanup marker is printed, synthesize and return exactly one JSON object "
            "that matches output_json_schema, with no markdown or prose.\n\n"
            f"PROVIDER_REQUEST_JSON:\n{provider_request}"
        )
        if len(prompt.encode("utf-8")) > 65536:
            raise BackendError(
                "CONTEXT_LIMIT_EXCEEDED",
                "Prime Agent RPC prompt exceeds the safe byte limit (65536)",
            )

        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[None] | None = None
        session_dir = tempfile.TemporaryDirectory(prefix="thinkroom-prime-rpc-")
        try:
            argv = [
                self.executable,
                "--mode",
                "rpc",
                "--tools",
                "ipython",
                "--cwd",
                session_dir.name,
                "--no-extensions",
                "--no-context-files",
                "--no-prompt-templates",
                "--session-dir",
                session_dir.name,
            ]
            if self.provider:
                argv += ["--provider", self.provider]
            if self.configured_model:
                argv += ["--model", self.configured_model]
            if self.thinking:
                argv += ["--thinking", self.thinking]
            encoded_sizes = [len(argument.encode("utf-8")) for argument in argv]
            if (
                any(size > 65536 for size in encoded_sizes)
                or sum(size + 1 for size in encoded_sizes) > 65536
            ):
                raise BackendError(
                    "CONTEXT_LIMIT_EXCEEDED",
                    "Prime Agent argv exceeds the safe aggregate byte limit (65536)",
                )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_PRIME_RPC_RAW_BYTE_LIMIT,
            )
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            rpc_proc = proc
            rpc_stdin = proc.stdin
            rpc_stdout = proc.stdout
            stderr_task = asyncio.create_task(_drain_unretained(proc.stderr))
            command = (
                json.dumps(
                    {"id": "thinkroom-provider", "type": "prompt", "message": prompt},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            rpc_stdin.write(command)
            await rpc_stdin.drain()

            transport_counters = {
                "raw_transport_bytes": 0,
                "event_count": 0,
                "max_event_bytes": 0,
                "message_update_count": 0,
                "message_snapshot_bytes": 0,
                "message_delta_bytes": 0,
            }

            def transport_metrics() -> BackendTransportMetrics:
                return BackendTransportMetrics(**transport_counters)

            def serialized_value_bytes(value: object) -> int:
                return len(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )

            async def run_rpc() -> dict[str, Any]:
                control_event_bytes = 0
                semantic_event_count = 0
                telemetry_event_count = 0
                prompt_accepted = False
                child_message_received = False
                child_snapshot_id: str | None = None
                child_snapshot_status: str | None = None
                child_snapshot_replied = False
                child_snapshot_completed = False
                cleanup_tool_call_id: str | None = None
                cleanup_observed = False
                post_cleanup_terminal_text: str | None = None

                def is_child_message(message: object) -> bool:
                    if not isinstance(message, dict):
                        return False
                    details = message.get("details")
                    sender = details.get("from") if isinstance(details, dict) else None
                    return (
                        message.get("role") == "custom"
                        and message.get("customType") == "agent_message"
                        and isinstance(details, dict)
                        and details.get("fromRelationship") == "child"
                        and isinstance(sender, dict)
                    )

                def child_message_matches(message: object) -> bool:
                    if not is_child_message(message) or not isinstance(message, dict):
                        return False
                    details = message.get("details")
                    if not isinstance(details, dict):
                        return False
                    sender = details.get("from")
                    return isinstance(sender, dict) and sender.get("sessionName") == child_name

                def assistant_text(message: object) -> str | None:
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        return None
                    stop_reason = message.get("stopReason")
                    if stop_reason == "toolUse":
                        return None
                    if stop_reason not in {None, "stop"}:
                        raise BackendError("PROVIDER_ERROR", "Prime Agent assistant turn failed")
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if not isinstance(content, list):
                        return None
                    blocks = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    return "".join(blocks) if blocks else None

                while True:
                    try:
                        line = await rpc_stdout.readline()
                    except ValueError as exc:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent RPC event exceeded byte limit",
                            audit_status="OUTPUT_LIMIT_RPC_EVENT_BYTES",
                        ) from exc
                    if not line:
                        returncode = await rpc_proc.wait()
                        if returncode != 0:
                            raise BackendError(
                                "PROVIDER_ERROR", "Prime Agent RPC exited unsuccessfully"
                            )
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC ended before an RLM-backed result",
                        )
                    line_bytes = len(line)
                    transport_counters["raw_transport_bytes"] += line_bytes
                    transport_counters["event_count"] += 1
                    transport_counters["max_event_bytes"] = max(
                        transport_counters["max_event_bytes"], line_bytes
                    )
                    if transport_counters["raw_transport_bytes"] > _PRIME_RPC_RAW_BYTE_LIMIT:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent RPC raw transport exceeded byte limit",
                            audit_status="OUTPUT_LIMIT_RAW_TRANSPORT",
                        )
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC emitted invalid JSONL",
                        ) from exc
                    if not isinstance(event, dict):
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC event was not an object",
                        )
                    if event.get("type") == "message_update":
                        telemetry_event_count += 1
                        transport_counters["message_update_count"] += 1
                        if "message" in event:
                            transport_counters["message_snapshot_bytes"] += serialized_value_bytes(
                                event["message"]
                            )
                        if "delta" in event:
                            transport_counters["message_delta_bytes"] += serialized_value_bytes(
                                event["delta"]
                            )
                        if telemetry_event_count > _PRIME_RPC_TELEMETRY_EVENT_COUNT_LIMIT:
                            raise BackendError(
                                "OUTPUT_LIMIT_EXCEEDED",
                                "Prime Agent RPC exceeded the telemetry event-count limit",
                                audit_status="OUTPUT_LIMIT_TELEMETRY_EVENTS",
                            )
                    else:
                        semantic_event_count += 1
                        if semantic_event_count > _PRIME_RPC_EVENT_COUNT_LIMIT:
                            raise BackendError(
                                "OUTPUT_LIMIT_EXCEEDED",
                                "Prime Agent RPC exceeded the semantic event-count limit",
                                audit_status="OUTPUT_LIMIT_SEMANTIC_EVENTS",
                            )
                    if event.get("type") == "response":
                        control_event_bytes += len(line)
                        if control_event_bytes > self.max_response_bytes:
                            raise BackendError(
                                "OUTPUT_LIMIT_EXCEEDED",
                                "Prime Agent RPC control events exceeded byte limit",
                                audit_status="OUTPUT_LIMIT_CONTROL_BYTES",
                            )
                    if event.get("type") == "response" and event.get("id") == "thinkroom-provider":
                        if event.get("command") != "prompt" or event.get("success") is not True:
                            raise BackendError(
                                "PROVIDER_ERROR", "Prime Agent RPC rejected the provider prompt"
                            )
                        prompt_accepted = True
                        continue
                    if event.get("type") == "rlm_child_update":
                        child = event.get("child")
                        if not isinstance(child, dict):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child update omitted lifecycle metadata",
                            )
                        candidate_id = child.get("id")
                        session_name = child.get("sessionName")
                        status = child.get("status")
                        replied_value = child.get("repliedSinceTask")
                        replied = replied_value is True
                        if (
                            not isinstance(candidate_id, str)
                            or not candidate_id
                            or "\x00" in candidate_id
                            or len(candidate_id.encode("utf-8")) > 256
                            or not isinstance(session_name, str)
                            or not session_name
                            or "\x00" in session_name
                            or len(session_name.encode("utf-8")) > 256
                            or status not in {"queued", "running", "done", "error", "cancelled"}
                            or (replied_value is not None and type(replied_value) is not bool)
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child update was invalid",
                            )
                        if session_name != child_name:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted an unexpected RLM child",
                            )
                        if child_snapshot_id is None:
                            child_snapshot_id = candidate_id
                        elif child_snapshot_id != candidate_id:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent replaced the expected RLM child identity",
                            )
                        if cleanup_observed:
                            if (
                                status == "cancelled"
                                and child_snapshot_replied
                                and child_snapshot_completed
                            ):
                                continue
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted child custody after RLM child cleanup",
                            )
                        if child_snapshot_replied and replied_value is False:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child reply evidence regressed",
                            )
                        lifecycle_rank = {"queued": 0, "running": 1, "done": 2}
                        if (
                            child_snapshot_status in lifecycle_rank
                            and status in lifecycle_rank
                            and lifecycle_rank[status] < lifecycle_rank[child_snapshot_status]
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child lifecycle status regressed",
                            )
                        if child_snapshot_status == "done" and status not in {"done", "cancelled"}:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child lifecycle status regressed",
                            )
                        if status == "error" or (
                            status == "cancelled" and cleanup_tool_call_id is None
                        ):
                            raise BackendError(
                                "PROVIDER_ERROR", "Prime Agent RLM child failed before cleanup"
                            )
                        child_snapshot_replied = child_snapshot_replied or replied
                        child_snapshot_completed = child_snapshot_completed or status == "done"
                        child_snapshot_status = status
                        continue
                    if (
                        event.get("type") == "custom_message"
                        and event.get("customType") == "agent_message"
                    ):
                        child_event = {
                            "role": "custom",
                            "customType": "agent_message",
                            "details": event.get("details"),
                        }
                        if cleanup_observed and is_child_message(child_event):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted child custody after RLM child cleanup",
                            )
                        if is_child_message(child_event) and not child_message_matches(child_event):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted an unexpected RLM child",
                            )
                        child_message_received = child_message_received or child_message_matches(
                            child_event
                        )
                        continue
                    if event.get("type") == "message_end":
                        message = event.get("message")
                        matched_child = child_message_matches(message)
                        if cleanup_observed and is_child_message(message):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted child custody after RLM child cleanup",
                            )
                        if is_child_message(message) and not matched_child:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent emitted an unexpected RLM child",
                            )
                        child_message_received = child_message_received or matched_child
                        text = assistant_text(message)
                        child_custody_received = child_message_received or child_snapshot_replied
                        if not child_custody_received and text is not None:
                            try:
                                parse_json_object(text)
                            except BackendError:
                                pass
                            else:
                                raise BackendError(
                                    "MALFORMED_PROVIDER_OUTPUT",
                                    "Prime Agent emitted final JSON before child custody",
                                )
                        elif child_custody_received and not cleanup_observed and not matched_child:
                            if text is not None:
                                try:
                                    parse_json_object(text)
                                except BackendError:
                                    pass
                                else:
                                    raise BackendError(
                                        "MALFORMED_PROVIDER_OUTPUT",
                                        "Prime Agent emitted final JSON before RLM child cleanup",
                                    )
                        elif cleanup_observed and text is not None:
                            parse_json_object(text)
                            if post_cleanup_terminal_text is not None:
                                raise BackendError(
                                    "MALFORMED_PROVIDER_OUTPUT",
                                    "Prime Agent emitted multiple terminal messages after RLM child cleanup",
                                )
                            post_cleanup_terminal_text = text
                        continue
                    ipython_code = _prime_ipython_code(event)
                    if cleanup_observed and event.get("type") in {
                        "tool_execution_start",
                        "tool_execution_end",
                    }:
                        if (
                            ipython_code is not None
                            and ipython_code.strip() == cleanup_recipe.strip()
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent replayed the RLM child cleanup recipe",
                            )
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent executed a tool after RLM child cleanup",
                        )
                    if ipython_code is not None and ipython_code.strip() == cleanup_recipe.strip():
                        if not prompt_accepted or not (
                            child_message_received or child_snapshot_replied
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent began RLM child cleanup before child custody",
                            )
                        if cleanup_tool_call_id is not None:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent replayed the RLM child cleanup recipe",
                            )
                        candidate_tool_call_id = event.get("toolCallId")
                        if (
                            not isinstance(candidate_tool_call_id, str)
                            or not candidate_tool_call_id
                            or "\x00" in candidate_tool_call_id
                            or len(candidate_tool_call_id.encode("utf-8")) > 256
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent cleanup tool call omitted a valid identity",
                            )
                        cleanup_tool_call_id = candidate_tool_call_id
                        continue
                    if (
                        cleanup_tool_call_id is not None
                        and event.get("type") == "tool_execution_end"
                        and event.get("toolName") == "ipython"
                        and event.get("toolCallId") == cleanup_tool_call_id
                    ):
                        if cleanup_observed:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent replayed the RLM child cleanup result",
                            )
                        if not _prime_tool_result_contains(
                            event, cleanup_marker, cleanup_tool_call_id
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child cleanup did not complete successfully",
                            )
                        if child_snapshot_id is not None and not _prime_tool_result_contains(
                            event,
                            _prime_child_id_marker(child_snapshot_id),
                            cleanup_tool_call_id,
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent RLM child cleanup identity did not match observed child",
                            )
                        if (
                            not child_message_received
                            and child_snapshot_replied
                            and not child_snapshot_completed
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent omitted completed RLM child lifecycle evidence",
                            )
                        cleanup_observed = True
                        continue
                    if event.get("type") != "agent_end":
                        continue
                    messages = event.get("messages")
                    if not isinstance(messages, list):
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC agent_end omitted messages",
                        )
                    matching_child_indexes = [
                        index
                        for index, message in enumerate(messages)
                        if child_message_matches(message)
                    ]
                    child_indexes = [
                        index for index, message in enumerate(messages) if is_child_message(message)
                    ]
                    if child_message_received:
                        if (
                            child_indexes != matching_child_indexes
                            or len(matching_child_indexes) != 1
                        ):
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent aggregate contained child custody after RLM child cleanup",
                            )
                        aggregate_terminal_start = matching_child_indexes[-1] + 1
                    else:
                        if child_indexes:
                            raise BackendError(
                                "MALFORMED_PROVIDER_OUTPUT",
                                "Prime Agent aggregate contained unexpected child custody",
                            )
                        aggregate_terminal_start = 0
                    if (
                        not prompt_accepted
                        or not (child_message_received or child_snapshot_replied)
                        or (not child_message_received and not child_snapshot_completed)
                    ):
                        continue
                    if cleanup_tool_call_id is None or not cleanup_observed:
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent omitted confirmed RLM child cleanup",
                        )
                    terminal_texts = [
                        text
                        for message in messages[aggregate_terminal_start:]
                        if (text := assistant_text(message)) is not None
                    ]
                    if not terminal_texts:
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC omitted the terminal assistant message",
                        )
                    if len(terminal_texts) != 1:
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent emitted multiple terminal messages after RLM child cleanup",
                        )
                    final_text = terminal_texts[0]
                    if (
                        post_cleanup_terminal_text is None
                        or final_text != post_cleanup_terminal_text
                    ):
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent did not prove a terminal assistant message after RLM child cleanup",
                        )
                    if len(final_text.encode("utf-8")) > self.max_response_bytes:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent final message exceeded byte limit",
                            audit_status="OUTPUT_LIMIT_FINAL_TEXT",
                        )
                    return parse_json_object(final_text)

            async def run_rpc_and_settle() -> dict[str, Any]:
                try:
                    result = await run_rpc()
                    if not rpc_stdin.is_closing():
                        rpc_stdin.close()
                    await _terminate_process(rpc_proc)
                    if not stderr_task.done():
                        stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                    return BackendResult(result, transport_metrics=transport_metrics())
                except BackendError as exc:
                    raise BackendError(
                        exc.code,
                        str(exc),
                        audit_status=exc.audit_status,
                        transport_metrics=transport_metrics(),
                    ) from exc

            try:
                return await asyncio.wait_for(run_rpc_and_settle(), timeout=self.timeout)
            except TimeoutError as exc:
                raise BackendError(
                    "BACKEND_TIMEOUT",
                    "Prime Agent RPC timed out",
                    transport_metrics=transport_metrics(),
                ) from exc
        finally:
            if proc is not None:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
                await _terminate_process(proc)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            session_dir.cleanup()


@dataclass(frozen=True)
class BackendInvocationIdentity:
    backend: str
    model: str
    used_fallback: bool
    primary_error_code: str | None = None
    call_id: int | None = None
    call_settled: bool = False


class _PrimaryCircuitOpen(Exception):
    pass


class FailoverBackend:
    """Enforce one durable, route-aware physical-call budget."""

    _TRANSIENT_CODES = frozenset({"PROVIDER_ERROR", "RATE_LIMITED"})
    _FALLBACK_CODES = frozenset(
        {"PROVIDER_ERROR", "RATE_LIMITED", "BACKEND_TIMEOUT", "UNCLASSIFIED_ERROR"}
    )
    _TERMINAL_CODES = frozenset(
        {
            "OUTPUT_LIMIT_EXCEEDED",
            "DEADLINE_EXCEEDED",
            "DEADLINE_INSUFFICIENT",
            "SOFT_DEADLINE_REACHED",
            "CALL_BUDGET_EXHAUSTED",
            "STALE_ATTEMPT",
            "CANCELLED",
            "CONTEXT_LIMIT_EXCEEDED",
            "INVALID_REQUEST",
            "UNSUPPORTED_PHASE",
            "PROVIDER_AUDIT_ERROR",
        }
    )
    _MAX_PHYSICAL_CALLS = 3

    @classmethod
    def _normalized_error_code(cls, code: str) -> str:
        known = cls._TERMINAL_CODES | cls._FALLBACK_CODES | {"MALFORMED_PROVIDER_OUTPUT"}
        return code if code in known else "UNCLASSIFIED_ERROR"

    def __init__(
        self,
        primary: RolloutBackend,
        fallback: RolloutBackend,
        *,
        primary_timeout_seconds: float = 90,
        fallback_timeout_seconds: float = 180,
        retry_delay_seconds: tuple[float, float] = (1, 3),
        fast_transient_seconds: float = 30,
    ) -> None:
        if primary_timeout_seconds <= 0 or fallback_timeout_seconds <= 0:
            raise ValueError("failover route timeouts must be positive")
        if (
            len(retry_delay_seconds) != 2
            or retry_delay_seconds[0] < 0
            or retry_delay_seconds[1] < retry_delay_seconds[0]
        ):
            raise ValueError("retry delay range is invalid")
        if fast_transient_seconds <= 0:
            raise ValueError("fast transient threshold must be positive")
        self.primary = primary
        self.fallback = fallback
        self.primary_timeout_seconds = primary_timeout_seconds
        self.fallback_timeout_seconds = fallback_timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.fast_transient_seconds = fast_transient_seconds
        self.name = self._join_identity(primary.name, fallback.name, "backend")
        self.model = self._join_identity(primary.model, fallback.model, "model")
        self._identities: dict[tuple[str, str, str | None, str], BackendInvocationIdentity] = {}
        self._attempt_admission_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )

    @staticmethod
    def _join_identity(primary: str, fallback: str, label: str) -> str:
        if type(primary) is not str or type(fallback) is not str or not primary or not fallback:
            raise ValueError(f"failover {label} identities must be nonempty strings")
        value = f"{primary}->{fallback}"
        if len(value) > 256:
            raise ValueError(f"failover {label} identity exceeds 256 characters")
        return value

    @staticmethod
    def _request_key(request: BackendRequestV1) -> tuple[str, str, str | None, str]:
        return (request.attempt_id, request.phase, request.branch_id, request.correlation_id)

    def _record(
        self,
        request: BackendRequestV1,
        route: RolloutBackend,
        *,
        used_fallback: bool,
        primary_error_code: str | None = None,
        call_id: int | None = None,
        call_settled: bool = False,
    ) -> None:
        self._identities[self._request_key(request)] = BackendInvocationIdentity(
            route.name,
            route.model,
            used_fallback,
            primary_error_code,
            call_id,
            call_settled,
        )

    def take_invocation_identity(self, request: BackendRequestV1) -> BackendInvocationIdentity:
        return self._identities.pop(
            self._request_key(request),
            BackendInvocationIdentity(self.primary.name, self.primary.model, False),
        )

    async def _invoke_route(
        self,
        route: RolloutBackend,
        request: BackendRequestV1,
        audit: ProviderInvocationAudit | None,
        *,
        route_role: str,
        effective_timeout_seconds: float,
        used_fallback: bool,
        primary_error_code: str | None = None,
    ) -> dict[str, Any]:
        if route_role == "primary":
            if route is not self.primary:
                raise BackendError("INVALID_REQUEST", "primary route identity is inconsistent")
            circuit_admission = True
        elif route_role == "fallback":
            if route is not self.fallback:
                raise BackendError("INVALID_REQUEST", "fallback route identity is inconsistent")
            circuit_admission = False
        else:
            raise BackendError("INVALID_REQUEST", "failover route role is invalid")
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining < effective_timeout_seconds:
            raise BackendError(
                "DEADLINE_INSUFFICIENT",
                "remaining deadline cannot cover the configured route timeout",
            )
        call_id: int | None = None

        def admit() -> None:
            nonlocal call_id
            if circuit_admission and audit is not None:
                if self._circuit_score(self._audit_rows(audit, "attempt_history", request)) >= 2:
                    raise _PrimaryCircuitOpen
            if audit is not None:
                try:
                    call_id = audit.start(
                        request,
                        route.name,
                        route.model,
                        route_role=route_role,
                        effective_timeout_seconds=effective_timeout_seconds,
                    )
                except TypeError:
                    # Compatibility for third-party audit adapters written before v0.2.5.
                    call_id = audit.start(request, route.name, route.model)
            self._record(
                request,
                route,
                used_fallback=used_fallback,
                primary_error_code=primary_error_code,
                call_id=call_id,
            )

        if circuit_admission and audit is not None:
            admission_lock = self._attempt_admission_locks.get(request.attempt_id)
            if admission_lock is None:
                admission_lock = asyncio.Lock()
                self._attempt_admission_locks[request.attempt_id] = admission_lock
            async with admission_lock:
                admit()
        else:
            admit()
        try:
            return await route.invoke(request)
        except asyncio.CancelledError:
            if audit is not None and call_id is not None:
                try:
                    admitted = audit.finish(call_id, request, "cancelled", error_code="CANCELLED")
                except TypeError:
                    admitted = audit.finish(call_id, request, "cancelled")
                self._record(
                    request,
                    route,
                    used_fallback=used_fallback,
                    primary_error_code=primary_error_code,
                    call_id=call_id,
                    call_settled=admitted,
                )
            raise
        except BackendError as exc:
            if audit is not None and call_id is not None:
                try:
                    admitted = audit.finish(
                        call_id,
                        request,
                        exc.audit_status,
                        error_code=self._normalized_error_code(exc.code),
                        transport_metrics=exc.transport_metrics,
                    )
                except TypeError:
                    try:
                        admitted = audit.finish(
                            call_id,
                            request,
                            exc.audit_status,
                            error_code=self._normalized_error_code(exc.code),
                        )
                    except TypeError:
                        admitted = audit.finish(call_id, request, exc.audit_status)
                self._record(
                    request,
                    route,
                    used_fallback=used_fallback,
                    primary_error_code=primary_error_code,
                    call_id=call_id,
                    call_settled=admitted,
                )
                if not admitted:
                    raise BackendError("STALE_ATTEMPT", "attempt is no longer current") from None
            raise

    @staticmethod
    def _audit_rows(
        audit: ProviderInvocationAudit | None,
        method: str,
        request: BackendRequestV1,
    ) -> list[Any]:
        reader = getattr(audit, method, None)
        if not callable(reader):
            return []
        rows = reader(request)
        return list(rows) if rows is not None else []

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        try:
            return row[key]
        except (KeyError, TypeError, IndexError):
            return getattr(row, key, None)

    @classmethod
    def _row_duration(cls, row: Any) -> float | None:
        started = cls._row_value(row, "started_at")
        ended = cls._row_value(row, "ended_at")
        try:
            return (
                datetime.fromisoformat(str(ended)) - datetime.fromisoformat(str(started))
            ).total_seconds()
        except (TypeError, ValueError):
            return None

    def _circuit_score(self, rows: list[Any]) -> int:
        score = 0
        for row in rows:
            if self._row_value(row, "route_role") != "primary":
                continue
            code = self._row_value(row, "error_code")
            if code == "BACKEND_TIMEOUT":
                score += 2
            elif code == "PROVIDER_ERROR":
                duration = self._row_duration(row)
                if duration is not None and duration <= self.fast_transient_seconds:
                    score += 1
        return score

    async def _invoke(
        self, request: BackendRequestV1, audit: ProviderInvocationAudit | None
    ) -> dict[str, Any]:
        phase_rows = self._audit_rows(audit, "history", request)
        attempt_rows = self._audit_rows(audit, "attempt_history", request)
        calls_used = len(phase_rows)
        if calls_used >= self._MAX_PHYSICAL_CALLS:
            raise BackendError("CALL_BUDGET_EXHAUSTED", "physical call budget exhausted")

        last = phase_rows[-1] if phase_rows else None
        if last is not None and self._row_value(last, "error_code") == "MALFORMED_PROVIDER_OUTPUT":
            role = self._row_value(last, "route_role")
            route = self.fallback if role == "fallback" else self.primary
            timeout = (
                self.fallback_timeout_seconds
                if role == "fallback"
                else self.primary_timeout_seconds
            )
            try:
                return await self._invoke_route(
                    route,
                    request,
                    audit,
                    route_role=str(role),
                    effective_timeout_seconds=timeout,
                    used_fallback=role == "fallback",
                )
            except _PrimaryCircuitOpen:
                raise BackendError(
                    "MALFORMED_PROVIDER_OUTPUT",
                    "producer-affine repair cannot start after the primary circuit opens",
                ) from None

        circuit_open = self._circuit_score(attempt_rows) >= 2
        primary_error_code: str | None = None

        async def call_primary() -> dict[str, Any]:
            return await self._invoke_route(
                self.primary,
                request,
                audit,
                route_role="primary",
                effective_timeout_seconds=self.primary_timeout_seconds,
                used_fallback=False,
            )

        async def call_fallback() -> dict[str, Any]:
            nonlocal calls_used
            if calls_used >= self._MAX_PHYSICAL_CALLS:
                raise BackendError("CALL_BUDGET_EXHAUSTED", "physical call budget exhausted")
            calls_used += 1
            return await self._invoke_route(
                self.fallback,
                request,
                audit,
                route_role="fallback",
                effective_timeout_seconds=self.fallback_timeout_seconds,
                used_fallback=True,
                primary_error_code=primary_error_code,
            )

        if circuit_open:
            return await call_fallback()

        calls_used += 1
        primary_started = time.monotonic()
        try:
            return await call_primary()
        except _PrimaryCircuitOpen:
            calls_used -= 1
            primary_error_code = "PROVIDER_ERROR"
            return await call_fallback()
        except BackendError as exc:
            primary_duration = max(0.0, time.monotonic() - primary_started)
            current_rows = self._audit_rows(audit, "history", request)
            if current_rows:
                audited_duration = self._row_duration(current_rows[-1])
                if audited_duration is not None:
                    primary_duration = audited_duration
            primary_error_code = exc.code
            if exc.code in self._TERMINAL_CODES or exc.code == "MALFORMED_PROVIDER_OUTPUT":
                raise
            retryable = exc.code == "RATE_LIMITED" or (
                exc.code == "PROVIDER_ERROR" and primary_duration <= self.fast_transient_seconds
            )
            if retryable and calls_used < self._MAX_PHYSICAL_CALLS:
                await asyncio.sleep(random.uniform(*self.retry_delay_seconds))
                calls_used += 1
                try:
                    return await call_primary()
                except _PrimaryCircuitOpen:
                    calls_used -= 1
                except BackendError as retry_exc:
                    primary_error_code = retry_exc.code
                    if (
                        retry_exc.code in self._TERMINAL_CODES
                        or retry_exc.code == "MALFORMED_PROVIDER_OUTPUT"
                    ):
                        raise
                    if retry_exc.code not in self._FALLBACK_CODES:
                        primary_error_code = "UNCLASSIFIED_ERROR"
            elif exc.code not in self._FALLBACK_CODES:
                primary_error_code = "UNCLASSIFIED_ERROR"
            return await call_fallback()

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        return await self._invoke(request, None)

    async def invoke_with_audit(
        self, request: BackendRequestV1, audit: ProviderInvocationAudit
    ) -> dict[str, Any]:
        return await self._invoke(request, audit)


def parse_json_object(raw: str) -> dict[str, Any]:
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().lower() in {"```", "```json"}
            and lines[-1].strip() == "```"
        ):
            value = "\n".join(lines[1:-1]).strip()
        else:
            raise BackendError("MALFORMED_PROVIDER_OUTPUT", "provider output fence is invalid")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BackendError(
            "MALFORMED_PROVIDER_OUTPUT", "provider output is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise BackendError("MALFORMED_PROVIDER_OUTPUT", "provider output must be a JSON object")
    return parsed
