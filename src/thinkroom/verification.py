from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .skills import _manifest


def verify_package() -> dict[str, Any]:
    expected = {
        "thinkroom-install/SKILL.md",
        "thinkroom-install/agents/openai.yaml",
        "thinkroom-operate/SKILL.md",
        "thinkroom-operate/agents/openai.yaml",
        "thinkroom-trigger/SKILL.md",
        "thinkroom-trigger/agents/openai.yaml",
    }
    manifest, _, payloads = _manifest()
    declared = {entry["path"] for entry in manifest["entries"]}
    if declared != expected or set(payloads) != expected:
        raise RuntimeError("package verification: bundled Skills payload set mismatch")
    for relative in expected:
        if relative.endswith("/agents/openai.yaml") and b"interface:" not in payloads[relative]:
            raise RuntimeError(f"package verification: invalid Codex metadata: {relative}")
    return {"status": "ok", "managed_payloads": len(expected)}


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urlopen(url, timeout=1) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_ready(port: int, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("process verification: process exited before readiness")
        try:
            if _get(f"http://127.0.0.1:{port}/health/ready")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("process verification: process did not become ready")


def _available_loopback_port(*, exclude: int | None = None) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port != exclude:
            return port


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def verify_process() -> dict[str, Any]:
    port = _available_loopback_port()
    lock_probe_port = _available_loopback_port(exclude=port)
    with tempfile.TemporaryDirectory(prefix="thinkroom-process-verification-") as directory:
        env = {
            **os.environ,
            "THINKROOM_DATABASE_URL": f"sqlite+aiosqlite:///{directory}/thinkroom.db",
            "THINKROOM_BACKEND": "scripted",
        }
        command = [sys.executable, "-m", "thinkroom", "serve", "--port", str(port)]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        detail: dict[str, Any] = {}
        try:
            _wait_ready(port, process)
            request = Request(
                f"http://127.0.0.1:{port}/api/v1/research",
                data=json.dumps({"question": "Should we choose this important option?"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                job = json.loads(response.read())["job_id"]
            for _ in range(100):
                _, detail = _get(f"http://127.0.0.1:{port}/api/v1/research/{job}")
                if detail.get("state") in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            if detail.get("state") != "succeeded":
                raise RuntimeError("process verification: submitted job did not succeed")
            second = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "thinkroom",
                    "serve",
                    "--port",
                    str(lock_probe_port),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(1)
                try:
                    urlopen(f"http://127.0.0.1:{lock_probe_port}/health/live", timeout=0.5)
                except OSError:
                    pass
                else:
                    raise RuntimeError(
                        "process verification: second instance became reachable despite service lock"
                    )
            finally:
                _stop_process(second)
        finally:
            _stop_process(process)

        restarted = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_ready(port, restarted)
            status, detail = _get(f"http://127.0.0.1:{port}/api/v1/research/{job}")
            if status != 200 or detail.get("state") != "succeeded":
                raise RuntimeError("process verification: restart readback failed")
        finally:
            _stop_process(restarted)
    return {"status": "ok", "restart_readback": True, "exclusive_lock": True}
