from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

import typer
import uvicorn

from .api import create_app
from .config import Settings
from .sdk import ThinkroomClient, ThinkroomError
from .service import ThinkroomService

app = typer.Typer(help="Thinkroom AI research")
skills_app = typer.Typer(help="Manage Agent Skills")
app.add_typer(skills_app, name="skills")
_T = TypeVar("_T")


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
def skills_install(target: str = typer.Option(..., "--target")) -> None:
    from .skills import install

    print(json.dumps(install(target), indent=2))


@skills_app.command("status")
def skills_status(target: str = typer.Option(..., "--target")) -> None:
    from .skills import status

    print(json.dumps(status(target), indent=2))


@skills_app.command("uninstall")
def skills_uninstall(target: str = typer.Option(..., "--target")) -> None:
    from .skills import uninstall

    uninstall(target)
    print("uninstalled")


if __name__ == "__main__":
    app()
