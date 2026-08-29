"""Live end-to-end Prime Agent smoke; requires explicit environment configuration."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from thinkroom.config import Settings
from thinkroom.sdk import Thinkroom


def failure_diagnostic(result: dict[str, Any]) -> dict[str, Any]:
    """Project only bounded, non-sensitive terminal metadata for gate diagnosis."""
    terminal_error = result.get("terminal_error")
    error_code = terminal_error.get("code") if isinstance(terminal_error, dict) else None
    attempts = result.get("attempts")
    transitions = result.get("transitions")
    return {
        "state": result.get("state"),
        "terminal_error_code": error_code,
        "attempt_outcomes": [item.get("outcome") for item in attempts if isinstance(item, dict)]
        if isinstance(attempts, list)
        else [],
        "transition_states": [
            item.get("to_state") for item in transitions if isinstance(item, dict)
        ]
        if isinstance(transitions, list)
        else [],
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings.from_env(
            backend="prime_agent",
            database_url=f"sqlite+aiosqlite:///{Path(directory) / 'thinkroom.db'}",
            max_concurrency=1,
            max_queued_jobs=4,
            backend_timeout_seconds=600,
            job_timeout_seconds=3600,
        )
        result = Thinkroom(domain="coding", settings=settings).research(
            question="Should a production research service prefer durable asynchronous jobs over one long synchronous HTTP request?",
            context="Model calls may take several minutes, clients may disconnect, and the service must recover after restart.",
            branch_count=2,
        )
        if result["state"] != "succeeded":
            print(json.dumps(failure_diagnostic(result), sort_keys=True), file=sys.stderr)
            raise RuntimeError("Prime smoke: research job did not succeed")
        if len(result["branches"]) != 2:
            raise RuntimeError("Prime smoke: branch count mismatch")
        if not result["critique"] or not result["synthesis"]:
            raise RuntimeError("Prime smoke: critique or synthesis is missing")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "state": result["state"],
                    "branches": len(result["branches"]),
                    "has_critique": bool(result["critique"]),
                    "has_synthesis": bool(result["synthesis"]),
                }
            )
        )


if __name__ == "__main__":
    main()
