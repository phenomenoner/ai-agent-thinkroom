"""Build, run, verify, and recycle a bounded Docker test instance."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

DOCKER = os.getenv("DOCKER_BIN", "docker")
CONTEXT = os.getenv("THINKROOM_DOCKER_CONTEXT", str(Path(__file__).parents[1]))
IMAGE = os.getenv("THINKROOM_DOCKER_IMAGE", "thinkroom:verification")
CONTAINER = os.getenv("THINKROOM_DOCKER_CONTAINER", "thinkroom-verification")
PORT = int(os.getenv("THINKROOM_DOCKER_PORT", "18788"))
OWNERSHIP_LABEL = "com.thinkroom.verification"

if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", CONTAINER):
    raise ValueError("invalid container name")
if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}(?::[A-Za-z0-9_.-]{1,128})?", IMAGE):
    raise ValueError("invalid image reference")
if not 1 <= PORT <= 65535:
    raise ValueError("invalid host port")
context_path = Path(CONTEXT).resolve(strict=True)
if not context_path.is_dir():
    raise ValueError("Docker context is not a directory")
CONTEXT = str(context_path)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([DOCKER, *args], check=check, capture_output=True, text=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def assert_owned_resource(kind: str, name: str) -> bool:
    label_path = ".Labels" if kind == "network" else ".Config.Labels"
    result = run(
        kind,
        "inspect",
        "--format",
        f'{{{{ index {label_path} "{OWNERSHIP_LABEL}" }}}}',
        "--",
        name,
        check=False,
    )
    if result.returncode != 0:
        return False
    if result.stdout.strip() != "true":
        raise RuntimeError(f"refusing to remove unowned {kind} resource: {name}")
    return True


def cleanup() -> None:
    if assert_owned_resource("container", CONTAINER):
        run("container", "rm", "--force", "--", CONTAINER)
    if assert_owned_resource("image", IMAGE):
        run("image", "rm", "--force", "--", IMAGE)


def get_json(url: str) -> tuple[int, dict]:
    with urlopen(url, timeout=2) as response:
        return response.status, json.loads(response.read())


cleanup()
try:
    run("build", "--label", f"{OWNERSHIP_LABEL}=true", "--tag", IMAGE, CONTEXT)
    user = run("image", "inspect", "--format", "{{.Config.User}}", "--", IMAGE).stdout.strip()
    require(user == "10001:10001", "Docker smoke: image user mismatch")
    run(
        "run",
        "--detach",
        "--name",
        CONTAINER,
        "--label",
        f"{OWNERSHIP_LABEL}=true",
        "--publish",
        f"127.0.0.1:{PORT}:8787",
        "--read-only",
        "--tmpfs",
        "/data:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        IMAGE,
    )
    health = ""
    for _ in range(90):
        state = run(
            "inspect", "--format", "{{.State.Status}} {{.State.Health.Status}}", "--", CONTAINER
        )
        status, health = state.stdout.strip().split(maxsplit=1)
        if status == "exited":
            raise RuntimeError(run("logs", "--", CONTAINER, check=False).stdout[-4000:])
        if health == "healthy":
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"container did not become healthy: {health}")

    request = Request(
        f"http://127.0.0.1:{PORT}/api/v1/research",
        data=json.dumps(
            {"question": "Should we choose this important option?", "branch_count": 2}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=3) as response:
        require(response.status == 202, "Docker smoke: submit status mismatch")
        require(bool(response.headers["Location"]), "Docker smoke: Location header missing")
        job_id = json.loads(response.read())["job_id"]
    detail: dict = {}
    for _ in range(120):
        _, detail = get_json(f"http://127.0.0.1:{PORT}/api/v1/research/{job_id}")
        if detail.get("state") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    require(detail.get("state") == "succeeded", "Docker smoke: job did not succeed")
    print(json.dumps({"status": "ok", "health": health, "state": detail["state"], "user": user}))
finally:
    cleanup()

require(
    run("container", "inspect", "--", CONTAINER, check=False).returncode != 0,
    "Docker smoke: container cleanup failed",
)
require(
    run("image", "inspect", "--", IMAGE, check=False).returncode != 0,
    "Docker smoke: image cleanup failed",
)
print(json.dumps({"cleanup": "verified", "container": CONTAINER, "image": IMAGE}))
