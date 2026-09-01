from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from .backends import FailoverBackend, OpenAIBackend, PrimeAgentBackend, ScriptedBackend
from .config import Settings
from .engine import ResearchEngine
from .ports import RolloutBackend
from .process_backend import ProcessIsolatedBackend
from .progress import derive_research_progress
from .repository import SQLiteRepository
from .schemas import ResearchDetail, ResearchRequest

log = logging.getLogger("thinkroom.service")
_DATABASE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _assert_database_path_custody(path: Path) -> None:
    if not hasattr(os, "geteuid"):
        raise RuntimeError("secure SQLite pathname custody is unavailable on this platform")
    effective_uid = os.geteuid()
    try:
        immediate_parent = path.parent.lstat()
    except FileNotFoundError:
        raise ValueError("database parent must already exist") from None
    if immediate_parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        if immediate_parent.st_mode & stat.S_ISVTX:
            raise ValueError("database directory is writable by untrusted principals")
        raise ValueError("database ancestor is writable by untrusted principals")
    current = path.parent
    while True:
        try:
            value = current.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(value.st_mode):
                raise ValueError("database ancestor is not a directory")
            writable_by_others = value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            trusted_sticky_owner = value.st_mode & stat.S_ISVTX and value.st_uid in {
                0,
                effective_uid,
            }
            if writable_by_others and not trusted_sticky_owner:
                raise ValueError("database ancestor is writable by untrusted principals")
            if value.st_uid not in {0, effective_uid}:
                raise ValueError("database ancestor is owned by an untrusted principal")
        parent = current.parent
        if parent == current:
            break
        current = parent

    try:
        value = path.lstat()
    except FileNotFoundError:
        return
    if value.st_uid != effective_uid:
        raise ValueError("database is owned by an untrusted principal")
    if value.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("database is writable by untrusted principals")


def _assert_database_sidecar_custody(path: Path) -> None:
    effective_uid = os.geteuid()
    for suffix in _DATABASE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{path}{suffix}")
        try:
            value = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != effective_uid
            or value.st_nlink != 1
            or value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("database sidecar failed custody validation")


def _reserve_database_path(path: Path) -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or not hasattr(os, "geteuid"):
        raise RuntimeError("secure SQLite pathname reservation is unavailable")
    try:
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise ValueError("database path may not contain symlink or unsafe identity") from exc
    try:
        opened = os.fstat(fd)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("database path failed custody validation")
        return opened.st_dev, opened.st_ino
    finally:
        os.close(fd)


def _assert_database_identity_token(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ValueError("database identity changed") from exc
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != expected:
        raise ValueError("database identity changed")


def canonical_database_path(path: str) -> str:
    raw_absolute = os.path.abspath(path)
    if os.name != "nt" and raw_absolute.startswith("//"):
        raw_absolute = "/" + raw_absolute.lstrip("/")
    absolute = Path(raw_absolute)
    try:
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("database path cannot be resolved safely") from exc
    if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
        raise ValueError("database path may not contain symlink")
    try:
        info = absolute.lstat()
    except FileNotFoundError:
        return str(absolute)
    except OSError as exc:
        raise ValueError("database path cannot be inspected safely") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("database path must be a regular file")
    if info.st_nlink != 1:
        raise ValueError("database has multiple filesystem names")
    return str(absolute)


class JsonFormatter(logging.Formatter):
    """Small structured formatter that never serializes arbitrary secret values."""

    _allowed = {
        "job_id",
        "attempt_id",
        "correlation_id",
        "phase",
        "backend",
        "model",
        "worker_id",
        "code",
        "input_bytes",
        "output_bytes",
        "retry_index",
        "removed",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in self._allowed:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper())
    package_logger = logging.getLogger("thinkroom")
    package_logger.setLevel(numeric)
    if not package_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        package_logger.addHandler(handler)
    package_logger.propagate = False


class ServiceLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> None:
        path = Path(os.path.abspath(self.path))
        try:
            parent = path.parent.lstat()
        except FileNotFoundError:
            raise ValueError("service lock parent must already exist") from None
        if not stat.S_ISDIR(parent.st_mode):
            raise ValueError("service lock parent is not a directory")
        if fcntl is None:
            self.handle = open(path, "a+")
            try:
                self.handle.seek(0)
                self.handle.write("0")
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self.handle.close()
                self.handle = None
                raise RuntimeError("SERVICE_LOCK_UNAVAILABLE") from exc
            return
        if not getattr(os, "O_NOFOLLOW", 0) or not hasattr(os, "geteuid"):
            raise RuntimeError("secure service-lock custody is unavailable")
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as exc:
            raise ValueError("service lock path is unsafe") from exc
        self.handle = os.fdopen(fd, "r+")
        try:
            self._assert_identity(path)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._assert_identity(path)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("SERVICE_LOCK_UNAVAILABLE") from exc
        except BaseException:
            self.handle.close()
            self.handle = None
            raise

    def _assert_identity(self, path: Path) -> None:
        assert self.handle is not None
        opened = os.fstat(self.handle.fileno())
        try:
            named = path.lstat()
        except OSError as exc:
            raise ValueError("service lock path changed during acquisition") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("service lock path failed custody validation")

    def assert_identity(self) -> None:
        if self.handle is None:
            raise RuntimeError("SERVICE_LOCK_UNAVAILABLE")
        self._assert_identity(Path(os.path.abspath(self.path)))

    def release(self) -> None:
        if self.handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        else:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
        self.handle = None


class ThinkroomService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.db_path = canonical_database_path(self.settings.db_path)
        self.lock = ServiceLock(self.db_path + ".lock")
        self.repo = SQLiteRepository(self.db_path, self.settings.max_persisted_bytes_per_job)
        self.engine: ResearchEngine | None = None
        self._database_identity: tuple[int, int] | None = None
        self._ready = False
        self._retention_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._ready and (self.engine is None or self.engine.provider_capacity_healthy)

    @ready.setter
    def ready(self, value: bool) -> None:
        self._ready = value

    @property
    def failed_readiness_predicates(self) -> list[str]:
        failed: list[str] = []
        if not self._ready:
            failed.extend(
                [
                    "configuration_validated",
                    "database_ready",
                    "exclusive_lock_acquired",
                    "recovery_complete",
                    "workers_ready",
                    "backend_ready",
                ]
            )
        if self.engine is not None and not self.engine.provider_capacity_healthy:
            failed.append("provider_capacity_available")
        return failed

    def _assert_database_identity(self) -> None:
        if canonical_database_path(self.db_path) != self.db_path:
            raise ValueError("database canonical identity changed")
        _assert_database_path_custody(Path(self.db_path))
        _assert_database_sidecar_custody(Path(self.db_path))
        if self._database_identity is not None:
            _assert_database_identity_token(Path(self.db_path), self._database_identity)

    def _assert_startup_custody(self) -> None:
        self._assert_database_identity()
        self.lock.assert_identity()

    def research_detail(self, job_id: str) -> ResearchDetail | None:
        row = self.repo.get_job(job_id)
        if row is None:
            return None
        detail: dict[str, Any] = {
            "job_id": job_id,
            "state": row["state"],
            "request": json.loads(row["request_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "attempt_id": row["attempt_id"],
            "attempt_number": row["attempt_number"],
            "branches": [],
            "perspectives": [],
            "frame": None,
            "critique": None,
            "critique_id": None,
            "synthesis": None,
            "terminal_error": json.loads(row["terminal_error"]) if row["terminal_error"] else None,
            "attempts": [dict(attempt) for attempt in self.repo.attempts(job_id)],
            "transitions": [dict(transition) for transition in self.repo.transitions(job_id)],
            "completion_status": None,
            "partial": None,
            "progress": None,
        }
        artifacts = self.repo.get_artifacts(job_id, row["attempt_id"]) if row["attempt_id"] else []
        for artifact in artifacts:
            payload = json.loads(artifact["payload"])
            if artifact["kind"] == "frame":
                detail["frame"] = payload
            elif artifact["kind"] == "fork":
                detail["perspectives"] = payload.get("perspectives", [])
            elif artifact["kind"] == "branch":
                detail["branches"].append(
                    {
                        "branch_id": artifact["branch_id"],
                        "state": artifact["state"],
                        "output": payload if artifact["state"] == "succeeded" else None,
                        "error": artifact["error"],
                    }
                )
            elif artifact["kind"] == "critique":
                detail["critique"] = payload
                detail["critique_id"] = f"critique-{artifact['id']}"
            elif artifact["kind"] == "synthesis":
                detail["synthesis"] = payload
                detail["completion_status"] = "complete"
            elif artifact["kind"] == "partial":
                detail["partial"] = payload
                detail["completion_status"] = "partial"
        request = ResearchRequest.model_validate(detail["request"])
        detail["progress"] = derive_research_progress(
            row,
            branch_count=request.branch_count,
            provider_calls=self.repo.provider_calls(job_id, row["attempt_id"])
            if row["attempt_id"]
            else [],
            artifacts=artifacts,
            transitions=self.repo.transitions(job_id),
        )
        return ResearchDetail.model_validate(detail)

    def _selected_backend(self) -> RolloutBackend:
        if self.settings.backend == "scripted":
            return ScriptedBackend()
        if self.settings.backend == "openai":
            return OpenAIBackend.from_env(
                self.settings.backend_timeout_seconds,
                self.settings.max_backend_output_tokens,
                self.settings.max_backend_response_bytes,
            )
        if self.settings.backend == "prime_agent":
            return PrimeAgentBackend.from_env(
                self.settings.backend_timeout_seconds,
                self.settings.max_backend_output_tokens,
                self.settings.max_backend_response_bytes,
            )
        primary = PrimeAgentBackend.from_env(
            self.settings.failover_primary_timeout_seconds,
            self.settings.max_backend_output_tokens,
            self.settings.max_backend_response_bytes,
        )
        if not primary.provider or not primary.configured_model:
            raise ValueError("prime_agent failover primary provider and model must be explicit")
        fallback = PrimeAgentBackend.fallback_from_env(
            self.settings.backend_timeout_seconds,
            self.settings.max_backend_output_tokens,
            self.settings.max_backend_response_bytes,
        )
        return FailoverBackend(
            ProcessIsolatedBackend(primary),
            ProcessIsolatedBackend(fallback),
            primary_timeout_seconds=self.settings.failover_primary_timeout_seconds,
            fallback_timeout_seconds=self.settings.backend_timeout_seconds,
        )

    async def start(self) -> None:
        self.settings.validate()
        self._assert_database_identity()
        backend = self._selected_backend()
        configure_logging(self.settings.log_level)
        self.lock.acquire()
        try:
            self._database_identity = _reserve_database_path(Path(self.db_path))
            self._assert_startup_custody()
            # No database connection or migration occurs until ownership is proven.
            self.repo.open(prepare_schema=False)
            self._assert_startup_custody()
            self.repo.migrate()
            self._assert_startup_custody()
            if type(backend) not in {ScriptedBackend, FailoverBackend}:
                backend = ProcessIsolatedBackend(backend)
            self.engine = ResearchEngine(self.repo, backend, self.settings)
            await self.engine.recover()
            await self.engine.start()
            self._assert_startup_custody()
            self._retention_task = asyncio.create_task(self._retention_loop())
            self.ready = True
            self._retention_task.add_done_callback(self._retention_done)
            log.info(
                "service ready",
                extra={"backend": self.settings.backend, "database": self.db_path},
            )
        except BaseException:
            self.ready = False
            cleanup_error: BaseException | None = None
            if self._retention_task is not None:
                self._retention_task.cancel()
                await asyncio.gather(self._retention_task, return_exceptions=True)
                self._retention_task = None
            if self.engine is not None:
                try:
                    await self.engine.stop()
                except BaseException as exc:
                    cleanup_error = exc
            try:
                self.repo.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            try:
                self.lock.release()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            self.engine = None
            if cleanup_error is not None:
                raise RuntimeError("failed to roll back service startup") from cleanup_error
            raise

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.ready:
                try:
                    removed = self.repo.cleanup_retention(self.settings.retention_days, 100)
                except Exception:
                    log.error(
                        "retention cleanup failed",
                        extra={"code": "RETENTION_CLEANUP_FAILED"},
                    )
                    continue
                if removed:
                    log.info("retention cleanup", extra={"removed": removed})

    def _retention_done(self, task: asyncio.Task[None]) -> None:
        if self._retention_task is task and self.ready:
            self.ready = False
            log.error(
                "retention supervisor stopped",
                extra={"code": "RETENTION_TASK_STOPPED"},
            )

    async def stop(self) -> None:
        self.ready = False
        cleanup_error: BaseException | None = None
        if self._retention_task is not None:
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
            self._retention_task = None
        if self.engine is not None:
            try:
                await self.engine.stop()
            except BaseException as exc:
                cleanup_error = exc
        try:
            self.repo.close()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
            self.lock.release()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        self.engine = None
        if cleanup_error is not None:
            raise RuntimeError("service shutdown did not complete cleanly") from cleanup_error


_default_service: ThinkroomService | None = None


def get_service() -> ThinkroomService:
    global _default_service
    if _default_service is None:
        _default_service = ThinkroomService()
    return _default_service
