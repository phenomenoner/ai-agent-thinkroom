from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import ValidationError

from .config import Settings
from .packs import get_pack
from .ports import BackendError, ResearchRepository, RolloutBackend, provider_payload
from .schemas import (
    MAX_SYNTHESIS_EVIDENCE_ITEMS,
    BackendRequestV1,
    BranchOutputV1,
    CritiqueOutputV1,
    EvidenceLedgerItemV1,
    EvidenceStatus,
    EvidenceV1,
    ForkOutputV1,
    FrameOutputV1,
    JobState,
    ResearchRequest,
    SynthesisOutputV1,
)
from .strategies import STRATEGIES

log = logging.getLogger("thinkroom.engine")
PHASE_MODELS: dict[str, type[Any]] = {
    "frame": FrameOutputV1,
    "fork": ForkOutputV1,
    "rollout": BranchOutputV1,
    "critique": CritiqueOutputV1,
    "synthesis": SynthesisOutputV1,
}

_PROVIDER_ERROR_MESSAGES = {
    "BACKEND_TIMEOUT": "backend invocation timed out",
    "CONTEXT_LIMIT_EXCEEDED": "provider request exceeds configured limit",
    "INVALID_REQUEST": "provider request is invalid",
    "MALFORMED_PROVIDER_OUTPUT": "provider output was not valid",
    "OUTPUT_LIMIT_EXCEEDED": "provider response exceeded configured limit",
    "PROVIDER_ERROR": "provider invocation failed",
    "UNSUPPORTED_PHASE": "provider phase is unsupported",
}


def model_dict(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def safe_exception_details(
    exc: BaseException,
    *,
    default_code: str = "INTERNAL_ERROR",
    default_message: str = "job failed",
    include_validation_message: bool = False,
) -> tuple[str, str]:
    message = default_message
    if type(exc) is BackendError or (
        include_validation_message and isinstance(exc, ValidationError)
    ):
        try:
            message = str(exc)
        except BaseException:
            message = default_message
    if type(message) is not str or not message:
        message = default_message
    message = (
        message.encode("utf-8", errors="replace")[:4000].decode("utf-8", errors="ignore")
        or default_message
    )
    candidate: object = None
    if type(exc) is BackendError:
        try:
            candidate = exc.code
        except BaseException:
            candidate = None
    code = candidate if type(candidate) is str and 1 <= len(candidate) <= 128 else default_code
    if type(exc) is RuntimeError and exc.args == ("ARTIFACT_LIMIT_EXCEEDED",):
        code = "ARTIFACT_LIMIT_EXCEEDED"
        message = "job persisted-byte limit exceeded"
    return code, message


class _RepositoryProviderInvocationAudit:
    def __init__(self, repo: Any, retry_index: int) -> None:
        self.repo = repo
        self.retry_index = retry_index

    def start(self, request: BackendRequestV1, backend: str, model: str) -> int:
        return self.repo.add_provider_call(
            {
                "job_id": request.job_id,
                "attempt_id": request.attempt_id,
                "phase": request.phase,
                "branch_id": request.branch_id,
                "prompt_version": request.prompt_version,
                "backend": backend,
                "model": model,
                "started_at": datetime.now(UTC).isoformat(),
                "retry_index": self.retry_index,
                "output_status": "started",
                "output_size": 0,
            }
        )

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
    ) -> bool:
        if output_status == "cancelled":
            return self.repo.settle_cancelled_provider_call(
                call_id,
                request.attempt_id,
                ended_at=datetime.now(UTC).isoformat(),
                output_size=output_size,
            )
        return self.repo.finish_provider_call(
            call_id,
            request.attempt_id,
            ended_at=datetime.now(UTC).isoformat(),
            output_status=output_status,
            output_size=output_size,
        )


class _ProviderBoundaryFailure(RuntimeError):
    pass


def normalize_provider_exception(exc: BaseException) -> Exception:
    """Translate an untrusted backend exception into the core-owned taxonomy."""
    if type(exc) is BackendError and type(exc.code) is str:
        message = _PROVIDER_ERROR_MESSAGES.get(exc.code)
        if message is not None:
            return BackendError(exc.code, message)
    return _ProviderBoundaryFailure()


class ResearchEngine:
    def __init__(
        self,
        repository: ResearchRepository,
        backend: RolloutBackend,
        settings: Settings,
        evidence_verifier: Callable[[EvidenceV1], str | None] | None = None,
    ) -> None:
        self.repo: Any = repository
        self._backend = backend
        self.settings = settings
        # Provider assertions are never trusted by default. Deployments may inject
        # an independent verifier for local artifacts or authoritative records.
        self.evidence_verifier = evidence_verifier
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}
        self._detached_provider_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def backend(self) -> RolloutBackend:
        return self._backend

    @property
    def provider_capacity_healthy(self) -> bool:
        return not self._detached_provider_tasks

    def _take_provider_identity(
        self, request: BackendRequestV1
    ) -> tuple[str, str, str | None, int | None, bool]:
        take_identity = getattr(self.backend, "take_invocation_identity", None)
        if callable(take_identity):
            identity = take_identity(request)
            backend = getattr(identity, "backend", None)
            model = getattr(identity, "model", None)
            primary_error_code = getattr(identity, "primary_error_code", None)
            call_id = getattr(identity, "call_id", None)
            call_settled = getattr(identity, "call_settled", False)
            if (
                type(backend) is str
                and type(model) is str
                and (primary_error_code is None or type(primary_error_code) is str)
                and (call_id is None or type(call_id) is int)
                and type(call_settled) is bool
            ):
                return backend, model, primary_error_code, call_id, call_settled
        return self.backend.name, getattr(self.backend, "model", "unknown"), None, None, False

    @staticmethod
    def request_hash(request: ResearchRequest) -> str:
        raw = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    async def start(self) -> None:
        if getattr(self.repo, "db", None) is None:
            self.repo.open()
        self._stopping = False
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"thinkroom-worker-{i}")
            for i in range(self.settings.max_concurrency)
        ]
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        active = tuple(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        await self._reap_detached_provider_tasks()
        self._workers.clear()
        self._active.clear()

    async def _reap_detached_provider_tasks(self) -> None:
        """Bound shutdown; production provider code is process-contained."""
        tasks = tuple(self._detached_provider_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=2.0)
        if pending:
            raise RuntimeError("provider task did not stop")
        await asyncio.gather(*tasks, return_exceptions=True)

    def _detach_provider_task(self, task: asyncio.Task[dict[str, Any]]) -> None:
        """Fence a noncooperative provider while retaining its concurrency lease."""
        self._detached_provider_tasks.add(task)

        def completed(done: asyncio.Task[dict[str, Any]]) -> None:
            self._detached_provider_tasks.discard(done)
            self._semaphore.release()
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException:
                    pass

        task.add_done_callback(completed)

    async def _invoke_provider_bounded(
        self, request: BackendRequestV1, deadline: datetime, retry_index: int
    ) -> dict[str, Any]:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded")
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except TimeoutError:
            raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded") from None

        invoke_with_audit = getattr(self.backend, "invoke_with_audit", None)
        if callable(invoke_with_audit):
            audit = _RepositoryProviderInvocationAudit(self.repo, retry_index)
            invocation = cast(Any, invoke_with_audit)(request, audit)
        else:
            invocation = self.backend.invoke(request)
        task: asyncio.Task[dict[str, Any]] = asyncio.create_task(invocation)
        detached = False
        try:
            remaining_after_acquire = (deadline - datetime.now(UTC)).total_seconds()
            if remaining_after_acquire <= 0:
                task.cancel()
                self._detach_provider_task(task)
                detached = True
                raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded")
            deadline_limited = remaining_after_acquire <= self.settings.backend_timeout_seconds
            timeout = min(self.settings.backend_timeout_seconds, remaining_after_acquire)
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                task.cancel()
                self._detach_provider_task(task)
                detached = True
                if deadline_limited:
                    raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded")
                raise BackendError("BACKEND_TIMEOUT", "backend invocation timed out")
            if task.cancelled():
                raise BackendError("PROVIDER_ERROR", "provider invocation failed")
            try:
                return task.result()
            except Exception as exc:
                raise normalize_provider_exception(exc) from None
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                self._detach_provider_task(task)
                detached = True
            raise
        finally:
            if not detached:
                self._semaphore.release()

    async def _worker(self, worker_id: int) -> None:
        while not self._stopping:
            try:
                claim = self.repo.claim_next_job(
                    self.settings.max_job_attempts,
                    f"worker-{worker_id}",
                    getattr(self.backend, "name", "unknown"),
                    getattr(self.backend, "model", "unknown"),
                )
                if claim is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                    continue
                job_id, aid = claim
                job_task = asyncio.create_task(
                    self.run(job_id, aid), name=f"thinkroom-job-{job_id}"
                )
                self._active[job_id] = job_task
                try:
                    await job_task
                except asyncio.CancelledError:
                    # Cancellation is directed at the job task, not the worker loop.
                    if not self._stopping:
                        self._settle_requested_cancellation(
                            job_id, aid, f"worker-{worker_id}-prestart-cancel"
                        )
                finally:
                    self._active.pop(job_id, None)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("worker failed", extra={"worker_id": worker_id})

    async def submit(
        self, request: ResearchRequest, idem_key: str | None = None
    ) -> tuple[str, bool]:
        if request.strategy not in STRATEGIES:
            raise ValueError("INVALID_STRATEGY")
        request_bytes = len(
            json.dumps(
                request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if request_bytes > self.settings.max_context_bytes:
            raise BackendError(
                "CONTEXT_LIMIT_EXCEEDED",
                f"request exceeds configured context byte limit ({self.settings.max_context_bytes})",
            )
        if getattr(self.repo, "db", None) is None:
            self.repo.open()
        if not self._workers:
            await self.start()
        now = datetime.now(UTC)
        configured = now + timedelta(seconds=self.settings.job_timeout_seconds)
        deadline = request.deadline
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline is None:
            deadline = now + timedelta(
                seconds=request.deadline_seconds or self.settings.job_timeout_seconds
            )
        deadline = min(deadline, configured)
        request_hash = self.request_hash(request)
        job_id, existing = self.repo.create_job(
            request, request_hash, idem_key, self.settings.max_queued_jobs, deadline
        )
        if not existing:
            self._wake.set()
        return job_id, existing

    async def recover(self) -> None:
        self.repo.recover_startup(self.settings.max_job_attempts)

    async def cancel(self, job_id: str) -> bool:
        ok = self.repo.request_cancel(job_id)
        if ok:
            task = self._active.get(job_id)
            if task is not None:
                task.cancel()
            self._wake.set()
        return ok

    def _settle_requested_cancellation(self, job_id: str, aid: str, correlation: str) -> bool:
        current = self.repo.get_job(job_id)
        if (
            not current
            or current["attempt_id"] != aid
            or current["state"]
            in {s.value for s in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)}
            or not self.repo.is_cancelled(job_id)
        ):
            return False
        return self.repo.settle_terminal(
            job_id,
            JobState.CANCELLED,
            aid,
            "cancelled",
            "cancellation requested",
            correlation,
            "CANCELLED",
            "job cancelled",
        )

    async def run(self, job_id: str, aid: str) -> None:
        row = self.repo.get_job(job_id)
        if not row or row["attempt_id"] != aid:
            return
        correlation = str(uuid.uuid4())
        deadline = datetime.fromisoformat(row["deadline"])
        try:
            await self._run_attempt(job_id, aid, correlation, deadline)
        except asyncio.CancelledError:
            if self._stopping:
                # The active attempt and its durable state are intentionally left recoverable.
                return
            if not self._settle_requested_cancellation(job_id, aid, correlation):
                raise
        except Exception as exc:
            if self._settle_requested_cancellation(job_id, aid, correlation):
                return
            current = self.repo.get_job(job_id)
            if (
                current
                and current["attempt_id"] == aid
                and current["state"]
                not in {s.value for s in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)}
            ):
                code, message = safe_exception_details(exc)
                self.repo.settle_terminal(
                    job_id,
                    JobState.FAILED,
                    aid,
                    "failed",
                    code,
                    correlation,
                    code,
                    message,
                )
                exception_type = (
                    "BackendError"
                    if type(exc) is BackendError
                    else "ValidationError"
                    if isinstance(exc, ValidationError)
                    else "RuntimeError"
                    if isinstance(exc, RuntimeError)
                    else "ValueError"
                    if isinstance(exc, ValueError)
                    else "TypeError"
                    if isinstance(exc, TypeError)
                    else "AssertionError"
                    if isinstance(exc, AssertionError)
                    else "KeyError"
                    if isinstance(exc, KeyError)
                    else "UnexpectedException"
                )
                log.warning(
                    "job failed",
                    extra={
                        "job_id": job_id,
                        "code": code,
                        "exception_type": exception_type,
                    },
                )

    async def _run_attempt(
        self, job_id: str, aid: str, correlation: str, deadline: datetime
    ) -> None:
        row = self.repo.get_job(job_id)
        assert row is not None
        request = ResearchRequest.model_validate_json(row["request_json"])
        pack = get_pack(request.domain)
        strategy = STRATEGIES.get(request.strategy)
        if strategy is None:
            raise BackendError("INVALID_STRATEGY", "unknown strategy")
        await self._guard(job_id, aid, deadline)
        frame = await self._phase(
            "frame",
            job_id,
            aid,
            None,
            {**pack.frame(request), "strategy": request.strategy},
            deadline,
            correlation,
            pack.prompt_version,
        )
        await self._guard(job_id, aid, deadline)
        self.repo.put_artifact(job_id, aid, "frame", frame.model_dump(mode="json"))
        self.repo.transition(job_id, JobState.ROLLING_OUT, aid, "frame complete", correlation)
        fork_input = {
            "frame": frame.model_dump(mode="json"),
            "branch_count": request.branch_count,
            "domain": request.domain,
            "strategy": request.strategy,
            "fallbacks": [p.model_dump(mode="json") for p in strategy(request, pack)],
        }
        warning: str | None
        fork_repair_budget = [1]
        try:
            fork = await self._phase(
                "fork",
                job_id,
                aid,
                None,
                fork_input,
                deadline,
                correlation,
                pack.prompt_version,
                repair_budget=fork_repair_budget,
            )
        except (ValidationError, BackendError) as exc:
            if isinstance(exc, BackendError):
                oversized_schema_repair = (
                    exc.code == "OUTPUT_LIMIT_EXCEEDED" and fork_repair_budget[0] == 0
                )
                if exc.code != "MALFORMED_PROVIDER_OUTPUT" and not oversized_schema_repair:
                    raise
            # _phase already performed the one allowed provider regeneration.
            # Schema-invalid or unparsable fork output must not destroy the
            # whole job. If that invalid output consumed the repair budget, an
            # oversized repair is also contained by the deterministic fallback.
            # Initial output limits and all other transport, deadline, and
            # resource failures remain fatal.
            fallback = ForkOutputV1(perspectives=pack.fallbacks(request.branch_count))
            perspectives = fallback.perspectives
            warning = "provider fork invalid; deterministic fallback used"
        else:
            perspectives, warning = await self._diverse_fork(
                fork,
                request.branch_count,
                fork_input,
                job_id,
                aid,
                deadline,
                correlation,
                pack.prompt_version,
                fork_repair_budget,
                pack,
            )
        await self._guard(job_id, aid, deadline)
        self.repo.put_artifact(
            job_id,
            aid,
            "fork",
            {
                "schema_version": 1,
                "perspectives": [p.model_dump(mode="json") for p in perspectives],
                "provenance_warning": warning,
            },
        )

        async def rollout(perspective: Any) -> tuple[str, BranchOutputV1 | None, str | None]:
            bid = f"branch-{perspective.id}"
            try:
                result = await self._phase(
                    "rollout",
                    job_id,
                    aid,
                    bid,
                    {
                        "frame": frame.model_dump(mode="json"),
                        "perspective": perspective.model_dump(mode="json"),
                        "context": request.context or "",
                        "domain_guidance": pack.guidance,
                        "safety": pack.safety,
                    },
                    deadline,
                    correlation,
                    pack.prompt_version,
                )
                result = self._normalize_evidence(result)
                await self._guard(job_id, aid, deadline)
                self.repo.put_artifact(
                    job_id, aid, "branch", result.model_dump(mode="json"), bid, "succeeded"
                )
                return bid, result, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, message = safe_exception_details(exc)
                if code == "ARTIFACT_LIMIT_EXCEEDED":
                    raise
                self.repo.put_artifact(job_id, aid, "branch", {}, bid, "failed", message)
                return bid, None, message

        raw_outcomes = await asyncio.gather(
            *(rollout(p) for p in perspectives), return_exceptions=True
        )
        for outcome in raw_outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        outcomes = cast(list[tuple[str, BranchOutputV1 | None, str | None]], raw_outcomes)
        await self._guard(job_id, aid, deadline)
        successful = [(bid, result) for bid, result, _ in outcomes if result is not None]
        failed = [{"branch_id": bid, "error": error} for bid, _, error in outcomes if error]
        if not successful:
            raise BackendError("NO_SUCCESSFUL_BRANCHES", "all branch rollouts failed")
        self.repo.transition(job_id, JobState.CRITIQUING, aid, "all branches terminal", correlation)
        successful_ids = [bid for bid, _ in successful]
        critique_input = {
            "successful_branches": [
                {"branch_id": bid, "output": result.model_dump(mode="json")}
                for bid, result in successful
            ],
            "successful_branch_ids": successful_ids,
            "failed_branches": failed,
        }
        critique_repair_budget = [1]
        critique = await self._phase(
            "critique",
            job_id,
            aid,
            None,
            critique_input,
            deadline,
            correlation,
            pack.prompt_version,
            repair_budget=critique_repair_budget,
        )
        if (
            set(critique.consumed_branch_ids) != set(successful_ids)
            or len(critique.consumed_branch_ids) != len(successful_ids)
            or {a.branch_id for a in critique.branch_assessments} != set(successful_ids)
            or len(critique.branch_assessments) != len(successful_ids)
        ):
            if critique_repair_budget[0] <= 0:
                raise BackendError("INVALID_PROVENANCE", "critique branch coverage is incomplete")
            critique_repair_budget[0] -= 1
            critique = await self._phase(
                "critique",
                job_id,
                aid,
                None,
                {
                    **critique_input,
                    "validation_feedback": "Use every successful_branch_id exactly once in consumed_branch_ids and branch_assessments; use no other branch IDs.",
                },
                deadline,
                correlation,
                pack.prompt_version,
                repair_budget=critique_repair_budget,
                retry_offset=1,
            )
            if (
                set(critique.consumed_branch_ids) != set(successful_ids)
                or len(critique.consumed_branch_ids) != len(successful_ids)
                or {a.branch_id for a in critique.branch_assessments} != set(successful_ids)
                or len(critique.branch_assessments) != len(successful_ids)
            ):
                raise BackendError("INVALID_PROVENANCE", "critique branch coverage is incomplete")
        await self._guard(job_id, aid, deadline)
        critique_artifact_id = self.repo.put_artifact(
            job_id, aid, "critique", critique.model_dump(mode="json")
        )
        critique_id = f"critique-{critique_artifact_id}"
        self.repo.transition(job_id, JobState.SYNTHESIZING, aid, "critique complete", correlation)
        synthesis_input = {
            "frame": frame.model_dump(mode="json"),
            "successful_branches": [
                {"branch_id": bid, "output": result.model_dump(mode="json")}
                for bid, result in successful
            ],
            "failed_branches": failed,
            "critique": critique.model_dump(mode="json"),
            "critique_id": critique_id,
            "successful_branch_ids": successful_ids,
        }
        synthesis_repair_budget = [1]
        synthesis = await self._phase(
            "synthesis",
            job_id,
            aid,
            None,
            synthesis_input,
            deadline,
            correlation,
            pack.prompt_version,
            repair_budget=synthesis_repair_budget,
        )
        try:
            synthesis = self._enforce_provenance(
                synthesis, aid, critique_id, successful_ids, successful
            )
        except BackendError as exc:
            if exc.code != "INVALID_PROVENANCE":
                raise
            if synthesis_repair_budget[0] <= 0:
                raise
            synthesis_repair_budget[0] -= 1
            synthesis = await self._phase(
                "synthesis",
                job_id,
                aid,
                None,
                {
                    **synthesis_input,
                    "validation_feedback": f"Use source_attempt_id {aid!r}, consumed_critique_id {critique_id!r}, consume exactly these branch IDs: {successful_ids!r}, and never upgrade an evidence verification status.",
                },
                deadline,
                correlation,
                pack.prompt_version,
                repair_budget=synthesis_repair_budget,
                retry_offset=1,
            )
            synthesis = self._enforce_provenance(
                synthesis, aid, critique_id, successful_ids, successful
            )
        await self._guard(job_id, aid, deadline)
        self.repo.put_artifact(job_id, aid, "synthesis", synthesis.model_dump(mode="json"))
        await self._guard(job_id, aid, deadline)
        self.repo.settle_terminal(
            job_id, JobState.SUCCEEDED, aid, "succeeded", "complete", correlation
        )

    async def _diverse_fork(
        self,
        fork: ForkOutputV1,
        count: int,
        original: dict[str, Any],
        job_id: str,
        aid: str,
        deadline: datetime,
        correlation: str,
        prompt_version: str,
        repair_budget: list[int],
        pack: Any,
    ) -> tuple[list[Any], str | None]:
        if self._unique(fork) and len(fork.perspectives) == count:
            return fork.perspectives, None
        retry_input = {
            **original,
            "validation_feedback": "Perspectives must be exactly the requested count and have unique IDs plus distinct case-folded title, hypothesis, and approach.",
        }
        if repair_budget[0] > 0:
            repair_budget[0] -= 1
            retry = await self._phase(
                "fork",
                job_id,
                aid,
                None,
                retry_input,
                deadline,
                correlation,
                prompt_version,
                repair_budget=repair_budget,
                retry_offset=1,
            )
        else:
            retry = fork
        if self._unique(retry) and len(retry.perspectives) == count:
            return retry.perspectives, None
        fork = retry
        seen: set[tuple[str, str, str]] = set()
        seen_ids: set[str] = set()
        result: list[Any] = []
        for perspective in fork.perspectives + pack.fallbacks(count):
            key = (
                perspective.title.casefold(),
                perspective.hypothesis.casefold(),
                perspective.approach.casefold(),
            )
            if key not in seen and perspective.id not in seen_ids:
                seen.add(key)
                seen_ids.add(perspective.id)
                result.append(perspective)
            if len(result) == count:
                break
        if len(result) < count:
            raise BackendError("INSUFFICIENT_DIVERSITY", "could not create requested perspectives")
        return (
            result,
            "provider perspectives failed exact diversity; deterministic domain fallback used",
        )

    @staticmethod
    def _unique(fork: ForkOutputV1) -> bool:
        keys = {
            (p.title.casefold(), p.hypothesis.casefold(), p.approach.casefold())
            for p in fork.perspectives
        }
        ids = {perspective.id for perspective in fork.perspectives}
        return len(keys) == len(fork.perspectives) and len(ids) == len(fork.perspectives)

    async def _phase(
        self,
        phase: Literal["frame", "fork", "rollout", "critique", "synthesis"],
        job_id: str,
        aid: str,
        branch_id: str | None,
        input_data: dict[str, Any],
        deadline: datetime,
        correlation: str,
        prompt_version: str,
        *,
        repair_budget: list[int] | None = None,
        retry_offset: int = 0,
    ) -> Any:
        budget = repair_budget if repair_budget is not None else [1]
        request = BackendRequestV1(
            phase=phase,
            job_id=job_id,
            attempt_id=aid,
            branch_id=branch_id,
            prompt_version=prompt_version,
            input=cast(Any, input_data),
            expected_output_schema=PHASE_MODELS[phase].__name__,
            deadline=deadline,
            correlation_id=correlation,
        )
        last: Exception | None = None
        for retry in range(2):
            retry_index = retry_offset + retry
            await self._guard(job_id, aid, deadline)
            physical_audit = callable(getattr(self.backend, "invoke_with_audit", None))
            call_id: int | None = None
            if not physical_audit:
                call_id = self.repo.add_provider_call(
                    {
                        "job_id": job_id,
                        "attempt_id": aid,
                        "phase": phase,
                        "branch_id": branch_id,
                        "prompt_version": prompt_version,
                        "backend": self.backend.name,
                        "model": getattr(self.backend, "model", "unknown"),
                        "started_at": datetime.now(UTC).isoformat(),
                        "retry_index": retry_index,
                        "output_status": "started",
                        "output_size": 0,
                    }
                )
            output_size = 0
            try:
                serialized_input = json.dumps(
                    provider_payload(request), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if len(serialized_input) > self.settings.max_context_bytes:
                    raise BackendError(
                        "CONTEXT_LIMIT_EXCEEDED",
                        "serialized provider input exceeds THINKROOM_MAX_CONTEXT_BYTES",
                    )
                log.info(
                    "provider_invocation_started",
                    extra={
                        "job_id": job_id,
                        "attempt_id": aid,
                        "correlation_id": correlation,
                        "phase": phase,
                        "backend": self.backend.name,
                        "model": getattr(self.backend, "model", "unknown"),
                        "input_bytes": len(serialized_input),
                        "retry_index": retry_index,
                    },
                )
                await self._guard(job_id, aid, deadline)
                raw = await self._invoke_provider_bounded(request, deadline, retry_index)
                await self._guard(job_id, aid, deadline)
                encoded = json.dumps(raw, ensure_ascii=False).encode()
                output_size = len(encoded)
                if output_size > self.settings.max_backend_response_bytes:
                    raise BackendError("OUTPUT_LIMIT_EXCEEDED", "backend response too large")
                result = PHASE_MODELS[phase].model_validate(raw)
                (
                    selected_backend,
                    selected_model,
                    _,
                    route_call_id,
                    call_settled,
                ) = self._take_provider_identity(request)
                final_call_id = route_call_id if route_call_id is not None else call_id
                if final_call_id is None or call_settled:
                    raise BackendError("PROVIDER_AUDIT_ERROR", "provider audit row is unavailable")
                admitted = self.repo.finish_provider_call(
                    final_call_id,
                    aid,
                    ended_at=datetime.now(UTC).isoformat(),
                    output_status="validated",
                    output_size=output_size,
                    backend=selected_backend,
                    model=selected_model,
                )
                if not admitted:
                    await self._guard(job_id, aid, deadline)
                    raise BackendError("STALE_ATTEMPT", "attempt is no longer current")
                log.info(
                    "provider_invocation_finished",
                    extra={
                        "job_id": job_id,
                        "attempt_id": aid,
                        "correlation_id": correlation,
                        "phase": phase,
                        "backend": selected_backend,
                        "model": selected_model,
                        "output_bytes": output_size,
                        "retry_index": retry_index,
                    },
                )
                return result
            except asyncio.CancelledError:
                (
                    selected_backend,
                    selected_model,
                    _,
                    route_call_id,
                    call_settled,
                ) = self._take_provider_identity(request)
                final_call_id = route_call_id if route_call_id is not None else call_id
                if final_call_id is not None and not call_settled:
                    self.repo.settle_cancelled_provider_call(
                        final_call_id,
                        aid,
                        ended_at=datetime.now(UTC).isoformat(),
                        output_size=output_size,
                    )
                raise
            except Exception as exc:
                last = exc
                (
                    selected_backend,
                    selected_model,
                    _,
                    route_call_id,
                    call_settled,
                ) = self._take_provider_identity(request)
                final_call_id = route_call_id if route_call_id is not None else call_id
                output_status, validation_feedback = safe_exception_details(
                    exc,
                    default_code="invalid",
                    default_message="provider output invalid",
                    include_validation_message=True,
                )
                if final_call_id is not None and not call_settled:
                    self.repo.finish_provider_call(
                        final_call_id,
                        aid,
                        ended_at=datetime.now(UTC).isoformat(),
                        output_status=output_status,
                        output_size=output_size,
                        backend=selected_backend,
                        model=selected_model,
                    )
                retryable = isinstance(exc, ValidationError) or (
                    isinstance(exc, BackendError) and exc.code == "MALFORMED_PROVIDER_OUTPUT"
                )
                if retry == 0 and retryable and budget[0] > 0:
                    budget[0] -= 1
                    request = BackendRequestV1.model_validate(
                        {
                            **request.model_dump(mode="json"),
                            "input": {
                                **model_dict(request.input),
                                "validation_feedback": validation_feedback[:4000],
                            },
                        }
                    )
                else:
                    raise
        assert last is not None
        raise last

    async def _guard(self, job_id: str, aid: str, deadline: datetime | None = None) -> None:
        row = self.repo.get_job(job_id)
        if not row or row["attempt_id"] != aid:
            raise BackendError("STALE_ATTEMPT", "attempt is no longer current")
        if self.repo.is_cancelled(job_id):
            raise asyncio.CancelledError
        if deadline is not None and deadline <= datetime.now(UTC):
            raise BackendError("DEADLINE_EXCEEDED", "job deadline exceeded")

    def _normalize_evidence(self, branch: BranchOutputV1) -> BranchOutputV1:
        """Apply system-controlled verification; provider text cannot self-promote."""
        normalized: list[EvidenceV1] = []
        for evidence in branch.supporting_evidence + branch.contradicting_evidence:
            if evidence.verification_status is not EvidenceStatus.VERIFIED:
                normalized.append(evidence)
                continue
            basis = None
            if self.evidence_verifier is not None:
                basis = self.evidence_verifier(evidence)
            if isinstance(basis, str) and basis.strip() and evidence.source_reference:
                normalized.append(
                    EvidenceV1.model_validate(
                        {
                            **evidence.model_dump(mode="python"),
                            "verification_basis": basis[:4000],
                        }
                    )
                )
            else:
                provider_basis = evidence.verification_basis or "no provider basis supplied"
                normalized.append(
                    EvidenceV1.model_validate(
                        {
                            **evidence.model_dump(mode="python"),
                            "verification_status": EvidenceStatus.UNVERIFIED,
                            "verification_basis": (
                                "provider verification assertion rejected; "
                                f"independent trusted provenance unavailable ({provider_basis})"
                            )[:4000],
                            "verification_warning": "provider output cannot establish verified evidence",
                        }
                    )
                )
        split = len(branch.supporting_evidence)
        return BranchOutputV1.model_validate(
            {
                **branch.model_dump(mode="python"),
                "supporting_evidence": normalized[:split],
                "contradicting_evidence": normalized[split:],
            }
        )

    @staticmethod
    def _enforce_provenance(
        synthesis: SynthesisOutputV1,
        aid: str,
        critique_id: str,
        branch_ids: list[str],
        successful: list[tuple[str, BranchOutputV1]],
    ) -> SynthesisOutputV1:
        allowed = set(branch_ids)
        if (
            set(synthesis.consumed_branch_ids) != allowed
            or len(synthesis.consumed_branch_ids) != len(branch_ids)
            or synthesis.source_attempt_id != aid
            or synthesis.consumed_critique_id != critique_id
        ):
            raise BackendError("INVALID_PROVENANCE", "synthesis provenance is incomplete")
        branch_evidence = [
            (bid, out.supporting_evidence + out.contradicting_evidence) for bid, out in successful
        ]
        total_evidence = sum(len(evidence) for _, evidence in branch_evidence)
        if total_evidence == 0:
            raise BackendError("INVALID_PROVENANCE", "no persisted branch evidence is available")

        ledger: list[EvidenceLedgerItemV1] = []
        max_branch_evidence = max(len(evidence) for _, evidence in branch_evidence)
        for index in range(max_branch_evidence):
            for bid, evidence in branch_evidence:
                if index >= len(evidence):
                    continue
                item = evidence[index]
                ledger.append(
                    EvidenceLedgerItemV1(
                        evidence_id=item.id,
                        branch_id=bid,
                        statement=item.statement,
                        verification_status=item.verification_status,
                        provenance=f"branch artifact {bid} evidence {item.id}",
                    )
                )
                if len(ledger) == MAX_SYNTHESIS_EVIDENCE_ITEMS:
                    break
            if len(ledger) == MAX_SYNTHESIS_EVIDENCE_ITEMS:
                break

        updates: dict[str, Any] = {"evidence_ledger": ledger}
        if total_evidence > len(ledger):
            note = (
                f"Evidence ledger projects {len(ledger)} of {total_evidence} persisted branch "
                "evidence items; consult branch artifacts for the complete evidence set."
            )
            uncertainties = list(synthesis.uncertainties[:49])
            uncertainties.append(note)
            updates["uncertainties"] = uncertainties
        return SynthesisOutputV1.model_validate({**synthesis.model_dump(mode="python"), **updates})
