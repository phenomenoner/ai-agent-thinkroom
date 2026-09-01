from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .schemas import (
    JobState,
    Phase,
    PhaseProgressV1,
    ProgressClassification,
    ProgressSubstate,
    ResearchProgressV1,
)

_TERMINAL = {JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.CANCELLED.value}


def _value(row: Mapping[str, Any] | Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, None)


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def derive_research_progress(
    job: Mapping[str, Any] | Any,
    *,
    branch_count: int,
    provider_calls: Sequence[Mapping[str, Any] | Any],
    artifacts: Sequence[Mapping[str, Any] | Any],
    transitions: Sequence[Mapping[str, Any] | Any],
    observed_at: datetime | None = None,
    slow_warning_seconds: float = 120,
    stale_seconds: float = 30,
    orphan_grace_seconds: float = 5,
) -> ResearchProgressV1:
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    state = str(_value(job, "state"))
    watermark = max((int(_value(row, "id") or 0) for row in provider_calls), default=0)
    progress_times = [
        instant
        for instant in (
            _instant(_value(job, "updated_at")),
            *(_instant(_value(row, "at")) for row in transitions),
            *(_instant(_value(row, "started_at")) for row in provider_calls),
            *(_instant(_value(row, "ended_at")) for row in provider_calls),
        )
        if instant is not None
    ]
    last_progress = max(progress_times, default=now)

    grouped: dict[tuple[str, str | None], list[Any]] = {}
    for row in provider_calls:
        grouped.setdefault((str(_value(row, "phase")), _value(row, "branch_id")), []).append(row)

    phase_progress: list[PhaseProgressV1] = []
    active_count = 0
    has_degraded = False
    has_slow = False
    has_presumed_dead = False
    circuit_score = 0
    for row in provider_calls:
        if _value(row, "route_role") != "primary":
            continue
        code = _value(row, "error_code")
        if code == "BACKEND_TIMEOUT":
            circuit_score += 2
        elif code == "PROVIDER_ERROR":
            started = _instant(_value(row, "started_at"))
            ended = _instant(_value(row, "ended_at"))
            if (
                started is not None
                and ended is not None
                and (ended - started).total_seconds() <= 30
            ):
                circuit_score += 1

    for (phase, branch_id), rows in grouped.items():
        current = rows[-1]
        if _value(current, "ended_at") is not None:
            continue
        started = _instant(_value(current, "started_at"))
        timeout = _value(current, "effective_timeout_seconds")
        elapsed = max(0.0, (now - started).total_seconds()) if started else None
        timeout_value = (
            float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None
        )
        expired = (
            started is None
            or timeout_value is None
            or now > started + timedelta(seconds=timeout_value + orphan_grace_seconds)
        )
        route = _value(current, "route_role")
        if expired:
            substate = ProgressSubstate.PRESUMED_DEAD
            has_presumed_dead = True
        else:
            active_count += 1
            previous_error = _value(rows[-2], "error_code") if len(rows) > 1 else None
            if previous_error == "MALFORMED_PROVIDER_OUTPUT":
                substate = ProgressSubstate.SCHEMA_REPAIR_ACTIVE
                has_degraded = True
            elif route == "fallback":
                substate = ProgressSubstate.FALLBACK_ACTIVE
                has_degraded = True
            else:
                substate = ProgressSubstate.PRIMARY_ACTIVE
            if elapsed is not None and elapsed >= slow_warning_seconds:
                has_slow = True
        phase_progress.append(
            PhaseProgressV1(
                phase=cast(Phase, phase),
                branch_id=branch_id,
                substate=substate,
                route=route if route in {"primary", "fallback", "single"} else None,
                backend=_value(current, "backend"),
                model=_value(current, "model"),
                elapsed_seconds=elapsed,
                timeout_remaining_seconds=(
                    timeout_value - elapsed
                    if timeout_value is not None and elapsed is not None
                    else None
                ),
                calls_used=min(len(rows), 3),
                budget_remaining=max(0, 3 - len(rows)),
            )
        )

    succeeded_keys = {
        (
            "rollout" if _value(row, "kind") == "branch" else str(_value(row, "kind")),
            _value(row, "branch_id"),
        )
        for row in artifacts
        if _value(row, "kind") in {"frame", "fork", "branch", "critique", "synthesis"}
        and _value(row, "state") != "failed"
    }
    failed_keys = {
        ("rollout", _value(row, "branch_id"))
        for row in artifacts
        if _value(row, "kind") == "branch" and _value(row, "state") == "failed"
    }
    planned = branch_count + 4
    succeeded = len(succeeded_keys)
    failed = len(failed_keys)
    queued = max(0, planned - succeeded - failed - active_count)
    if state in _TERMINAL:
        classification = ProgressClassification.TERMINAL
        substate = ProgressSubstate.SETTLING
        queued = 0
    elif has_presumed_dead:
        classification = ProgressClassification.PRESUMED_DEAD
        substate = ProgressSubstate.PRESUMED_DEAD
    elif (
        active_count == 0 and queued == 0 and (now - last_progress).total_seconds() >= stale_seconds
    ):
        classification = ProgressClassification.STALLED
        substate = ProgressSubstate.WAITING_FOR_SLOT
    elif has_degraded or circuit_score >= 2:
        classification = ProgressClassification.DEGRADED
        substate = (
            phase_progress[0].substate if phase_progress else ProgressSubstate.PRIMARY_CIRCUIT_OPEN
        )
    elif has_slow:
        classification = ProgressClassification.SLOW
        substate = phase_progress[0].substate
    elif active_count:
        classification = ProgressClassification.ACTIVE
        substate = phase_progress[0].substate
    elif state == JobState.SYNTHESIZING.value:
        classification = ProgressClassification.SETTLING
        substate = ProgressSubstate.SETTLING
    else:
        classification = ProgressClassification.ACTIVE
        substate = ProgressSubstate.WAITING_FOR_SLOT

    return ResearchProgressV1(
        observed_at=now,
        evidence_watermark=watermark,
        as_of=now,
        classification=classification,
        substate=substate,
        planned=planned,
        active=active_count,
        succeeded=succeeded,
        failed=failed,
        queued=queued,
        last_progress_at=last_progress,
        phases=phase_progress,
    )
