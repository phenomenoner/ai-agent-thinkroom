from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite+aiosqlite:///.data/thinkroom.db"
    backend: str = "scripted"
    max_concurrency: int = 3
    max_queued_jobs: int = 100
    job_timeout_seconds: int = 900
    backend_timeout_seconds: int = 180
    max_job_attempts: int = 2
    max_backend_response_bytes: int = 1_000_000
    max_context_bytes: int = 1_000_000
    max_backend_output_tokens: int = 8192
    max_persisted_bytes_per_job: int = 10_000_000
    retention_days: int = 30
    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        fields = cls.__dataclass_fields__
        vals: dict[str, object] = {}
        for name, field in fields.items():
            env = os.getenv("THINKROOM_" + name.upper())
            vals[name] = env if env is not None else field.default
        vals.update(overrides)
        for n in (
            "max_concurrency",
            "max_queued_jobs",
            "job_timeout_seconds",
            "backend_timeout_seconds",
            "max_job_attempts",
            "max_backend_response_bytes",
            "max_context_bytes",
            "max_backend_output_tokens",
            "max_persisted_bytes_per_job",
            "retention_days",
            "port",
        ):
            vals[n] = int(str(vals[n]))
        s = cls(**vals)  # type: ignore[arg-type]
        s.validate()
        return s

    @property
    def db_path(self) -> str:
        path = urlparse(self.database_url).path
        if path.startswith("/.data/"):
            return path[1:]
        return path or ".data/thinkroom.db"

    def validate(self) -> None:
        if self.backend not in {"scripted", "openai", "prime_agent"}:
            raise ValueError("invalid backend")
        if urlparse(self.database_url).scheme not in {"sqlite", "sqlite+aiosqlite"}:
            raise ValueError("only SQLite database URLs are supported")
        bounds = {
            "max_concurrency": (1, 12),
            "max_queued_jobs": (1, 10000),
            "job_timeout_seconds": (30, 7200),
            "backend_timeout_seconds": (10, 1800),
            "max_job_attempts": (1, 5),
            "max_backend_response_bytes": (16384, 10_000_000),
            "max_context_bytes": (16384, 10_000_000),
            "max_backend_output_tokens": (256, 32768),
            "max_persisted_bytes_per_job": (1_000_000, 100_000_000),
            "retention_days": (1, 3650),
            "port": (1, 65535),
        }
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if not lo <= value <= hi:
                raise ValueError(f"{name} outside validated range")
        if self.backend_timeout_seconds > self.job_timeout_seconds:
            raise ValueError("backend timeout cannot exceed job timeout")
        try:
            bind_address = ip_address(self.host)
        except ValueError as exc:
            raise ValueError("bind host must be a literal loopback IP address") from exc
        if not bind_address.is_loopback:
            raise ValueError("non-loopback bind addresses are not supported")
        if self.log_level.upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("invalid log level")
