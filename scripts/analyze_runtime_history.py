#!/usr/bin/env python3
"""Summarize one read-only SQLite snapshot without emitting research contents."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def safe_code(value: object) -> str:
    return (
        value if type(value) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value) else "UNKNOWN"
    )


def latency_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "observations": len(values),
        "percentile_method": "nearest_rank",
        **{
            name: round(ordered[max(0, math.ceil(q * len(ordered)) - 1)], 3) if ordered else None
            for name, q in [("p50_seconds", 0.5), ("p95_seconds", 0.95), ("max_seconds", 1.0)]
        },
    }


def summarize(database: Path, *, limit: int = 1000, since: str = "") -> dict[str, Any]:
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    if since:
        parsed = datetime.fromisoformat(since)
        if parsed.tzinfo is None:
            raise ValueError("since must include an explicit timezone")
        since = parsed.astimezone(UTC).isoformat()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        available = db.execute(
            "SELECT count(*) FROM research_jobs WHERE created_at>=?", (since,)
        ).fetchone()[0]
        jobs = db.execute(
            "SELECT job_id,attempt_id,state,created_at,updated_at,terminal_error,request_json FROM research_jobs WHERE created_at>=? ORDER BY created_at DESC,job_id DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        routes: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        completion: Counter[str] = Counter()
        terminal_errors: Counter[str] = Counter()
        contexts: Counter[str] = Counter()
        stages: Counter[str] = Counter()
        failed_branches = 0
        diagnostics_count = 0
        diagnosed_call_ids: set[int] = set()
        for job in jobs:
            try:
                body = json.loads(job["request_json"])
                context_bytes = len((body.get("context") or "").encode("utf-8"))
                context_bucket = next(
                    (
                        label
                        for ceiling, label in [
                            (4096, "under_4KiB"),
                            (16384, "4_to_16KiB"),
                            (65536, "16_to_64KiB"),
                        ]
                        if context_bytes < ceiling
                    ),
                    "64KiB_or_more",
                )
                contexts[context_bucket] += 1
            except (ValueError, TypeError, AttributeError):
                contexts["unknown"] += 1
            if job["terminal_error"]:
                try:
                    terminal_errors[safe_code(json.loads(job["terminal_error"]).get("code"))] += 1
                except (ValueError, TypeError, AttributeError):
                    terminal_errors["UNKNOWN"] += 1
            artifacts = db.execute(
                "SELECT kind,state,payload FROM artifacts WHERE job_id=? AND attempt_id=?",
                (job["job_id"], job["attempt_id"]),
            ).fetchall()
            kinds = {artifact["kind"] for artifact in artifacts}
            if job["state"] == "succeeded":
                completion[
                    "partial"
                    if "partial" in kinds
                    else "complete"
                    if "synthesis" in kinds
                    else "unknown"
                ] += 1
            failed_branches += sum(
                artifact["kind"] == "branch" and artifact["state"] == "failed"
                for artifact in artifacts
            )
            calls = db.execute(
                "SELECT * FROM provider_calls WHERE job_id=?", (job["job_id"],)
            ).fetchall()
            calls_by_id = {call["id"]: call for call in calls}
            for call in calls:
                routes[
                    (call["backend"] or "unknown", call["model"] or "unknown", call["phase"])
                ].append(call)
            for artifact in artifacts:
                if artifact["kind"] != "provider_diagnostics":
                    continue
                try:
                    body = json.loads(artifact["payload"])
                    metrics = body["runtime_metrics"]
                    stage = metrics["runtime_stage"]
                    if type(stage) is not int or not 0 <= stage <= 6:
                        continue
                    diagnostics_count += 1
                    diagnosed_call_ids.add(body["call_id"])
                    call = calls_by_id.get(body["call_id"])
                    if call is not None and call["error_code"] == "BACKEND_TIMEOUT":
                        stages[str(stage)] += 1
                except (ValueError, KeyError, TypeError):
                    continue
        route_summaries = []
        for (backend, model, phase), calls in sorted(routes.items()):
            durations = []
            validated = []
            for call in calls:
                if call["ended_at"]:
                    seconds = (
                        datetime.fromisoformat(call["ended_at"])
                        - datetime.fromisoformat(call["started_at"])
                    ).total_seconds()
                    if seconds >= 0:
                        durations.append(seconds)
                        if call["output_status"] == "validated":
                            validated.append(seconds)
            route_summaries.append(
                {
                    "backend": backend,
                    "model": model,
                    "phase": phase,
                    "calls": len(calls),
                    "statuses": dict(Counter(call["output_status"] for call in calls)),
                    "error_codes": dict(
                        Counter(
                            safe_code(call["error_code"]) for call in calls if call["error_code"]
                        )
                    ),
                    "validated_latency": latency_summary(validated),
                    "settled_call_latency": latency_summary(durations),
                    "runtime_diagnostics": sum(call["id"] in diagnosed_call_ids for call in calls),
                }
            )
        db.rollback()
    return {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "snapshot": "one_read_only_transaction",
        "since": since or None,
        "available_jobs": available,
        "sampled_jobs": len(jobs),
        "truncated": available > len(jobs),
        "states": dict(Counter(job["state"] for job in jobs)),
        "succeeded_completion_statuses": dict(completion),
        "terminal_error_codes": dict(terminal_errors),
        "failed_branches": failed_branches,
        "context_size_buckets": dict(contexts),
        "provider_diagnostics_count": diagnostics_count,
        "timeout_last_observed_stages": dict(stages),
        "routes_by_phase": route_summaries,
        "limitations": [
            "Retained jobs are not lifetime history.",
            "Timeout latencies are censored observations, not provider response latencies.",
            "A completed synthesis can include failed branches.",
            "Missing diagnostics are unknown; journal progress can supplement abruptly killed calls.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--since", default="", help="ISO 8601 timestamp including timezone")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    print(
        json.dumps(
            summarize(args.database, limit=args.limit, since=args.since),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
