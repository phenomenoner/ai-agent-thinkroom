from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "build_release.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("thinkroom_build_release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_builder_pins_frontend_backend_and_epoch(tmp_path, monkeypatch):
    module = _load_builder()
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    real_uv = module.shutil.which("uv")
    assert real_uv is not None

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "20310102030405")
    monkeypatch.setattr(module.shutil, "which", lambda name: real_uv)
    monkeypatch.setattr(module, "_sha256_file", lambda path: module.REQUIRED_UV_SHA256)
    monkeypatch.setattr(module, "_validate_wheel", lambda path: None)
    monkeypatch.setattr(module, "_validate_sdist", lambda path: None)

    def fake_check_output(command, **kwargs):
        if "--version" in command:
            return "uv 0.12.3 (x86_64-unknown-linux-gnu)\n"
        assert "export" in command
        return (ROOT / "requirements-production.txt").read_text()

    monkeypatch.setattr(module.subprocess, "check_output", fake_check_output)

    def fake_run(command, *, cwd, env, check):
        assert check is True
        calls.append((command, cwd, env))
        output.mkdir(parents=True, exist_ok=True)
        (output / ".gitignore").write_text("*\n")
        (output / "thinkroom-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (output / "thinkroom-0.1.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    output = tmp_path / "dist"
    module.build_release(output, project_root=ROOT)

    command, cwd, observed_env = calls[0]
    assert Path(command[0]).name == "uv"
    assert "thinkroom-trusted-uv-" in str(Path(command[0]).parent)
    assert command[1:] == [
        "--no-config",
        "build",
        str(ROOT.resolve()),
        "--build-constraints",
        str((ROOT / "build-constraints.txt").resolve()),
        "--require-hashes",
        "--out-dir",
        str(output.resolve()),
    ]
    assert cwd == ROOT.resolve()
    assert observed_env is calls[0][2]
    assert calls[0][2]["SOURCE_DATE_EPOCH"] == "0"
    assert "UV_WORKING_DIR" not in calls[0][2]
    assert "UV_PROJECT" not in calls[0][2]
    assert "UV_CONFIG_FILE" not in calls[0][2]
    assert "UV_NO_BUILD_ISOLATION" not in calls[0][2]
    assert calls[0][2]["UV_NO_CONFIG"] == "1"
    assert module.REQUIRED_UV_VERSION == "0.12.3"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["build-system"]["requires"] == ["hatchling==1.32.0"]
    assert ".venv" in pyproject["tool"]["ruff"]["extend-exclude"]
    assert pyproject["tool"]["ruff"]["force-exclude"] is True
    constraints = (ROOT / "build-constraints.txt").read_text()
    assert "hatchling==1.32.0" in constraints
    assert constraints.count("--hash=sha256:") == 12
    assert sorted(path.name for path in output.iterdir()) == [
        "thinkroom-0.1.0-py3-none-any.whl",
        "thinkroom-0.1.0.tar.gz",
    ]


def test_release_builder_rejects_output_inside_source_tree(tmp_path):
    module = _load_builder()
    with pytest.raises(ValueError, match="outside the project tree"):
        module.build_release(ROOT / "dist-release", project_root=ROOT)


def test_release_builder_rejects_stale_production_requirements(monkeypatch):
    module = _load_builder()
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: "stale-generated-requirements\n",
    )

    with pytest.raises(RuntimeError, match="requirements-production.txt is stale"):
        module._verify_production_requirements(Path("/trusted/uv"), ROOT, {})


def test_release_builder_rejects_wrong_uv_version(tmp_path, monkeypatch):
    module = _load_builder()
    real_uv = module.shutil.which("uv")
    assert real_uv is not None
    monkeypatch.setattr(module.shutil, "which", lambda name: real_uv)
    monkeypatch.setattr(module, "_sha256_file", lambda path: module.REQUIRED_UV_SHA256)
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: "uv 99.0.0 (x86_64-unknown-linux-gnu)\n",
    )
    with pytest.raises(RuntimeError, match="uv 0.12.3 is required"):
        module.build_release(tmp_path / "dist", project_root=ROOT)


def test_release_builder_rejects_nonempty_output(tmp_path, monkeypatch):
    module = _load_builder()
    output = tmp_path / "dist"
    output.mkdir()
    (output / "stale.whl").write_bytes(b"stale")
    with pytest.raises(ValueError, match="empty"):
        module.build_release(output, project_root=ROOT)


def test_release_builder_rejects_unexpected_generated_output(tmp_path, monkeypatch):
    module = _load_builder()
    real_uv = module.shutil.which("uv")
    assert real_uv is not None
    monkeypatch.setattr(module.shutil, "which", lambda name: real_uv)
    monkeypatch.setattr(module, "_sha256_file", lambda path: module.REQUIRED_UV_SHA256)

    def fake_check_output(command, **kwargs):
        if "--version" in command:
            return "uv 0.12.3 (test)\n"
        return (ROOT / "requirements-production.txt").read_text()

    monkeypatch.setattr(module.subprocess, "check_output", fake_check_output)
    output = tmp_path / "dist"

    def fake_run(command, *, cwd, env, check):
        output.mkdir(parents=True, exist_ok=True)
        (output / "thinkroom-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (output / "thinkroom-0.1.0.tar.gz").write_bytes(b"sdist")
        (output / "unexpected.txt").write_text("unexpected")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="unexpected release build output"):
        module.build_release(output, project_root=ROOT)


def test_release_builder_subprocess_ignores_uv_project_redirectors(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    receipt = tmp_path / "uv-receipt.json"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    print("uv 0.12.3 (test)")
    raise SystemExit(0)
Path(os.environ["THINKROOM_TEST_RECEIPT"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "uv_working_dir": os.environ.get("UV_WORKING_DIR"),
    "uv_project": os.environ.get("UV_PROJECT"),
    "uv_config_file": os.environ.get("UV_CONFIG_FILE"),
    "uv_no_build_isolation": os.environ.get("UV_NO_BUILD_ISOLATION"),
    "uv_no_config": os.environ.get("UV_NO_CONFIG"),
}))
output = Path(sys.argv[sys.argv.index("--out-dir") + 1])
output.mkdir(parents=True, exist_ok=True)
(output / ".gitignore").write_text("*\\n")
(output / "thinkroom-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
(output / "thinkroom-0.1.0.tar.gz").write_bytes(b"sdist")
"""
    )
    fake_uv.chmod(0o755)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "pyproject.toml").write_text("[project]\nname='foreign-proof'\nversion='9.9.9'\n")
    output = tmp_path / "dist"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "THINKROOM_TEST_RECEIPT": str(receipt),
        "UV_WORKING_DIR": str(foreign),
        "UV_PROJECT": str(foreign),
        "UV_CONFIG_FILE": str(foreign / "uv.toml"),
        "UV_NO_BUILD_ISOLATION": "1",
    }

    result = subprocess.run(
        ["python3", str(SCRIPT), "--out-dir", str(output)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "failed identity verification" in result.stderr
    assert not receipt.exists()
    assert not output.exists()


def test_required_release_smokes_do_not_use_optimization_removable_asserts():
    for relative in (
        "scripts/smoke_package.py",
        "scripts/smoke_process.py",
        "scripts/smoke_prime_e2e.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(), filename=relative)
        assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree)), relative


def test_native_process_is_the_release_authority_and_docker_is_operator_owned():
    specification = (ROOT / "docs" / "specification.md").read_text()
    agents = (ROOT / "AGENTS.md").read_text()
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text()

    assert "Local-process deployment is the v0.1 release authority." in specification
    assert "Docker is an operator-owned reference integration" in specification
    assert "requirements-production.txt" in specification
    assert "requirements-production.txt" in agents
    assert "Docker availability or smoke status does not gate the native release." in agents
    assert "Thinkroom does not certify the operator's Docker bridge" in operations


def test_windows_docker_smoke_has_no_machine_local_context_default():
    script = (ROOT / "scripts" / "smoke_docker.ps1").read_text()
    assert "[Parameter(Mandatory = $true)][string]$Context" in script
    assert "D:\\Warehouse" not in script


def test_public_package_declares_source_available_license_and_repository():
    metadata = (ROOT / "pyproject.toml").read_text()
    readme = (ROOT / "README.md").read_text()
    license_text = (ROOT / "LICENSE").read_text()

    assert (ROOT / "LICENSE").is_file()
    assert 'license = { file = "LICENSE" }' in metadata
    assert 'Repository = "https://github.com/phenomenoner/ai-agent-thinkroom"' in metadata
    assert "source-available, not open source" in readme
    assert "use and run an unmodified copy" in license_text
    assert "including production use" in license_text
    assert "requirements-production.txt" in readme
    assert "--require-hashes" in readme
    assert "--no-deps" in readme
    assert "verify_locked_runtime.py" in readme


def test_release_docs_execute_the_flat_verifier_asset():
    readme = (ROOT / "README.md").read_text()
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
    specification = (ROOT / "docs" / "specification.md").read_text()
    agents = (ROOT / "AGENTS.md").read_text()

    assert ".venv/bin/python verify_locked_runtime.py uv.lock" in readme
    assert "python verify_locked_runtime.py uv.lock" in operations
    for text in (readme, operations, specification, agents):
        assert "`scripts/verify_locked_runtime.py`" not in text


def test_public_ci_uses_pinned_uv_action_without_persisted_checkout_credentials():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "persist-credentials: false" in workflow
    assert "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d" in workflow
    assert "pip install uv" not in workflow


def test_public_docs_do_not_claim_a_pre_tag_signed_release():
    for relative in (
        "README.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "docs/specification.md",
        "docs/adr/0002-native-process-release-authority.md",
    ):
        text = (ROOT / relative).read_text().lower()
        assert "signed deployment" not in text
        assert "signed release" not in text
        assert "signed product" not in text


def test_prime_smoke_failure_diagnostic_excludes_request_and_error_details():
    script = ROOT / "scripts" / "smoke_prime_e2e.py"
    spec = importlib.util.spec_from_file_location("thinkroom_prime_smoke", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    canary = "credential-canary"

    diagnostic = module.failure_diagnostic(
        {
            "state": "failed",
            "request": {"question": canary, "context": canary},
            "terminal_error": {"code": "INTERNAL_ERROR", "details": {"secret": canary}},
            "attempts": [{"outcome": "failed", "private": canary}],
            "transitions": [{"to_state": "failed", "reason": canary}],
        }
    )

    encoded = json.dumps(diagnostic)
    assert canary not in encoded
    assert diagnostic == {
        "state": "failed",
        "terminal_error_code": "INTERNAL_ERROR",
        "attempt_outcomes": ["failed"],
        "transition_states": ["failed"],
    }


def test_container_healthcheck_is_not_optimization_removable():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "assert " not in dockerfile
    assert 'CMD ["python", "scripts/healthcheck.py"]' in dockerfile
    script = ROOT / "scripts" / "healthcheck.py"
    tree = ast.parse(script.read_text(), filename=str(script))
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))

    result = subprocess.run(
        ["python3", "-O", str(script)],
        env={**os.environ, "THINKROOM_HEALTHCHECK_URL": "http://127.0.0.1:9/health/ready"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_healthcheck_cannot_be_redirected_to_an_arbitrary_200_endpoint(monkeypatch):
    script = ROOT / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("thinkroom_healthcheck", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, url, timeout):
            requested.append(url)
            assert timeout == 2
            return Response()

    def fake_build_opener(*handlers):
        proxy = next(handler for handler in handlers if isinstance(handler, module.ProxyHandler))
        assert proxy.proxies == {}
        assert any(isinstance(handler, module._RejectRedirects) for handler in handlers)
        return Opener()

    monkeypatch.setenv("THINKROOM_HEALTHCHECK_URL", "http://127.0.0.1:8787/health/live")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(module, "build_opener", fake_build_opener)
    assert module.main() == 0
    assert requested == [module.DEFAULT_URL]


def test_release_builder_rejects_forged_path_uv(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nprintf 'uv 0.12.3 (forged)\\n'\n")
    fake_uv.chmod(0o755)
    result = subprocess.run(
        ["python3", str(SCRIPT), "--out-dir", str(tmp_path / "dist")],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "dist").exists()


def test_release_builder_rejects_malformed_archives(tmp_path):
    module = _load_builder()
    output = tmp_path / "dist"
    output.mkdir()
    (output / "thinkroom-0.1.0-py3-none-any.whl").write_bytes(b"not a wheel")
    (output / "thinkroom-0.1.0.tar.gz").write_bytes(b"not an sdist")
    with pytest.raises(RuntimeError, match="invalid release artifact"):
        module._close_release_output(output)


def test_release_builder_executes_private_copy_after_selected_path_replacement(
    tmp_path, monkeypatch
):
    module = _load_builder()
    real_uv = Path(module.shutil.which("uv") or "")
    assert real_uv.is_file()
    selected = tmp_path / "selected-uv"
    selected.write_bytes(real_uv.read_bytes())
    selected.chmod(0o755)
    output = tmp_path / "dist"
    monkeypatch.setattr(module.shutil, "which", lambda name: str(selected))
    monkeypatch.setattr(module, "_validate_wheel", lambda path: None)
    monkeypatch.setattr(module, "_validate_sdist", lambda path: None)

    def fake_version(command, **kwargs):
        selected.write_text("#!/bin/sh\nexit 99\n")
        selected.chmod(0o755)
        assert Path(command[0]) != selected
        assert module._sha256_file(Path(command[0])) == module.REQUIRED_UV_SHA256
        if "--version" in command:
            return "uv 0.12.3 (trusted-copy)\n"
        return (ROOT / "requirements-production.txt").read_text()

    def fake_run(command, **kwargs):
        assert Path(command[0]) != selected
        assert module._sha256_file(Path(command[0])) == module.REQUIRED_UV_SHA256
        output.mkdir(exist_ok=True)
        (output / "thinkroom-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (output / "thinkroom-0.1.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "check_output", fake_version)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.build_release(output, project_root=ROOT)
