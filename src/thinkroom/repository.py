from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .schemas import JobState, ResearchRequest


def now() -> str:
    return datetime.now(UTC).isoformat()


_ACTIVE = {
    JobState.FRAMING.value,
    JobState.ROLLING_OUT.value,
    JobState.CRITIQUING.value,
    JobState.SYNTHESIZING.value,
}
_TERMINAL = {JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.CANCELLED.value}
# Every accepted nonterminal mutation must leave enough room for one bounded
# state-transition row, terminal error, and attempt closure. Terminal liveness
# must not depend on provider output leaving a convenient amount of space.
_TERMINAL_SETTLEMENT_RESERVE = 16_384
_ALLOWED: dict[str, set[str]] = {
    JobState.QUEUED.value: {
        JobState.FRAMING.value,
        JobState.CANCELLED.value,
        JobState.FAILED.value,
    },
    JobState.FRAMING.value: {
        JobState.ROLLING_OUT.value,
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.QUEUED.value,
    },
    JobState.ROLLING_OUT.value: {
        JobState.CRITIQUING.value,
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.QUEUED.value,
    },
    JobState.CRITIQUING.value: {
        JobState.SYNTHESIZING.value,
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.QUEUED.value,
    },
    JobState.SYNTHESIZING.value: {
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.QUEUED.value,
    },
}

_SCHEMA_SQL_V024 = """
CREATE TABLE IF NOT EXISTS research_jobs (job_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deadline TEXT NOT NULL, cancellation_requested INTEGER NOT NULL DEFAULT 0, terminal_error TEXT, attempt_id TEXT, attempt_number INTEGER);
CREATE TABLE IF NOT EXISTS attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, recovery_reason TEXT, backend TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS state_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt_id TEXT, from_state TEXT, to_state TEXT NOT NULL, at TEXT NOT NULL, reason TEXT, correlation_id TEXT);
CREATE TABLE IF NOT EXISTS artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, kind TEXT NOT NULL, branch_id TEXT, payload TEXT NOT NULL, state TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS provider_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, phase TEXT NOT NULL, branch_id TEXT, prompt_version TEXT, backend TEXT, model TEXT, started_at TEXT NOT NULL, ended_at TEXT, retry_index INTEGER, output_status TEXT, output_size INTEGER);
CREATE TABLE IF NOT EXISTS idempotency_keys (key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, job_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON research_jobs(created_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON research_jobs(state, created_at);
"""
_SCHEMA_SQL_V025 = _SCHEMA_SQL_V024.replace(
    "output_status TEXT, output_size INTEGER)",
    "output_status TEXT, output_size INTEGER, route_role TEXT, "
    "effective_timeout_seconds REAL, error_code TEXT, transport_bytes INTEGER, "
    "transport_events INTEGER, transport_max_event_bytes INTEGER, "
    "transport_message_updates INTEGER, transport_snapshot_bytes INTEGER, "
    "transport_partial_bytes INTEGER, transport_delta_bytes INTEGER)",
)
_SCHEMA_SQL = _SCHEMA_SQL_V025.replace(
    "transport_delta_bytes INTEGER)",
    "transport_delta_bytes INTEGER, transport_accounted_bytes INTEGER)",
)
_MANAGED_TABLES = (
    "research_jobs",
    "attempts",
    "state_transitions",
    "artifacts",
    "provider_calls",
    "idempotency_keys",
)
_MANAGED_INDEXES = ("idx_jobs_created", "idx_jobs_queue")
_SQLITE_FAMILY_SUFFIXES = ("", "-journal", "-wal", "-shm")


class SQLiteRepository:
    def __init__(self, path: str, max_persisted_bytes: int = 10_000_000) -> None:
        self.path = path
        self.max_persisted_bytes = max_persisted_bytes
        self.db: sqlite3.Connection | None = None
        self.lock = threading.RLock()

    @staticmethod
    def _family_signatures(path: Path) -> dict[str, tuple[int, int, int, int]]:
        signatures: dict[str, tuple[int, int, int, int]] = {}
        for suffix in _SQLITE_FAMILY_SUFFIXES:
            member = Path(f"{path}{suffix}")
            try:
                value = member.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(value.st_mode):
                if suffix == "":
                    raise ValueError("database identity changed")
                raise ValueError("database sidecar failed custody validation")
            signatures[suffix] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        return signatures

    @staticmethod
    def _copy_family_member(source: Path, destination: Path) -> None:
        if not getattr(os, "O_NOFOLLOW", 0):
            raise RuntimeError("DATABASE_SCHEMA_MISMATCH")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            opened = os.fstat(fd)
            named = source.lstat()
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise RuntimeError("DATABASE_SCHEMA_MISMATCH")
            with os.fdopen(fd, "rb", closefd=False) as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            after = os.fstat(fd)
            if opened.st_size != after.st_size or opened.st_mtime_ns != after.st_mtime_ns:
                raise RuntimeError("DATABASE_SCHEMA_MISMATCH")
        finally:
            os.close(fd)

    def _preflight_existing_schema(self) -> None:
        source = Path(self.path)
        before = self._family_signatures(source)
        if "" not in before:
            if before:
                raise RuntimeError("DATABASE_SCHEMA_MISMATCH")
            return
        if before[""][2] == 0 and len(before) == 1:
            return
        with tempfile.TemporaryDirectory(prefix="thinkroom-schema-preflight-") as directory:
            copy_path = Path(directory) / source.name
            for suffix in before:
                self._copy_family_member(Path(f"{source}{suffix}"), Path(f"{copy_path}{suffix}"))
            copied_shm = Path(f"{copy_path}-shm")
            if copied_shm.exists():
                copied_shm.unlink()
            try:
                copy = sqlite3.connect(copy_path, check_same_thread=False, timeout=30)
                try:
                    has_schema = copy.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    if has_schema is None:
                        raise RuntimeError("DATABASE_SCHEMA_MISMATCH")
                    self._attest_schema(copy, allow_legacy=True)
                finally:
                    copy.close()
            except (OSError, sqlite3.Error) as exc:
                raise RuntimeError("DATABASE_SCHEMA_MISMATCH") from exc
            finally:
                if self._family_signatures(source) != before:
                    raise RuntimeError("DATABASE_SCHEMA_MISMATCH")

    def open(self, *, prepare_schema: bool = True) -> None:
        with self.lock:
            if self.db is not None:
                return
            try:
                parent = Path(self.path).parent.lstat()
            except FileNotFoundError:
                raise ValueError("database parent must already exist") from None
            if not stat.S_ISDIR(parent.st_mode):
                raise ValueError("database parent is not a directory")
            self._preflight_existing_schema()
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self.db.row_factory = sqlite3.Row
            try:
                if prepare_schema:
                    self.migrate()
            except BaseException:
                self.db.close()
                self.db = None
                raise

    def _db(self) -> sqlite3.Connection:
        if self.db is None:
            raise RuntimeError("repository is not open")
        return self.db

    def migrate(self) -> None:
        db = self._db()
        with self.lock:
            try:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is not None:
                    schema = self._attest_schema(db, allow_legacy=True)
                    if schema == "v0.2.4":
                        db.execute("ALTER TABLE provider_calls ADD COLUMN route_role TEXT")
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN effective_timeout_seconds REAL"
                        )
                        db.execute("ALTER TABLE provider_calls ADD COLUMN error_code TEXT")
                        db.execute("ALTER TABLE provider_calls ADD COLUMN transport_bytes INTEGER")
                        db.execute("ALTER TABLE provider_calls ADD COLUMN transport_events INTEGER")
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_max_event_bytes INTEGER"
                        )
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_message_updates INTEGER"
                        )
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_snapshot_bytes INTEGER"
                        )
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_partial_bytes INTEGER"
                        )
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_delta_bytes INTEGER"
                        )
                        schema = "v0.2.5"
                    if schema == "v0.2.5":
                        db.execute(
                            "ALTER TABLE provider_calls ADD COLUMN transport_accounted_bytes INTEGER"
                        )
                else:
                    for statement in _SCHEMA_SQL.split(";"):
                        if sql := statement.strip():
                            db.execute(sql)
                self._attest_schema(db)
                db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise

    @staticmethod
    def _schema_shape(db: sqlite3.Connection) -> dict[str, object]:
        def normalized_default(value: object) -> str | None:
            if value is None:
                return None
            text = " ".join(str(value).strip().split())
            while text.startswith("(") and text.endswith(")"):
                inner = text[1:-1].strip()
                if inner.count("(") != inner.count(")"):
                    break
                text = inner
            return text

        tables: dict[str, list[tuple[str, str, int, str | None, int]]] = {}
        for table in _MANAGED_TABLES:
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            tables[table] = [
                (
                    str(row[1]),
                    str(row[2]).upper(),
                    int(row[3]),
                    normalized_default(row[4]),
                    int(row[5]),
                )
                for row in rows
            ]
        indexes: dict[str, tuple[str, int, str, int, tuple[tuple[str, int], ...]]] = {}
        for table in _MANAGED_TABLES:
            for index_row in db.execute(f"PRAGMA index_list({table})").fetchall():
                index = str(index_row[1])
                columns = tuple(
                    (str(row[2]), int(row[3]))
                    for row in db.execute(f"PRAGMA index_xinfo({index})").fetchall()
                    if int(row[5]) == 1
                )
                indexes[index] = (
                    table,
                    int(index_row[2]),
                    str(index_row[3]),
                    int(index_row[4]),
                    columns,
                )
        dangerous = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in db.execute(
                "SELECT type,name,tbl_name FROM sqlite_master "
                "WHERE type IN ('trigger','view') AND tbl_name IN (?,?,?,?,?,?)",
                _MANAGED_TABLES,
            ).fetchall()
        ]
        table_sql = {
            str(row[0]): " ".join(str(row[1]).strip().split())
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?,?)",
                _MANAGED_TABLES,
            ).fetchall()
        }
        all_objects = [
            (str(row[0]), str(row[1]), str(row[2]), " ".join(str(row[3]).strip().split()))
            for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
            ).fetchall()
        ]
        return {
            "tables": tables,
            "indexes": indexes,
            "dangerous_objects": dangerous,
            "table_sql": table_sql,
            "all_objects": all_objects,
        }

    @classmethod
    def _attest_schema(cls, db: sqlite3.Connection, *, allow_legacy: bool = False) -> str:
        actual_shape = cls._schema_shape(db)
        candidates = [("v0.2.6", _SCHEMA_SQL)]
        if allow_legacy:
            candidates.append(("v0.2.5", _SCHEMA_SQL_V025))
            candidates.append(("v0.2.4", _SCHEMA_SQL_V024))
        for version, schema_sql in candidates:
            expected = sqlite3.connect(":memory:")
            try:
                expected.executescript(schema_sql)
                expected_shape = cls._schema_shape(expected)
            finally:
                expected.close()
            if cls._schema_shapes_match(actual_shape, expected_shape):
                return version
        raise RuntimeError("DATABASE_SCHEMA_MISMATCH")

    @classmethod
    def _schema_shapes_match(
        cls, actual_shape: dict[str, object], expected_shape: dict[str, object]
    ) -> bool:
        actual_tables = actual_shape["tables"]
        expected_tables = expected_shape["tables"]
        assert isinstance(actual_tables, dict) and isinstance(expected_tables, dict)
        for table in _MANAGED_TABLES:
            actual_rows = list(actual_tables[table])
            expected_rows = list(expected_tables[table])
            if table == "research_jobs":
                actual_rows = [
                    (name, affinity, required if name != "deadline" else 1, default, primary)
                    for name, affinity, required, default, primary in actual_rows
                ]
            if actual_rows != expected_rows:
                return False
        if actual_shape["indexes"] != expected_shape["indexes"]:
            return False
        if actual_shape["table_sql"] != expected_shape["table_sql"]:
            return False
        if actual_shape["all_objects"] != expected_shape["all_objects"]:
            return False
        if actual_shape["dangerous_objects"]:
            return False
        return True

    def cleanup_retention(self, retention_days: int, limit: int = 100) -> int:
        db = self._db()
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.lock, db:
            rows = db.execute(
                "SELECT job_id FROM research_jobs WHERE state IN ('succeeded','failed','cancelled') AND updated_at < ? ORDER BY updated_at LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                job_id = row["job_id"]
                for table in (
                    "artifacts",
                    "provider_calls",
                    "state_transitions",
                    "attempts",
                    "idempotency_keys",
                ):
                    db.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM research_jobs WHERE job_id=?", (job_id,))
            return len(rows)

    def count_queued(self) -> int:
        return int(
            self._db()
            .execute("SELECT COUNT(*) c FROM research_jobs WHERE state='queued'")
            .fetchone()["c"]
        )

    def create_job(
        self,
        request: ResearchRequest,
        request_hash: str,
        idem_key: str | None = None,
        max_queued: int = 100,
        deadline: datetime | None = None,
    ) -> tuple[str, bool]:
        db = self._db()
        with self.lock, db:
            if idem_key:
                row = db.execute(
                    "SELECT request_hash,job_id FROM idempotency_keys WHERE key=?", (idem_key,)
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise ValueError("IDEMPOTENCY_CONFLICT")
                    return str(row["job_id"]), True
            if (
                int(
                    db.execute(
                        "SELECT COUNT(*) c FROM research_jobs WHERE state='queued'"
                    ).fetchone()["c"]
                )
                >= max_queued
            ):
                raise RuntimeError("QUEUE_FULL")
            request_json = request.model_dump_json()
            if (
                len(request_json.encode("utf-8")) + _TERMINAL_SETTLEMENT_RESERVE
                > self.max_persisted_bytes
            ):
                raise RuntimeError("ARTIFACT_LIMIT_EXCEEDED")
            job_id, ts = str(uuid.uuid4()), now()
            if deadline is None:
                seconds = request.deadline_seconds or 900
                deadline = datetime.now(UTC) + timedelta(seconds=seconds)
            db.execute(
                "INSERT INTO research_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    request_json,
                    request_hash,
                    JobState.QUEUED.value,
                    ts,
                    ts,
                    deadline.astimezone(UTC).isoformat(),
                    0,
                    None,
                    None,
                    None,
                ),
            )
            if idem_key:
                db.execute(
                    "INSERT INTO idempotency_keys VALUES (?,?,?)", (idem_key, request_hash, job_id)
                )
            self._assert_persisted_budget_locked(job_id)
            return job_id, False

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return (
            self._db().execute("SELECT * FROM research_jobs WHERE job_id=?", (job_id,)).fetchone()
        )

    def list_jobs(self, limit: int, before: tuple[str, str] | None = None) -> list[sqlite3.Row]:
        db = self._db()
        if before:
            return db.execute(
                "SELECT * FROM research_jobs WHERE (created_at < ? OR (created_at = ? AND job_id < ?)) ORDER BY created_at DESC,job_id DESC LIMIT ?",
                (before[0], before[0], before[1], limit),
            ).fetchall()
        return db.execute(
            "SELECT * FROM research_jobs ORDER BY created_at DESC,job_id DESC LIMIT ?", (limit,)
        ).fetchall()

    def update_job(self, job_id: str, **fields: object) -> None:
        db = self._db()
        fields["updated_at"] = now()
        sql = ",".join(f"{k}=?" for k in fields)
        with self.lock, db:
            db.execute(f"UPDATE research_jobs SET {sql} WHERE job_id=?", (*fields.values(), job_id))
            self._assert_persisted_budget_locked(job_id)

    def _transition_locked(
        self,
        job_id: str,
        to_state: JobState,
        attempt_id: str | None,
        reason: str,
        correlation_id: str,
    ) -> bool:
        db = self._db()
        row = db.execute("SELECT * FROM research_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return False
        if attempt_id is not None and row["attempt_id"] != attempt_id:
            return False
        old = str(row["state"])
        if old in _TERMINAL:
            return False
        if to_state.value not in _ALLOWED.get(old, set()):
            raise ValueError(f"invalid state transition {old}->{to_state.value}")
        ts = now()
        bounded_reason = self._bounded_error(reason)
        bounded_correlation = self._bounded_error(correlation_id, 256)
        db.execute(
            "INSERT INTO state_transitions(job_id,attempt_id,from_state,to_state,at,reason,correlation_id) VALUES(?,?,?,?,?,?,?)",
            (
                job_id,
                attempt_id,
                old,
                to_state.value,
                ts,
                bounded_reason,
                bounded_correlation,
            ),
        )
        db.execute(
            "UPDATE research_jobs SET state=?,updated_at=? WHERE job_id=?",
            (to_state.value, ts, job_id),
        )
        self._assert_persisted_budget_locked(
            job_id, reserve_terminal=to_state.value not in _TERMINAL
        )
        return True

    def _attempt_accepts_output_locked(self, job_id: str, attempt_id: str) -> bool:
        row = (
            self._db()
            .execute(
                "SELECT state,cancellation_requested,attempt_id,deadline "
                "FROM research_jobs WHERE job_id=?",
                (job_id,),
            )
            .fetchone()
        )
        if not row or row["attempt_id"] != attempt_id:
            return False
        if row["state"] in _TERMINAL or row["cancellation_requested"]:
            return False
        return datetime.fromisoformat(str(row["deadline"])) > datetime.now(UTC)

    def transition(
        self,
        job_id: str,
        to_state: JobState,
        attempt_id: str | None,
        reason: str = "",
        correlation_id: str = "",
    ) -> bool:
        with self.lock, self._db():
            if (
                attempt_id is not None
                and to_state is not JobState.CANCELLED
                and not self._attempt_accepts_output_locked(job_id, attempt_id)
            ):
                return False
            return self._transition_locked(job_id, to_state, attempt_id, reason, correlation_id)

    @staticmethod
    def _bounded_error(value: object, max_bytes: int = 4000) -> str:
        raw = str(value).encode("utf-8", errors="replace")[:max_bytes]
        return raw.decode("utf-8", errors="ignore")

    def _persisted_bytes_locked(self, job_id: str) -> int:
        db = self._db()
        row = db.execute(
            "SELECT "
            "length(CAST(job_id AS BLOB)) + length(CAST(request_json AS BLOB)) + "
            "length(CAST(request_hash AS BLOB)) + length(CAST(state AS BLOB)) + "
            "length(CAST(created_at AS BLOB)) + length(CAST(updated_at AS BLOB)) + "
            "length(CAST(deadline AS BLOB)) + COALESCE(length(CAST(terminal_error AS BLOB)),0) + "
            "COALESCE(length(CAST(attempt_id AS BLOB)),0) + 16 AS job_bytes "
            "FROM research_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            return 0
        queries = (
            "SELECT COALESCE(SUM(length(CAST(attempt_id AS BLOB)) + length(CAST(job_id AS BLOB)) + length(CAST(state AS BLOB)) + length(CAST(started_at AS BLOB)) + COALESCE(length(CAST(ended_at AS BLOB)),0) + COALESCE(length(CAST(outcome AS BLOB)),0) + COALESCE(length(CAST(recovery_reason AS BLOB)),0) + COALESCE(length(CAST(backend AS BLOB)),0) + COALESCE(length(CAST(model AS BLOB)),0) + 8),0) n FROM attempts WHERE job_id=?",
            "SELECT COALESCE(SUM(length(CAST(job_id AS BLOB)) + COALESCE(length(CAST(attempt_id AS BLOB)),0) + COALESCE(length(CAST(from_state AS BLOB)),0) + length(CAST(to_state AS BLOB)) + length(CAST(at AS BLOB)) + COALESCE(length(CAST(reason AS BLOB)),0) + COALESCE(length(CAST(correlation_id AS BLOB)),0) + 8),0) n FROM state_transitions WHERE job_id=?",
            "SELECT COALESCE(SUM(length(CAST(job_id AS BLOB)) + length(CAST(attempt_id AS BLOB)) + length(CAST(kind AS BLOB)) + COALESCE(length(CAST(branch_id AS BLOB)),0) + length(CAST(payload AS BLOB)) + COALESCE(length(CAST(state AS BLOB)),0) + COALESCE(length(CAST(error AS BLOB)),0) + 8),0) n FROM artifacts WHERE job_id=?",
            "SELECT COALESCE(SUM(length(CAST(job_id AS BLOB)) + length(CAST(attempt_id AS BLOB)) + length(CAST(phase AS BLOB)) + COALESCE(length(CAST(branch_id AS BLOB)),0) + COALESCE(length(CAST(prompt_version AS BLOB)),0) + COALESCE(length(CAST(backend AS BLOB)),0) + COALESCE(length(CAST(model AS BLOB)),0) + length(CAST(started_at AS BLOB)) + COALESCE(length(CAST(ended_at AS BLOB)),0) + COALESCE(length(CAST(output_status AS BLOB)),0) + COALESCE(length(CAST(route_role AS BLOB)),0) + COALESCE(length(CAST(error_code AS BLOB)),0) + 96),0) n FROM provider_calls WHERE job_id=?",
            "SELECT COALESCE(SUM(length(CAST(key AS BLOB)) + length(CAST(request_hash AS BLOB)) + length(CAST(job_id AS BLOB))),0) n FROM idempotency_keys WHERE job_id=?",
        )
        return int(row["job_bytes"] or 0) + sum(
            int(db.execute(query, (job_id,)).fetchone()["n"] or 0) for query in queries
        )

    def _assert_persisted_budget_locked(
        self, job_id: str, *, reserve_terminal: bool = True
    ) -> None:
        reserve = _TERMINAL_SETTLEMENT_RESERVE if reserve_terminal else 0
        if self._persisted_bytes_locked(job_id) + reserve > self.max_persisted_bytes:
            raise RuntimeError("ARTIFACT_LIMIT_EXCEEDED")

    def settle_terminal(
        self,
        job_id: str,
        to_state: JobState,
        attempt_id: str,
        outcome: str,
        reason: str,
        correlation_id: str,
        error_code: str | None = None,
        error_message: object = "",
    ) -> bool:
        if to_state.value not in _TERMINAL:
            raise ValueError("terminal state required")
        db = self._db()
        with self.lock, db:
            if to_state is JobState.SUCCEEDED and not self._attempt_accepts_output_locked(
                job_id, attempt_id
            ):
                return False
            terminal_error = None
            if error_code is not None:
                bounded_code = self._bounded_error(error_code, 128) or "INTERNAL_ERROR"
                bounded_message = self._bounded_error(error_message) or "job failed"
                terminal_error = json.dumps(
                    {
                        "code": bounded_code,
                        "message": bounded_message,
                        "details": {},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                current = db.execute(
                    "SELECT COALESCE(length(CAST(terminal_error AS BLOB)),0) n "
                    "FROM research_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                old_error_bytes = int(current["n"] or 0)
                projected = (
                    self._persisted_bytes_locked(job_id)
                    - old_error_bytes
                    + len(terminal_error.encode("utf-8"))
                )
                if projected > self.max_persisted_bytes:
                    terminal_error = json.dumps(
                        {
                            "code": "ARTIFACT_LIMIT_EXCEEDED",
                            "message": "job persisted-byte limit exceeded",
                            "details": {},
                        },
                        separators=(",", ":"),
                    )
                if (
                    self._persisted_bytes_locked(job_id)
                    - old_error_bytes
                    + len(terminal_error.encode("utf-8"))
                    > self.max_persisted_bytes
                ):
                    raise RuntimeError("ARTIFACT_LIMIT_EXCEEDED")
            if not self._transition_locked(
                job_id,
                to_state,
                attempt_id,
                self._bounded_error(reason),
                correlation_id,
            ):
                return False
            ts = now()
            db.execute(
                "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                (terminal_error, ts, job_id),
            )
            bounded_outcome = self._bounded_error(outcome, 128)
            db.execute(
                "UPDATE attempts SET state='terminal',ended_at=?,outcome=? WHERE attempt_id=?",
                (ts, bounded_outcome, attempt_id),
            )
            self._assert_persisted_budget_locked(job_id, reserve_terminal=False)
            return True

    def claim_next_job(
        self,
        max_attempts: int,
        correlation_id: str,
        backend: str | None = None,
        model: str | None = None,
    ) -> tuple[str, str] | None:
        db = self._db()
        with self.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM research_jobs WHERE state='queued' AND cancellation_requested=0 ORDER BY created_at,job_id LIMIT 1"
                ).fetchone()
                if not row:
                    db.commit()
                    return None
                deadline = datetime.fromisoformat(row["deadline"])
                if deadline <= datetime.now(UTC):
                    self._transition_locked(
                        row["job_id"],
                        JobState.FAILED,
                        None,
                        "deadline before claim",
                        correlation_id,
                    )
                    db.execute(
                        "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                        (
                            json.dumps(
                                {
                                    "code": "DEADLINE_EXCEEDED",
                                    "message": "job deadline exceeded",
                                    "details": {},
                                }
                            ),
                            now(),
                            row["job_id"],
                        ),
                    )
                    db.commit()
                    return None
                previous_policy = db.execute(
                    "SELECT backend,model FROM attempts WHERE job_id=? ORDER BY number LIMIT 1",
                    (row["job_id"],),
                ).fetchone()
                if previous_policy is not None:
                    previous_backend = previous_policy["backend"]
                    previous_model = previous_policy["model"]
                    failover_policy = "->" in str(previous_backend or "") or "->" in str(
                        backend or ""
                    )
                    if failover_policy and (previous_backend != backend or previous_model != model):
                        self._transition_locked(
                            row["job_id"],
                            JobState.FAILED,
                            None,
                            "provider policy drift before retry",
                            correlation_id,
                        )
                        db.execute(
                            "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                            (
                                json.dumps(
                                    {
                                        "code": "PROVIDER_POLICY_DRIFT",
                                        "message": "provider policy changed before retry",
                                        "details": {},
                                    }
                                ),
                                now(),
                                row["job_id"],
                            ),
                        )
                        db.commit()
                        return None
                attempt_number = int(
                    db.execute(
                        "SELECT COALESCE(MAX(number),0)+1 n FROM attempts WHERE job_id=?",
                        (row["job_id"],),
                    ).fetchone()["n"]
                )
                if attempt_number > max_attempts:
                    self._transition_locked(
                        row["job_id"],
                        JobState.FAILED,
                        None,
                        "retry ceiling reached",
                        correlation_id,
                    )
                    db.execute(
                        "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                        (
                            json.dumps(
                                {
                                    "code": "RETRY_EXHAUSTED",
                                    "message": "attempt ceiling reached",
                                    "details": {},
                                }
                            ),
                            now(),
                            row["job_id"],
                        ),
                    )
                    db.commit()
                    return None
                aid = str(uuid.uuid4())
                ts = now()
                db.execute(
                    "INSERT INTO attempts(attempt_id,job_id,number,state,started_at,ended_at,outcome,recovery_reason,backend,model) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        row["job_id"],
                        attempt_number,
                        "active",
                        ts,
                        None,
                        None,
                        None,
                        backend,
                        model,
                    ),
                )
                db.execute(
                    "UPDATE research_jobs SET attempt_id=?,attempt_number=?,updated_at=? WHERE job_id=?",
                    (aid, attempt_number, ts, row["job_id"]),
                )
                self._transition_locked(
                    row["job_id"], JobState.FRAMING, aid, "worker claim", correlation_id
                )
                db.commit()
                return str(row["job_id"]), aid
            except Exception:
                db.rollback()
                raise

    def recover_startup(self, max_attempts: int) -> None:
        """Make cancellation durable before any queued/active job can be claimed."""
        db = self._db()
        with self.lock, db:
            stranded = db.execute(
                "SELECT a.attempt_id,j.state FROM attempts a "
                "JOIN research_jobs j ON j.job_id=a.job_id "
                "WHERE a.state='active' AND j.state IN ('succeeded','failed','cancelled')"
            ).fetchall()
            for row in stranded:
                db.execute(
                    "UPDATE attempts SET state='terminal',ended_at=?,outcome=?,recovery_reason=? "
                    "WHERE attempt_id=?",
                    (now(), row["state"], "terminal attempt reconciliation", row["attempt_id"]),
                )
            open_terminal_calls = db.execute(
                "SELECT p.id,j.state FROM provider_calls p "
                "JOIN research_jobs j ON j.job_id=p.job_id "
                "WHERE p.ended_at IS NULL AND j.state IN ('succeeded','failed','cancelled')"
            ).fetchall()
            for row in open_terminal_calls:
                status = "cancelled" if row["state"] == "cancelled" else "uncertain"
                db.execute(
                    "UPDATE provider_calls SET ended_at=?,output_status=? "
                    "WHERE id=? AND ended_at IS NULL",
                    (now(), status, row["id"]),
                )
            cancelled = db.execute(
                "SELECT j.job_id,j.state,j.attempt_id,a.attempt_id AS active_attempt "
                "FROM research_jobs j LEFT JOIN attempts a ON a.attempt_id=j.attempt_id "
                "WHERE j.cancellation_requested=1 AND j.state NOT IN ('succeeded','failed','cancelled')"
            ).fetchall()
            for row in cancelled:
                aid = row["attempt_id"]
                if row["active_attempt"]:
                    db.execute(
                        "UPDATE attempts SET state='abandoned',ended_at=?,outcome='cancelled' WHERE attempt_id=?",
                        (now(), row["active_attempt"]),
                    )
                self._transition_locked(
                    row["job_id"],
                    JobState.CANCELLED,
                    aid,
                    "startup cancellation recovery",
                    "recovery",
                )
                db.execute(
                    "UPDATE research_jobs SET terminal_error=?,cancellation_requested=1,updated_at=? WHERE job_id=?",
                    (
                        json.dumps(
                            {
                                "code": "CANCELLED",
                                "message": "job cancelled during startup recovery",
                                "details": {},
                            }
                        ),
                        now(),
                        row["job_id"],
                    ),
                )
            rows = db.execute(
                "SELECT a.attempt_id,a.job_id,a.backend,j.attempt_number,j.state "
                "FROM attempts a JOIN research_jobs j ON j.job_id=a.job_id "
                "WHERE a.state='active' "
                "AND j.state IN ('framing','rolling_out','critiquing','synthesizing') "
                "AND j.cancellation_requested=0"
            ).fetchall()
            for row in rows:
                physical_calls = int(
                    db.execute(
                        "SELECT COUNT(*) n FROM provider_calls WHERE attempt_id=?",
                        (row["attempt_id"],),
                    ).fetchone()["n"]
                )
                if "->" in str(row["backend"] or "") and physical_calls:
                    ts = now()
                    db.execute(
                        "UPDATE provider_calls SET ended_at=?,output_status='uncertain' "
                        "WHERE attempt_id=? AND ended_at IS NULL",
                        (ts, row["attempt_id"]),
                    )
                    db.execute(
                        "UPDATE attempts SET state='abandoned',ended_at=?,outcome=?,recovery_reason=? "
                        "WHERE attempt_id=?",
                        (
                            ts,
                            "provider_attempt_interrupted",
                            "failover attempt requires reconciliation",
                            row["attempt_id"],
                        ),
                    )
                    self._transition_locked(
                        row["job_id"],
                        JobState.FAILED,
                        row["attempt_id"],
                        "failover attempt interrupted",
                        "recovery",
                    )
                    db.execute(
                        "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                        (
                            json.dumps(
                                {
                                    "code": "PROVIDER_ATTEMPT_INTERRUPTED",
                                    "message": "failover attempt interrupted; automatic replay refused",
                                    "details": {},
                                }
                            ),
                            ts,
                            row["job_id"],
                        ),
                    )
                    continue
                db.execute(
                    "UPDATE attempts SET state='abandoned',ended_at=?,outcome='abandoned' WHERE attempt_id=?",
                    (now(), row["attempt_id"]),
                )
                if int(row["attempt_number"] or 0) < max_attempts:
                    self._transition_locked(
                        row["job_id"],
                        JobState.QUEUED,
                        row["attempt_id"],
                        "startup recovery",
                        "recovery",
                    )
                    db.execute(
                        "UPDATE research_jobs SET attempt_id=NULL,terminal_error=NULL,updated_at=? WHERE job_id=?",
                        (now(), row["job_id"]),
                    )
                else:
                    self._transition_locked(
                        row["job_id"],
                        JobState.FAILED,
                        row["attempt_id"],
                        "retry ceiling reached",
                        "recovery",
                    )
                    db.execute(
                        "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                        (
                            json.dumps(
                                {
                                    "code": "RETRY_EXHAUSTED",
                                    "message": "attempt ceiling reached",
                                    "details": {},
                                }
                            ),
                            now(),
                            row["job_id"],
                        ),
                    )

    def request_cancel(self, job_id: str) -> bool:
        db = self._db()
        with self.lock, db:
            row = db.execute("SELECT * FROM research_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return False
            if row["state"] == JobState.QUEUED.value:
                self._transition_locked(
                    job_id, JobState.CANCELLED, None, "cancelled before claim", "cancel"
                )
                db.execute(
                    "UPDATE research_jobs SET terminal_error=?,updated_at=? WHERE job_id=?",
                    (
                        json.dumps(
                            {
                                "code": "CANCELLED",
                                "message": "cancelled before execution",
                                "details": {},
                            }
                        ),
                        now(),
                        job_id,
                    ),
                )
            elif row["state"] not in _TERMINAL:
                db.execute(
                    "UPDATE research_jobs SET cancellation_requested=1,updated_at=? WHERE job_id=?",
                    (now(), job_id),
                )
            return True

    def is_cancelled(self, job_id: str) -> bool:
        row = self.get_job(job_id)
        return bool(
            row and (row["cancellation_requested"] or row["state"] == JobState.CANCELLED.value)
        )

    def put_artifact(
        self,
        job_id: str,
        attempt_id: str,
        kind: str,
        payload: Any,
        branch_id: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> int:
        raw = json.dumps(payload, ensure_ascii=False)
        bounded_error = self._bounded_error(error) if error is not None else None
        db = self._db()
        with self.lock, db:
            if not self._attempt_accepts_output_locked(job_id, attempt_id):
                raise RuntimeError("CANCELLED_OR_STALE_ATTEMPT")
            added = len(raw.encode("utf-8")) + len(
                bounded_error.encode("utf-8") if bounded_error is not None else b""
            )
            if (
                self._persisted_bytes_locked(job_id) + added + _TERMINAL_SETTLEMENT_RESERVE
                > self.max_persisted_bytes
            ):
                raise RuntimeError("ARTIFACT_LIMIT_EXCEEDED")
            cursor = db.execute(
                "INSERT INTO artifacts(job_id,attempt_id,kind,branch_id,payload,state,error) VALUES(?,?,?,?,?,?,?)",
                (job_id, attempt_id, kind, branch_id, raw, state, bounded_error),
            )
            self._assert_persisted_budget_locked(job_id)
            return int(cursor.lastrowid or 0)

    def get_artifacts(self, job_id: str, attempt_id: str | None = None) -> list[sqlite3.Row]:
        db = self._db()
        if attempt_id:
            return db.execute(
                "SELECT * FROM artifacts WHERE job_id=? AND attempt_id=? ORDER BY id",
                (job_id, attempt_id),
            ).fetchall()
        return db.execute(
            "SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def provider_calls(self, job_id: str, attempt_id: str | None = None) -> list[sqlite3.Row]:
        db = self._db()
        if attempt_id is not None:
            return db.execute(
                "SELECT * FROM provider_calls WHERE job_id=? AND attempt_id=? ORDER BY id",
                (job_id, attempt_id),
            ).fetchall()
        return db.execute(
            "SELECT * FROM provider_calls WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def add_provider_call(self, values: dict[str, object]) -> int:
        db = self._db()
        keys = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.lock, db:
            job_id = values.get("job_id")
            attempt_id = values.get("attempt_id")
            if type(job_id) is not str or type(attempt_id) is not str:
                raise ValueError("provider call requires job_id and attempt_id")
            if not self._attempt_accepts_output_locked(job_id, attempt_id):
                raise RuntimeError("CANCELLED_OR_STALE_ATTEMPT")
            cur = db.execute(
                f"INSERT INTO provider_calls({keys}) VALUES({marks})", tuple(values.values())
            )
            self._assert_persisted_budget_locked(job_id)
            return int(cur.lastrowid or 0)

    def finish_provider_call(self, call_id: int, attempt_id: str, **values: object) -> bool:
        db = self._db()
        sql = ", ".join(f"{k}=?" for k in values)
        with self.lock, db:
            row = db.execute(
                "SELECT job_id,attempt_id FROM provider_calls WHERE id=?", (call_id,)
            ).fetchone()
            if row is None:
                raise ValueError("provider call not found")
            job_id = str(row["job_id"])
            if row["attempt_id"] != attempt_id or not self._attempt_accepts_output_locked(
                job_id, attempt_id
            ):
                return False
            updated = db.execute(
                f"UPDATE provider_calls SET {sql} WHERE id=? AND attempt_id=?",
                (*values.values(), call_id, attempt_id),
            )
            if updated.rowcount != 1:
                return False
            self._assert_persisted_budget_locked(job_id)
            return True

    def settle_cancelled_provider_call(
        self,
        call_id: int,
        attempt_id: str,
        *,
        ended_at: str,
        output_size: int = 0,
    ) -> bool:
        """Close one started audit row without admitting late provider output."""
        db = self._db()
        with self.lock, db:
            row = db.execute(
                "SELECT p.job_id,p.attempt_id,p.ended_at,j.attempt_id AS current_attempt,"
                "j.cancellation_requested FROM provider_calls p "
                "JOIN research_jobs j ON j.job_id=p.job_id WHERE p.id=?",
                (call_id,),
            ).fetchone()
            if (
                row is None
                or row["attempt_id"] != attempt_id
                or row["current_attempt"] != attempt_id
                or row["ended_at"] is not None
                or int(row["cancellation_requested"] or 0) != 1
            ):
                return False
            updated = db.execute(
                "UPDATE provider_calls SET ended_at=?,output_status='cancelled',output_size=? "
                "WHERE id=? AND attempt_id=? AND ended_at IS NULL",
                (ended_at, output_size, call_id, attempt_id),
            )
            if updated.rowcount != 1:
                return False
            self._assert_persisted_budget_locked(str(row["job_id"]))
            return True

    def end_attempt(self, attempt_id: str, outcome: str) -> None:
        with self.lock, self._db():
            db = self._db()
            row = db.execute(
                "SELECT job_id FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("attempt not found")
            db.execute(
                "UPDATE attempts SET state='terminal',ended_at=?,outcome=? WHERE attempt_id=?",
                (now(), outcome, attempt_id),
            )
            self._assert_persisted_budget_locked(str(row["job_id"]))

    def attempts(self, job_id: str) -> list[sqlite3.Row]:
        return (
            self._db()
            .execute("SELECT * FROM attempts WHERE job_id=? ORDER BY number", (job_id,))
            .fetchall()
        )

    def transitions(self, job_id: str) -> list[sqlite3.Row]:
        return (
            self._db()
            .execute("SELECT * FROM state_transitions WHERE job_id=? ORDER BY id", (job_id,))
            .fetchall()
        )

    def close(self) -> None:
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
