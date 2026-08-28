from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Literal

import httpx

from .config import Settings
from .schemas import ResearchRequest
from .service import ThinkroomService

_SAFE_REMOTE_ERRORS: dict[str, tuple[str, str]] = {
    "PAYLOAD_TOO_LARGE": ("PAYLOAD_TOO_LARGE", "request exceeds configured limit"),
    "INVALID_ARGUMENT": ("INVALID_ARGUMENT", "request is invalid"),
    "INVALID_REQUEST": ("INVALID_REQUEST", "request is invalid"),
    "INVALID_IDEMPOTENCY_KEY": ("INVALID_IDEMPOTENCY_KEY", "request is invalid"),
    "INVALID_CURSOR": ("INVALID_CURSOR", "request is invalid"),
    "INVALID_HOST": ("INVALID_HOST", "request is invalid"),
    "NOT_FOUND": ("NOT_FOUND", "Thinkroom resource not found"),
    "METHOD_NOT_ALLOWED": ("METHOD_NOT_ALLOWED", "method is not allowed"),
    "IDEMPOTENCY_CONFLICT": ("IDEMPOTENCY_CONFLICT", "Thinkroom request conflicts"),
    "RESOURCE_EXHAUSTED": ("RESOURCE_EXHAUSTED", "Thinkroom capacity exhausted"),
    "NOT_READY": ("NOT_READY", "Thinkroom service unavailable"),
    "INTERNAL_ERROR": ("INTERNAL_ERROR", "internal server error"),
    "TRANSPORT_ERROR": ("TRANSPORT_ERROR", "Thinkroom service unavailable"),
}
_SAFE_DETAIL_INTEGERS = frozenset({"limit", "max_bytes", "max_queued_jobs"})
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _safe_remote_details(code: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in _SAFE_DETAIL_INTEGERS:
        item = value.get(key)
        if type(item) is int and 0 <= item <= 2**63 - 1:
            safe[key] = item
    if code != "INVALID_ARGUMENT":
        return safe
    errors = value.get("errors")
    if not isinstance(errors, list):
        return safe
    projected: list[dict[str, Any]] = []
    for item in errors[:50]:
        if not isinstance(item, dict):
            continue
        error_type = item.get("type")
        location = item.get("loc")
        if type(error_type) is not str or _SAFE_IDENTIFIER.fullmatch(error_type) is None:
            continue
        safe_location: list[str | int] = []
        if isinstance(location, list):
            for part in location[:16]:
                if type(part) is int and 0 <= part <= 2**31 - 1:
                    safe_location.append(part)
                elif type(part) is str and _SAFE_IDENTIFIER.fullmatch(part):
                    safe_location.append(part)
        projected.append(
            {"type": error_type, "loc": safe_location, "msg": "input validation failed"}
        )
    if projected:
        safe["errors"] = projected
    return safe


def _project_remote_error(body: object) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(body, dict):
        return "HTTP_ERROR", "Thinkroom request failed", {}
    raw_code = body.get("code")
    if type(raw_code) is not str or raw_code not in _SAFE_REMOTE_ERRORS:
        return "HTTP_ERROR", "Thinkroom request failed", {}
    code, message = _SAFE_REMOTE_ERRORS[raw_code]
    return code, message, _safe_remote_details(code, body.get("details"))


class ThinkroomError(RuntimeError):
    """Typed remote failure preserving the service's error contract."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.status_code = status
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ThinkroomClient:
    def __init__(self, endpoint: str | None = None, timeout: float = 30.0) -> None:
        self.endpoint = (
            endpoint or os.getenv("THINKROOM_ENDPOINT") or "http://127.0.0.1:8787"
        ).rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = httpx.request(method, self.endpoint + path, timeout=self.timeout, **kwargs)
        except httpx.HTTPError:
            raise ThinkroomError(
                503,
                "TRANSPORT_ERROR",
                "Thinkroom service unavailable",
                {},
            ) from None
        try:
            body = response.json()
        except ValueError:
            if not response.is_error:
                raise ThinkroomError(
                    502,
                    "INVALID_RESPONSE",
                    "Thinkroom service returned an invalid response",
                    {},
                ) from None
            body = {}
        if response.is_error:
            code, message, details = _project_remote_error(body)
            raise ThinkroomError(
                response.status_code,
                code,
                message,
                details,
            )
        if not isinstance(body, dict):
            raise ThinkroomError(
                502,
                "INVALID_RESPONSE",
                "Thinkroom service returned an invalid response",
                {},
            )
        return body

    def research(
        self, question: str, *, idempotency_key: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"json": {"question": question, **kwargs}}
        if idempotency_key is not None:
            request["headers"] = {"Idempotency-Key": idempotency_key}
        return self._request("POST", "/api/v1/research", **request)

    def get(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/research/{job_id}")

    def list(self, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", "/api/v1/research", params=kwargs)

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/v1/research/{job_id}")


class Thinkroom:
    def __init__(
        self,
        domain: Literal["generic", "coding", "trading"] = "generic",
        settings: Settings | None = None,
    ) -> None:
        self.domain = domain
        self.settings = settings or Settings.from_env()

    def research(self, question: str, context: str | None = None, **kwargs: Any) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            service = ThinkroomService(self.settings)
            await service.start()
            try:
                assert service.engine is not None
                req = ResearchRequest(
                    question=question, context=context, domain=self.domain, **kwargs
                )
                job, _ = await service.engine.submit(req)
                while True:
                    row = service.repo.get_job(job)
                    if row and row["state"] in {"succeeded", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.02)
                detail = service.research_detail(job)
                if detail is None:
                    raise RuntimeError("embedded research result disappeared")
                return detail.model_dump(mode="json")
            finally:
                await service.stop()

        return asyncio.run(run())
