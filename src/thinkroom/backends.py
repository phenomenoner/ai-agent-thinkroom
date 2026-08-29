from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from .ports import (
    BackendError,
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
                    raise BackendError(
                        "PROVIDER_ERROR", f"provider returned HTTP {response.status_code}"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED", "provider response exceeded byte limit"
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
        target_output_bytes = min(self.max_response_bytes, self.max_output_tokens, 7000)
        payload["instruction"] += (
            f" Keep the complete JSON under {target_output_bytes} UTF-8 bytes; "
            "be concise, emit no prose, and include only schema fields."
        )
        child_name = f"thinkroom-{request.phase}-worker"
        provider_request = json.dumps(payload, ensure_ascii=False)
        prompt = (
            "Act only as a structured Thinkroom research provider. Do not read or write files, "
            "run shell commands, or perform external effects. Use the persistent IPython kernel "
            "and call the preloaded rlm exactly once. Name the child "
            f"{child_name}. Give that child the complete PROVIDER_REQUEST_JSON below and instruct "
            "it to solve the requested research phase independently, then send its findings to "
            "the parent with agent_message.send(receiver_role='parent'). End the first turn after "
            "showing the admission handle. Do not invent or infer the child response. Only after a "
            "real child agent_message arrives, synthesize and return exactly one JSON object that "
            "matches output_json_schema, with no markdown or prose.\n\n"
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
                limit=max(self.max_response_bytes, 65536),
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

            async def run_rpc() -> dict[str, Any]:
                raw_transport_bytes = 0
                retained_event_bytes = 0
                event_count = 0
                prompt_accepted = False
                child_message_received = False

                def child_message_matches(message: object) -> bool:
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
                        and sender.get("sessionName") == child_name
                    )

                def assistant_text(message: object) -> str | None:
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        return None
                    if message.get("stopReason") not in {None, "stop"}:
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
                    raw_transport_bytes += len(line)
                    if raw_transport_bytes > _PRIME_RPC_RAW_BYTE_LIMIT:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent RPC raw transport exceeded byte limit",
                        )
                    event_count += 1
                    if event_count > _PRIME_RPC_EVENT_COUNT_LIMIT:
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent RPC exceeded the event-count limit",
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
                    if event.get("type") in {"response", "message_end", "agent_end"}:
                        retained_event_bytes += len(line)
                        if retained_event_bytes > self.max_response_bytes:
                            raise BackendError(
                                "OUTPUT_LIMIT_EXCEEDED",
                                "Prime Agent RPC retained events exceeded byte limit",
                            )
                    if event.get("type") == "response" and event.get("id") == "thinkroom-provider":
                        if event.get("command") != "prompt" or event.get("success") is not True:
                            raise BackendError(
                                "PROVIDER_ERROR", "Prime Agent RPC rejected the provider prompt"
                            )
                        prompt_accepted = True
                        continue
                    if event.get("type") == "message_end":
                        child_message_received = child_message_received or child_message_matches(
                            event.get("message")
                        )
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
                    child_message_received = child_message_received or bool(matching_child_indexes)
                    if (
                        not prompt_accepted
                        or not child_message_received
                        or not matching_child_indexes
                    ):
                        continue
                    final_text = next(
                        (
                            text
                            for message in reversed(messages[matching_child_indexes[-1] + 1 :])
                            if (text := assistant_text(message)) is not None
                        ),
                        None,
                    )
                    if final_text is None:
                        raise BackendError(
                            "MALFORMED_PROVIDER_OUTPUT",
                            "Prime Agent RPC omitted the terminal assistant message",
                        )
                    if len(final_text.encode("utf-8")) > min(
                        self.max_response_bytes, self.max_output_tokens
                    ):
                        raise BackendError(
                            "OUTPUT_LIMIT_EXCEEDED",
                            "Prime Agent final message exceeded byte limit",
                        )
                    return parse_json_object(final_text)

            async def run_rpc_and_settle() -> dict[str, Any]:
                result = await run_rpc()
                if not rpc_stdin.is_closing():
                    rpc_stdin.close()
                await _terminate_process(rpc_proc)
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                return result

            try:
                return await asyncio.wait_for(run_rpc_and_settle(), timeout=self.timeout)
            except TimeoutError as exc:
                raise BackendError("BACKEND_TIMEOUT", "Prime Agent RPC timed out") from exc
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


class FailoverBackend:
    """Try one physical backend, then one bounded availability fallback."""

    _FALLBACK_CODES = frozenset({"PROVIDER_ERROR", "BACKEND_TIMEOUT"})

    def __init__(self, primary: RolloutBackend, fallback: RolloutBackend) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = self._join_identity(primary.name, fallback.name, "backend")
        self.model = self._join_identity(primary.model, fallback.model, "model")
        self._identities: dict[tuple[str, str, str | None, str], BackendInvocationIdentity] = {}

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
        used_fallback: bool,
        primary_error_code: str | None = None,
    ) -> dict[str, Any]:
        call_id = audit.start(request, route.name, route.model) if audit is not None else None
        self._record(
            request,
            route,
            used_fallback=used_fallback,
            primary_error_code=primary_error_code,
            call_id=call_id,
        )
        try:
            return await route.invoke(request)
        except asyncio.CancelledError:
            if audit is not None and call_id is not None:
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
                admitted = audit.finish(call_id, request, exc.code)
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

    async def _invoke(
        self, request: BackendRequestV1, audit: ProviderInvocationAudit | None
    ) -> dict[str, Any]:
        try:
            return await self._invoke_route(self.primary, request, audit, used_fallback=False)
        except BackendError as exc:
            if exc.code not in self._FALLBACK_CODES:
                raise
            if request.deadline <= datetime.now(UTC):
                raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded") from None
            return await self._invoke_route(
                self.fallback,
                request,
                audit,
                used_fallback=True,
                primary_error_code=exc.code,
            )

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
