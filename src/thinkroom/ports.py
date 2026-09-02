from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class BackendTransportMetrics:
    """Content-free numeric evidence for one provider transport."""

    raw_transport_bytes: int = 0
    event_count: int = 0
    max_event_bytes: int = 0
    message_update_count: int = 0
    message_snapshot_bytes: int = 0
    message_partial_bytes: int = 0
    message_delta_bytes: int = 0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if type(value) is not int or value < 0 or value > 2**63 - 1:
                raise ValueError("transport metrics must be non-negative 64-bit integers")

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_untrusted(cls, value: object) -> BackendTransportMetrics | None:
        if value is None:
            return None
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("transport metrics shape is invalid")
        return cls(**value)

    def repository_values(self) -> dict[str, int]:
        return {
            "transport_bytes": self.raw_transport_bytes,
            "transport_events": self.event_count,
            "transport_max_event_bytes": self.max_event_bytes,
            "transport_message_updates": self.message_update_count,
            "transport_snapshot_bytes": self.message_snapshot_bytes,
            "transport_partial_bytes": self.message_partial_bytes,
            "transport_delta_bytes": self.message_delta_bytes,
        }


class BackendResult(dict[str, Any]):
    """Provider result carrying optional content-free transport evidence."""

    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        transport_metrics: BackendTransportMetrics | None = None,
    ) -> None:
        super().__init__(value)
        self.transport_metrics = transport_metrics


class BackendError(RuntimeError):
    """Core-owned typed provider-boundary error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        audit_status: str | None = None,
        transport_metrics: BackendTransportMetrics | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        candidate = code if audit_status is None else audit_status
        self.audit_status = (
            candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", candidate) else "invalid"
        )
        self.transport_metrics = (
            transport_metrics if type(transport_metrics) is BackendTransportMetrics else None
        )


class RolloutBackend(Protocol):
    """Process-free application port for one typed provider invocation."""

    name: str
    model: str

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]: ...


class ProviderInvocationAudit(Protocol):
    """Application-owned durable audit seam for one physical provider call."""

    def start(
        self,
        request: BackendRequestV1,
        backend: str,
        model: str,
        *,
        route_role: str = "single",
        effective_timeout_seconds: float = 0,
    ) -> int: ...

    def finish(
        self,
        call_id: int,
        request: BackendRequestV1,
        output_status: str,
        output_size: int = 0,
        *,
        error_code: str | None = None,
        transport_metrics: BackendTransportMetrics | None = None,
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
