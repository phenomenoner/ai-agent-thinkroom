from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
from multiprocessing.connection import Connection
from typing import Any

from .ports import BackendError, RolloutBackend
from .schemas import BackendRequestV1

_CANCEL = b"cancel"
_CLOSE = b"close"
_MAX_ENVELOPE_BYTES = 10_000_000
_NORMAL_EXIT_GRACE_SECONDS = 2.0


def _safe_error(exc: BaseException) -> dict[str, Any]:
    if (
        type(exc) is BackendError
        and type(exc.code) is str
        and type(exc.audit_status) is str
        and type(str(exc)) is str
    ):
        return {
            "kind": "backend_error",
            "code": exc.code,
            "message": str(exc),
            "audit_status": exc.audit_status,
        }
    return {
        "kind": "backend_error",
        "code": "PROVIDER_ERROR",
        "message": "provider invocation failed",
        "audit_status": "PROVIDER_ERROR",
    }


async def _run_child(
    connection: Connection, backend: RolloutBackend, request: BackendRequestV1
) -> None:
    invocation = asyncio.create_task(backend.invoke(request), name="thinkroom-provider-child")
    cancellation_requested = False
    try:
        while not invocation.done():
            if connection.poll():
                if connection.recv_bytes(32) == _CANCEL:
                    cancellation_requested = True
                    invocation.cancel()
            await asyncio.sleep(0.01)
        try:
            result = invocation.result()
        except BaseException as exc:
            envelope = _safe_error(exc)
        else:
            envelope = {"kind": "result", "value": result}
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            encoded = json.dumps(
                {
                    "kind": "backend_error",
                    "code": "OUTPUT_LIMIT_EXCEEDED",
                    "message": "provider response exceeded byte limit",
                    "audit_status": "OUTPUT_LIMIT_PROCESS_ENVELOPE",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        connection.send_bytes(encoded)
        # Keep the session leader alive until the parent owns the result and
        # explicitly closes the complete process tree. This prevents a normal
        # provider return from orphaning descendants behind an already-reaped
        # group leader.
        while not cancellation_requested:
            if connection.poll():
                if connection.recv_bytes(32) in {_CLOSE, _CANCEL}:
                    break
            await asyncio.sleep(0.01)
    finally:
        if not invocation.done():
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)


def _child_main(connection: Connection, backend: RolloutBackend, request_payload: str) -> None:
    if os.name == "posix":
        os.setsid()
    try:
        request = BackendRequestV1.model_validate_json(request_payload)
        asyncio.run(_run_child(connection, backend, request))
    except BaseException as exc:
        try:
            connection.send_bytes(
                json.dumps(_safe_error(exc), separators=(",", ":")).encode("utf-8")
            )
        except BaseException:
            pass
    finally:
        connection.close()


class ProcessIsolatedBackend:
    """Run provider code behind a killable process boundary.

    The engine owns logical state; this adapter owns only one physical provider
    process per invocation. Cancellation first asks the child to unwind, then
    terminates the complete process group if provider code refuses to cooperate.
    """

    def __init__(
        self,
        backend: RolloutBackend,
        *,
        shutdown_grace_seconds: float = 0.5,
        context_name: str | None = None,
    ) -> None:
        if shutdown_grace_seconds <= 0:
            raise ValueError("shutdown grace must be positive")
        if os.name != "posix":
            raise RuntimeError("provider process isolation requires a POSIX runtime")
        self._backend = backend
        self.name = backend.name
        self.model = backend.model
        self._grace = shutdown_grace_seconds
        selected_context = context_name or ("forkserver" if os.name == "posix" else "spawn")
        self._context: Any = multiprocessing.get_context(selected_context)
        self._processes: set[multiprocessing.Process] = set()

    @property
    def active_process_count(self) -> int:
        return sum(process.is_alive() for process in self._processes)

    async def _join(self, process: multiprocessing.Process, timeout: float) -> None:
        await asyncio.to_thread(process.join, timeout)

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _wait_group_exit(self, pgid: int, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._group_exists(pgid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def _cleanup_process_group(self, pgid: int) -> None:
        if not self._group_exists(pgid):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if await self._wait_group_exit(pgid, self._grace):
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if not await self._wait_group_exit(pgid, _NORMAL_EXIT_GRACE_SECONDS):
            raise RuntimeError("provider process group did not stop")

    @staticmethod
    def _terminate(process: multiprocessing.Process, *, force: bool) -> None:
        signum = signal.SIGKILL if force else signal.SIGTERM
        if os.name == "posix" and process.pid is not None:
            try:
                os.killpg(process.pid, signum)
                return
            except ProcessLookupError:
                # The child may still be between start() and setsid().
                pass
        if force:
            process.kill()
        else:
            process.terminate()

    async def _stop_process(self, process: multiprocessing.Process, control: Connection) -> None:
        pgid = process.pid
        if not process.is_alive():
            await self._join(process, 0)
            if pgid is not None:
                await self._cleanup_process_group(pgid)
            return
        try:
            control.send_bytes(_CANCEL)
        except (BrokenPipeError, EOFError, OSError):
            pass
        await self._join(process, self._grace)
        if process.is_alive():
            self._terminate(process, force=False)
            await self._join(process, self._grace)
        if process.is_alive():
            self._terminate(process, force=True)
            await self._join(process, self._grace)
        if process.is_alive():
            raise RuntimeError("provider process did not stop")
        if pgid is not None:
            await self._cleanup_process_group(pgid)

    async def _stop_process_reliably(
        self, process: multiprocessing.Process, control: Connection
    ) -> bool:
        """Finish physical containment and report caller cancellation."""
        cleanup = asyncio.create_task(self._stop_process(process, control))
        caller_cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                return caller_cancelled
            except asyncio.CancelledError:
                caller_cancelled = True

    @staticmethod
    async def _consume_receiver_reliably(receiver: asyncio.Task[bytes]) -> None:
        """Observe receiver completion even while the caller remains cancelled."""
        while not receiver.done():
            try:
                await asyncio.shield(receiver)
            except asyncio.CancelledError:
                continue
            except BaseException:
                return
        if receiver.cancelled():
            return
        try:
            receiver.exception()
        except BaseException:
            pass

    async def invoke(self, request: BackendRequestV1) -> dict[str, Any]:
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_child_main,
            args=(child, self._backend, request.model_dump_json()),
            name="thinkroom-provider",
        )
        receiver: asyncio.Task[bytes] | None = None
        try:
            process.start()
            child.close()
            self._processes.add(process)
            receiver = asyncio.create_task(
                asyncio.to_thread(parent.recv_bytes, _MAX_ENVELOPE_BYTES),
                name="thinkroom-provider-result",
            )
            try:
                encoded = await receiver
            except asyncio.CancelledError:
                await self._stop_process_reliably(process, parent)
                parent.close()
                await self._consume_receiver_reliably(receiver)
                raise
            try:
                parent.send_bytes(_CLOSE)
            except (BrokenPipeError, EOFError, OSError):
                pass
            caller_cancelled = await self._stop_process_reliably(process, parent)
            if caller_cancelled:
                raise asyncio.CancelledError() from None
            try:
                envelope = json.loads(encoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackendError(
                    "MALFORMED_PROVIDER_OUTPUT", "provider process returned invalid data"
                ) from exc
            if not isinstance(envelope, dict) or envelope.get("kind") not in {
                "result",
                "backend_error",
            }:
                raise BackendError(
                    "MALFORMED_PROVIDER_OUTPUT", "provider process returned invalid data"
                )
            if envelope["kind"] == "backend_error":
                code = envelope.get("code")
                message = envelope.get("message")
                audit_status = envelope.get("audit_status")
                if (
                    type(code) is not str
                    or type(message) is not str
                    or type(audit_status) is not str
                ):
                    raise BackendError(
                        "MALFORMED_PROVIDER_OUTPUT", "provider process returned invalid data"
                    )
                raise BackendError(code, message, audit_status=audit_status)
            value = envelope.get("value")
            if not isinstance(value, dict):
                raise BackendError(
                    "MALFORMED_PROVIDER_OUTPUT", "provider process returned invalid data"
                )
            return value
        except (EOFError, BrokenPipeError, OSError) as exc:
            if process.pid is not None:
                caller_cancelled = await self._stop_process_reliably(process, parent)
                if caller_cancelled:
                    raise asyncio.CancelledError() from None
            raise BackendError("PROVIDER_ERROR", "provider process failed") from exc
        finally:
            parent.close()
            child.close()
            if receiver is not None and not receiver.done():
                receiver.cancel()
            if receiver is not None:
                await self._consume_receiver_reliably(receiver)
            if process.is_alive():
                caller_cancelled = await self._stop_process_reliably(process, parent)
                if caller_cancelled:
                    raise asyncio.CancelledError() from None
            if not process.is_alive():
                self._processes.discard(process)
                process.close()
