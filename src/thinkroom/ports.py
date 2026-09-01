from __future__ import annotations

import re
from typing import Any, Protocol

from .schemas import (
    BackendRequestV1,
    BranchOutputV1,
    CritiqueOutputV1,
    ForkOutputV1,
    FrameOutputV1,
    SynthesisOutputV1,
)


class ResearchRepository(Protocol):
    """Marker for the application persistence port.

    Concrete adapters expose the repository operations consumed by the engine.
    """


class BackendError(RuntimeError):
    """Core-owned typed provider-boundary error."""

    def __init__(self, code: str, message: str, *, audit_status: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        candidate = code if audit_status is None else audit_status
        self.audit_status = (
            candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", candidate) else "invalid"
        )


class RolloutBackend(Protocol):
    """Process-free application port for one typed provider invocation."""

    name: str
    model: str

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]: ...


class ProviderInvocationAudit(Protocol):
    """Application-owned durable audit seam for one physical provider call."""

    def start(self, request: BackendRequestV1, backend: str, model: str) -> int: ...

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
    ) -> bool: ...


_OUTPUT_MODELS = {
    model.__name__: model
    for model in (
        FrameOutputV1,
        ForkOutputV1,
        BranchOutputV1,
        CritiqueOutputV1,
        SynthesisOutputV1,
    )
}


def backend_input(request: BackendRequestV1) -> dict[str, Any]:
    return request.input.model_dump(mode="json")


def provider_payload(request: BackendRequestV1) -> dict[str, Any]:
    """Build the bounded schema request without importing provider adapters."""
    try:
        output_model = _OUTPUT_MODELS[request.expected_output_schema]
    except KeyError as exc:
        raise BackendError("INVALID_REQUEST", "unknown expected output schema") from exc
    return {
        "instruction": "Return exactly one JSON object conforming to output_json_schema.",
        "phase": request.phase,
        "input": backend_input(request),
        "output_schema_name": request.expected_output_schema,
        "output_json_schema": output_model.model_json_schema(),
    }
