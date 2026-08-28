from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from .ports import BackendError, backend_input, provider_payload
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


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise BackendError("OUTPUT_LIMIT_EXCEEDED", "provider output exceeded byte limit")
        chunks.append(chunk)


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

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        payload = provider_payload(request)
        target_output_bytes = min(self.max_response_bytes, self.max_output_tokens, 7000)
        payload["instruction"] += (
            f" Keep the complete JSON under {target_output_bytes} UTF-8 bytes; "
            "be concise, emit no prose, and include only schema fields."
        )
        prompt = json.dumps(payload, ensure_ascii=False)
        if len(prompt.encode("utf-8")) > 65536:
            raise BackendError(
                "CONTEXT_LIMIT_EXCEEDED",
                "Prime Agent prompt exceeds the safe argv byte limit (65536)",
            )
        argv = [
            self.executable,
            "--print",
            "--mode",
            "text",
            "--no-tools",
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
        ]
        if self.provider:
            argv += ["--provider", self.provider]
        if self.configured_model:
            argv += ["--model", self.configured_model]
        if self.thinking:
            argv += ["--thinking", self.thinking]
        argv.extend(["--", prompt])
        encoded_sizes = [len(argument.encode("utf-8")) for argument in argv]
        if (
            any(size > 65536 for size in encoded_sizes)
            or sum(size + 1 for size in encoded_sizes) > 65536
        ):
            raise BackendError(
                "CONTEXT_LIMIT_EXCEEDED",
                "Prime Agent argv exceeds the safe aggregate byte limit (65536)",
            )
        proc: asyncio.subprocess.Process | None = None
        reader_tasks: list[asyncio.Task[bytes]] = []
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            assert proc.stdout is not None and proc.stderr is not None
            # Prime Agent has no supported max-output-tokens CLI flag. Enforce a
            # conservative UTF-8 byte ceiling no larger than the configured
            # token ceiling; this may stop early, but can never exceed policy.
            stdout_limit = min(self.max_response_bytes, self.max_output_tokens)
            stdout_task = asyncio.create_task(_read_limited(proc.stdout, stdout_limit))
            stderr_task = asyncio.create_task(
                _read_limited(proc.stderr, min(self.max_response_bytes, 65536))
            )
            reader_tasks = [stdout_task, stderr_task]
            try:
                stdout, _ = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task), timeout=self.timeout
                )
                returncode = await proc.wait()
            except BaseException as exc:
                # This covers cancellation, timeout, stream overflow, and reader errors.
                # Always terminate first; _terminate_process escalates after grace.
                await _terminate_process(proc)
                for task in reader_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*reader_tasks, return_exceptions=True)
                if isinstance(exc, TimeoutError):
                    raise BackendError("BACKEND_TIMEOUT", "Prime Agent timed out") from exc
                raise
            if returncode != 0:
                raise BackendError("PROVIDER_ERROR", "Prime Agent exited unsuccessfully")
            try:
                decoded = stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackendError(
                    "MALFORMED_PROVIDER_OUTPUT", "Prime Agent output was not valid UTF-8"
                ) from exc
            return parse_json_object(decoded)
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate_process(proc)
            for task in reader_tasks:
                if not task.done():
                    task.cancel()
            if reader_tasks:
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            raise


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
