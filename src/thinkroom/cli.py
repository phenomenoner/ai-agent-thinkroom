from __future__ import annotations

import json
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

import typer
import uvicorn

from .api import create_app
from .config import Settings
from .sdk import ThinkroomClient, ThinkroomError
from .service import ThinkroomService

app = typer.Typer(help="Thinkroom AI research")
skills_app = typer.Typer(help="Manage Agent Skills")
verify_app = typer.Typer(help="Verify an installed Thinkroom runtime")
app.add_typer(skills_app, name="skills")
app.add_typer(verify_app, name="verify")
_T = TypeVar("_T")


class AgentProfile(StrEnum):
    CODEX = "codex"
    HERMES = "hermes"


def _skills_target(target: str | None, profile: AgentProfile | None) -> str:
    if (target is None) == (profile is None):
        raise typer.BadParameter("choose exactly one of --target or --profile")
    if target is not None:
        return target
    assert profile is not None
    if profile is AgentProfile.CODEX:
        return str(Path.home() / ".agents" / "skills")
    configured_hermes_home = os.environ.get("HERMES_HOME")
    if configured_hermes_home is None:
        hermes_home = Path.home() / ".hermes"
    else:
        if not configured_hermes_home:
            raise typer.BadParameter("HERMES_HOME may not be empty")
        hermes_home = Path(configured_hermes_home)
        if not hermes_home.is_absolute():
            raise typer.BadParameter("HERMES_HOME must be an absolute path")
    return str(hermes_home / "skills")


def _remote(operation: Callable[[], _T]) -> _T:  # noqa: UP047
    try:
        return operation()
    except ThinkroomError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


@app.command()
def research(
    question: str = typer.Option(..., "--question"),
    context: str | None = None,
    domain: str = "generic",
    branch_count: int = 3,
    idempotency_key: str | None = None,
    endpoint: str | None = None,
) -> None:
    print(
        json.dumps(
            _remote(
                lambda: ThinkroomClient(endpoint).research(
                    question,
                    context=context,
                    domain=domain,
                    branch_count=branch_count,
                    idempotency_key=idempotency_key,
                )
            ),
            indent=2,
        )
    )


@app.command("get")
def get(job_id: str, endpoint: str | None = None) -> None:
    print(json.dumps(_remote(lambda: ThinkroomClient(endpoint).get(job_id)), indent=2))


@app.command("list")
def list_jobs(endpoint: str | None = None, limit: int = 20, cursor: str | None = None) -> None:
    print(
        json.dumps(
            _remote(lambda: ThinkroomClient(endpoint).list(limit=limit, cursor=cursor)), indent=2
        )
    )


@app.command()
def cancel(job_id: str, endpoint: str | None = None) -> None:
    print(json.dumps(_remote(lambda: ThinkroomClient(endpoint).cancel(job_id)), indent=2))


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    overrides: dict[str, object] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    settings = Settings.from_env(**overrides)
    uvicorn.run(create_app(ThinkroomService(settings)), host=settings.host, port=settings.port)


@app.command()
def mcp() -> None:
    from .mcp import run_stdio

    run_stdio()


@skills_app.command("install")
def skills_install(
    target: Annotated[str | None, typer.Option("--target")] = None,
    profile: Annotated[AgentProfile | None, typer.Option("--profile", case_sensitive=False)] = None,
) -> None:
    from .skills import install

    print(json.dumps(install(_skills_target(target, profile)), indent=2))


@skills_app.command("status")
def skills_status(
    target: Annotated[str | None, typer.Option("--target")] = None,
    profile: Annotated[AgentProfile | None, typer.Option("--profile", case_sensitive=False)] = None,
) -> None:
    from .skills import status

    print(json.dumps(status(_skills_target(target, profile)), indent=2))


@skills_app.command("uninstall")
def skills_uninstall(
    target: Annotated[str | None, typer.Option("--target")] = None,
    profile: Annotated[AgentProfile | None, typer.Option("--profile", case_sensitive=False)] = None,
) -> None:
    from .skills import uninstall

    uninstall(_skills_target(target, profile))
    print("uninstalled")


@verify_app.command("package")
def verify_package_command() -> None:
    from .verification import verify_package

    print(json.dumps(verify_package(), sort_keys=True))


@verify_app.command("process")
def verify_process_command() -> None:
    from .verification import verify_process

    print(json.dumps(verify_process(), sort_keys=True))


if __name__ == "__main__":
    app()
