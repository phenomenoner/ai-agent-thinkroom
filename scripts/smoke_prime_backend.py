"""Live Prime Agent adapter smoke; requires explicit environment configuration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from thinkroom.backends import PrimeAgentBackend
from thinkroom.schemas import BackendRequestV1, FrameInputV1, FrameOutputV1


async def main() -> None:
    backend = PrimeAgentBackend.from_env(
        timeout=600,
        max_output_tokens=4096,
        max_response_bytes=1_000_000,
    )
    request = BackendRequestV1(
        phase="frame",
        job_id="smoke-job",
        attempt_id="smoke-attempt",
        prompt_version="smoke-v1",
        input=FrameInputV1(
            question="Should a production research service prefer durable asynchronous jobs over one long synchronous HTTP request?",
            context="The service calls model backends that may take several minutes and must survive restarts.",
            domain="coding",
            guidance="Assess correctness, operability, recovery, and complexity.",
            safety="Advisory only; do not modify systems.",
        ),
        expected_output_schema="FrameOutputV1",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        correlation_id="smoke-correlation",
    )
    validated = FrameOutputV1.model_validate(await backend.invoke(request))
    print(
        json.dumps(
            {
                "status": "ok",
                "backend": backend.name,
                "model": backend.model or "configured-default",
                "schema": validated.__class__.__name__,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
