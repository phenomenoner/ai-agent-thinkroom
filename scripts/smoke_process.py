"""Production-process smoke: completion, restart persistence, and exclusive lock."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def get(url: str) -> tuple[int, dict]:
    try:
        with urlopen(url, timeout=1) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_ready(port: int, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("process exited before readiness")
        try:
            if get(f"http://127.0.0.1:{port}/health/ready")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("process did not become ready")


def available_loopback_port(*, exclude: int | None = None) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port != exclude:
            return port


port = available_loopback_port()
lock_probe_port = available_loopback_port(exclude=port)
with tempfile.TemporaryDirectory() as directory:
    env = {
        **os.environ,
        "THINKROOM_DATABASE_URL": f"sqlite+aiosqlite:///{directory}/thinkroom.db",
        "THINKROOM_BACKEND": "scripted",
    }
    command = [sys.executable, "-m", "thinkroom", "serve", "--port", str(port)]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_ready(port, process)
        request = Request(
            f"http://127.0.0.1:{port}/api/v1/research",
            data=json.dumps({"question": "Should we choose this important option?"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            job = json.loads(response.read())["job_id"]
        for _ in range(100):
            status, detail = get(f"http://127.0.0.1:{port}/api/v1/research/{job}")
            if detail.get("state") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        if detail["state"] != "succeeded":
            raise RuntimeError("process smoke: submitted job did not succeed")
        second_command = [
            sys.executable,
            "-m",
            "thinkroom",
            "serve",
            "--port",
            str(lock_probe_port),
        ]
        second = subprocess.Popen(
            second_command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            time.sleep(1)
            try:
                urlopen(f"http://127.0.0.1:{lock_probe_port}/health/live", timeout=0.5)
            except OSError:
                pass
            else:
                raise AssertionError("second instance became reachable despite service lock")
        finally:
            if second.poll() is None:
                second.terminate()
                second.wait(timeout=5)
    finally:
        process.terminate()
        process.wait(timeout=5)
    restarted = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_ready(port, restarted)
        status, detail = get(f"http://127.0.0.1:{port}/api/v1/research/{job}")
        if status != 200 or detail["state"] != "succeeded":
            raise RuntimeError("process smoke: restart readback failed")
    finally:
        restarted.terminate()
        restarted.wait(timeout=5)
print("process smoke: ok")
