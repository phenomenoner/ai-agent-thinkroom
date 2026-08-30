from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import WithJsonSchema

from .sdk import ThinkroomClient, ThinkroomError

_SAFE_REMOTE_ERRORS: dict[str, tuple[str, str, dict[str, str]]] = {
    "PAYLOAD_TOO_LARGE": (
        "INVALID_ARGUMENT",
        "request exceeds configured limit",
        {"service_code": "PAYLOAD_TOO_LARGE"},
    ),
    "INVALID_ARGUMENT": ("INVALID_ARGUMENT", "request is invalid", {}),
    "INVALID_REQUEST": ("INVALID_ARGUMENT", "request is invalid", {}),
    "INVALID_IDEMPOTENCY_KEY": ("INVALID_ARGUMENT", "request is invalid", {}),
    "INVALID_CURSOR": ("INVALID_ARGUMENT", "request is invalid", {}),
    "INVALID_HOST": ("INVALID_ARGUMENT", "request is invalid", {}),
    "METHOD_NOT_ALLOWED": ("INVALID_ARGUMENT", "method is not allowed", {}),
    "NOT_FOUND": ("NOT_FOUND", "Thinkroom resource not found", {}),
    "IDEMPOTENCY_CONFLICT": ("CONFLICT", "Thinkroom request conflicts", {}),
    "RESOURCE_EXHAUSTED": ("RESOURCE_EXHAUSTED", "Thinkroom capacity exhausted", {}),
    "NOT_READY": ("UNAVAILABLE", "Thinkroom service unavailable", {}),
    "INTERNAL_ERROR": ("INTERNAL_ERROR", "Thinkroom tool failed", {}),
    "TRANSPORT_ERROR": ("UNAVAILABLE", "Thinkroom service unavailable", {}),
}


def _mcp_call(operation: Callable[[], Any]) -> Any:
    error: BaseException
    try:
        return operation()
    except ThinkroomError as exc:
        from mcp.server.fastmcp.exceptions import ToolError

        code, message, details = _SAFE_REMOTE_ERRORS.get(
            exc.code,
            ("INTERNAL_ERROR", "Thinkroom tool failed", {}),
        )
        error = ToolError(json.dumps({"code": code, "message": message, "details": details}))
    except Exception:
        from mcp.server.fastmcp.exceptions import ToolError

        error = ToolError(
            json.dumps(
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Thinkroom tool failed",
                    "details": {},
                }
            )
        )
    raise error from None


def thinkroom_research(
    question: Annotated[
        str,
        WithJsonSchema({"type": "string", "minLength": 10, "maxLength": 10000}),
    ],
    context: Annotated[
        str | None,
        WithJsonSchema({"anyOf": [{"type": "string", "maxLength": 100000}, {"type": "null"}]}),
    ] = None,
    domain: Annotated[
        str,
        WithJsonSchema({"type": "string", "enum": ["generic", "coding", "trading"]}),
    ] = "generic",
    branch_count: Annotated[
        int,
        WithJsonSchema({"type": "integer", "minimum": 2, "maximum": 6}),
    ] = 3,
    idempotency_key: Annotated[
        str | None,
        WithJsonSchema(
            {
                "anyOf": [
                    {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": "^[!-~]+$",
                    },
                    {"type": "null"},
                ]
            }
        ),
    ] = None,
) -> dict[str, Any]:
    return _mcp_call(
        lambda: ThinkroomClient().research(
            question,
            context=context,
            domain=domain,
            branch_count=branch_count,
            idempotency_key=idempotency_key,
        )
    )


def thinkroom_get_research(job_id: str) -> dict[str, Any]:
    return _mcp_call(lambda: ThinkroomClient().get(job_id))


def thinkroom_list_research(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    return _mcp_call(lambda: ThinkroomClient().list(limit=limit, cursor=cursor))


def thinkroom_cancel_research(job_id: str) -> dict[str, Any]:
    return _mcp_call(lambda: ThinkroomClient().cancel(job_id))


def run_stdio() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("thinkroom")
    server.tool(
        name="thinkroom_research", description="Submit a long-running Thinkroom research job"
    )(thinkroom_research)
    server.tool(
        name="thinkroom_get_research",
        description="Get a Thinkroom research job and evidence result",
    )(thinkroom_get_research)
    server.tool(name="thinkroom_list_research", description="List Thinkroom research jobs")(
        thinkroom_list_research
    )
    server.tool(name="thinkroom_cancel_research", description="Cancel a Thinkroom research job")(
        thinkroom_cancel_research
    )
    server.run(transport="stdio")
