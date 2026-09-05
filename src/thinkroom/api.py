from __future__ import annotations

import base64
import binascii
import json
from contextlib import asynccontextmanager
from datetime import datetime
from ipaddress import ip_address
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .schemas import (
    ErrorBody,
    JobList,
    JobResource,
    JobState,
    ResearchDetail,
    ResearchDiagnosticsV1,
    ResearchRequest,
)
from .service import ThinkroomService, get_service


def err(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": code, "message": message, "details": details or {}}
    )


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI/Pydantic buffer them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "code": "PAYLOAD_TOO_LARGE",
                "message": "request exceeds configured byte limit",
                "details": {"max_bytes": self.max_bytes},
            },
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "code": "INVALID_ARGUMENT",
                        "message": "invalid Content-Length",
                        "details": {},
                    },
                )
                await response(scope, receive, send)
                return
        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class LoopbackHostMiddleware:
    """Reject DNS names at the no-auth browser boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _literal_host(value: bytes) -> str | None:
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError:
            return None
        port: str | None = None
        if text.startswith("["):
            end = text.find("]")
            if end < 0:
                return None
            host, suffix = text[1:end], text[end + 1 :]
            if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
                return None
            if suffix:
                port = suffix[1:]
        else:
            if text.count(":") > 1:
                return None
            host, separator, port_text = text.partition(":")
            if separator and not port_text.isdigit():
                return None
            if separator:
                port = port_text
        if port is not None:
            if len(port) > 5:
                return None
            try:
                if not 1 <= int(port) <= 65535:
                    return None
            except ValueError:
                return None
        try:
            return host if ip_address(host).is_loopback else None
        except ValueError:
            return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts = [value for name, value in scope.get("headers", []) if name.lower() == b"host"]
        if len(hosts) != 1 or self._literal_host(hosts[0]) is None:
            response = JSONResponse(
                status_code=400,
                content={
                    "code": "INVALID_HOST",
                    "message": "Host header must be a literal loopback IP address",
                    "details": {},
                },
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(service: ThinkroomService | None = None) -> FastAPI:
    svc = service or get_service()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await svc.start()
            yield
        finally:
            await svc.stop()

    app = FastAPI(title="Thinkroom", version="0.2.7", openapi_version="3.1.0", lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=svc.settings.max_context_bytes)
    app.add_middleware(LoopbackHostMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        too_large = any(
            e.get("type") in {"string_too_long", "bytes_too_long"} for e in exc.errors()
        )
        return JSONResponse(
            status_code=413 if too_large else 422,
            content={
                "code": "PAYLOAD_TOO_LARGE" if too_large else "INVALID_ARGUMENT",
                "message": "request exceeds configured limit"
                if too_large
                else "request validation failed",
                "details": {"errors": exc.errors()},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        default_codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {
                "code": default_codes.get(exc.status_code, "HTTP_ERROR"),
                "message": str(exc.detail),
                "details": {},
            }
        )
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "internal server error",
                "details": {},
            },
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", responses={503: {"model": ErrorBody}})
    async def ready() -> dict[str, Any]:
        if not svc.ready:
            raise err(
                503,
                "NOT_READY",
                "service is not ready",
                {"failed_predicates": svc.failed_readiness_predicates},
            )
        return {"status": "ready"}

    @app.get("/api/v1/version")
    async def version() -> dict[str, str]:
        return {"version": "0.2.7", "schema_version": "1"}

    def resource(job_id: str) -> JobResource:
        row = svc.repo.get_job(job_id)
        if not row:
            raise err(404, "NOT_FOUND", "research job not found")
        return JobResource(
            job_id=job_id,
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            url=f"/api/v1/research/{job_id}",
        )

    @app.post(
        "/api/v1/research",
        status_code=202,
        response_model=JobResource,
        responses={
            200: {"model": JobResource, "description": "Terminal idempotent replay"},
            400: {"model": ErrorBody},
            409: {"model": ErrorBody},
            413: {"model": ErrorBody},
            422: {"model": ErrorBody},
            429: {"model": ErrorBody},
        },
    )
    async def create(
        req: ResearchRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JobResource:
        request_bytes = len(
            json.dumps(
                req.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if request_bytes > svc.settings.max_context_bytes:
            raise err(
                413,
                "PAYLOAD_TOO_LARGE",
                "request exceeds configured context byte limit",
                {"max_bytes": svc.settings.max_context_bytes},
            )
        if idempotency_key is not None and (
            not 1 <= len(idempotency_key) <= 128
            or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
        ):
            raise err(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be printable ASCII")
        try:
            assert svc.engine is not None
            job_id, existing = await svc.engine.submit(req, idempotency_key)
        except RuntimeError as exc:
            if str(exc) == "QUEUE_FULL":
                raise err(429, "RESOURCE_EXHAUSTED", "queue is full") from None
            raise
        except ValueError as exc:
            if str(exc) == "IDEMPOTENCY_CONFLICT":
                raise err(
                    409, "IDEMPOTENCY_CONFLICT", "key already used with different request"
                ) from None
            if str(exc) == "INVALID_STRATEGY":
                raise err(422, "INVALID_REQUEST", "unknown strategy") from None
            raise
        result = resource(job_id)
        response.headers["Location"] = result.url
        if existing and result.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            response.status_code = 200
        else:
            response.status_code = 202
        return result

    @app.get("/api/v1/research", response_model=JobList, responses={422: {"model": ErrorBody}})
    async def list_research(
        limit: int = Query(20, ge=1, le=100), cursor: str | None = None
    ) -> JobList:
        before = None
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor + "===").decode())
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or not all(isinstance(v, str) and v for v in value)
                ):
                    raise ValueError
                before = (value[0], value[1])
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
                raise err(422, "INVALID_CURSOR", "cursor is invalid") from exc
        rows = svc.repo.list_jobs(limit + 1, before)
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = (
                base64.urlsafe_b64encode(json.dumps([last["created_at"], last["job_id"]]).encode())
                .decode()
                .rstrip("=")
            )
            rows = rows[:limit]
        return JobList(items=[resource(r["job_id"]) for r in rows], next_cursor=next_cursor)

    @app.get(
        "/api/v1/research/{job_id}",
        response_model=ResearchDetail,
        responses={404: {"model": ErrorBody}},
    )
    async def get_research(job_id: str) -> ResearchDetail:
        detail = svc.research_detail(job_id)
        if detail is None:
            raise err(404, "NOT_FOUND", "research job not found")
        return detail

    @app.get(
        "/api/v1/research/{job_id}/diagnostics",
        response_model=ResearchDiagnosticsV1,
        responses={404: {"model": ErrorBody}},
    )
    async def get_diagnostics(job_id: str) -> ResearchDiagnosticsV1:
        row = svc.repo.get_job(job_id)
        if row is None:
            raise err(404, "NOT_FOUND", "research job not found")
        aid = row["attempt_id"]
        failures = (
            [
                json.loads(item["payload"])
                for item in svc.repo.get_artifacts(job_id, aid)
                if item["kind"] == "admission"
            ]
            if aid
            else []
        )
        return ResearchDiagnosticsV1(job_id=job_id, attempt_id=aid, admission_failures=failures)

    @app.delete(
        "/api/v1/research/{job_id}",
        response_model=JobResource,
        responses={
            200: {"model": JobResource, "description": "Already terminal or cancellation settled"},
            202: {"model": JobResource, "description": "Cancellation propagation pending"},
            404: {"model": ErrorBody},
            422: {"model": ErrorBody},
            429: {"model": ErrorBody},
            503: {"model": ErrorBody},
        },
    )
    async def cancel(job_id: str) -> Any:
        if not svc.repo.get_job(job_id):
            raise err(404, "NOT_FOUND", "research job not found")
        assert svc.engine is not None
        await svc.engine.cancel(job_id)
        current = resource(job_id)
        if current.state in {"succeeded", "failed", "cancelled"}:
            return current
        return JSONResponse(status_code=202, content=current.model_dump(mode="json"))

    @app.get("/", response_class=HTMLResponse)
    async def web() -> HTMLResponse:
        html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Thinkroom</title>
<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#202124}textarea{width:100%;min-height:7rem}button{margin:.5rem .5rem .5rem 0;padding:.5rem 1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}.card{border:1px solid #ddd;border-radius:6px;padding:1rem;background:#fafafa}pre{white-space:pre-wrap;overflow:auto;background:#f5f5f5;padding:.75rem;border-radius:4px}.error{color:#a00}.muted{color:#666}</style></head>
<body><h1>Thinkroom</h1><form id="research-form"><label>Question<textarea id="question" minlength="10" required placeholder="Ask an important question"></textarea></label><br><button>Research</button><button id="cancel" type="button" disabled>Cancel</button></form><h2 id="state">Idle</h2><p id="terminal" class="error"></p><div id="sections" class="grid"></div><details><summary>Raw JSON</summary><pre id="raw"></pre></details>
<script>
let jobId=null; const terminalStates=['succeeded','failed','cancelled'];
const text=x=>String(x??'');
function element(tag,value,cls){const node=document.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=text(value);return node}
function card(title){const section=element('section',undefined,'card');section.append(element('h3',title));document.getElementById('sections').append(section);return section}
function paragraph(parent,label,value){const p=element('p');if(label){const strong=element('b',label);p.append(strong,document.createTextNode(' '))}p.append(document.createTextNode(text(value)));parent.append(p)}
function render(x){
 document.getElementById('state').textContent='State: '+text(x.state); document.getElementById('raw').textContent=JSON.stringify(x,null,2);
 document.getElementById('terminal').textContent=x.terminal_error?((x.terminal_error.code||'error')+': '+(x.terminal_error.message||'')):'';
 document.getElementById('sections').replaceChildren();
 if(x.frame){const section=card('Frame');paragraph(section,'Decision:',x.frame.decision);paragraph(section,'',x.frame.scope)}
 if(x.branches?.length){const section=card('Branches');for(const branch of x.branches){const article=element('article');paragraph(article,'Branch:',text(branch.branch_id)+' — '+text(branch.state));paragraph(article,'',branch.output?.summary||branch.error);paragraph(article,'Evidence:',(branch.output?.supporting_evidence||[]).map(e=>text(e.statement)+' ['+text(e.verification_status)+']').join('; '));section.append(article)}}
 if(x.critique){const section=card('Critique');paragraph(section,'Agreements:',(x.critique.agreements||[]).join('; '));paragraph(section,'Contradictions:',(x.critique.contradictions||[]).join('; '))}
 if(x.synthesis){const section=card('Synthesis');paragraph(section,'Disposition:',x.synthesis.disposition);paragraph(section,'',x.synthesis.recommendation);paragraph(section,'',x.synthesis.rationale);paragraph(section,'Next actions:',(x.synthesis.next_actions||[]).join('; '))}
 document.getElementById('cancel').disabled=!jobId||terminalStates.includes(x.state);
}
async function poll(){if(!jobId)return;const r=await fetch('/api/v1/research/'+encodeURIComponent(jobId));const x=await r.json();render(x);if(!terminalStates.includes(x.state))setTimeout(poll,500)}
document.getElementById('research-form').onsubmit=async e=>{e.preventDefault();const r=await fetch('/api/v1/research',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:document.getElementById('question').value})});const x=await r.json();if(!r.ok){render(x);return}jobId=x.job_id;poll()};
document.getElementById('cancel').onclick=async()=>{if(jobId){await fetch('/api/v1/research/'+encodeURIComponent(jobId),{method:'DELETE'});poll()}};
</script></body></html>"""
        return HTMLResponse(
            content=html,
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            },
        )

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            routes=app.routes,
        )
        error_response = {
            "description": "Typed Thinkroom error",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}},
        }
        for methods in schema.get("paths", {}).values():
            if not isinstance(methods, dict):
                continue
            for operation in methods.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                responses = operation["responses"]
                if isinstance(responses, dict):
                    responses.setdefault("400", error_response)
                    responses.setdefault("405", error_response)
                    responses.setdefault("413", error_response)
                    responses.setdefault("500", error_response)
        app.openapi_schema = schema
        return schema

    cast(Any, app).openapi = openapi

    return app
