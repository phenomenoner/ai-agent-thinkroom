#!/usr/bin/env python3
"""Build release artifacts under a closed reproducibility contract."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

REQUIRED_UV_VERSION = "0.12.3"
REQUIRED_UV_SHA256 = "729d27dbea534ee540a2d3ef43a62fa1a10af7fcbb6d57a70d5859509f624578"
RELEASE_SOURCE_DATE_EPOCH = "0"


def _outside_project(output_dir: Path, project_root: Path) -> None:
    try:
        output_dir.relative_to(project_root)
    except ValueError:
        return
    raise ValueError("release output directory must be outside the project tree")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_trusted_uv(source: Path, directory: Path) -> Path:
    if not getattr(os, "O_NOFOLLOW", 0):
        raise RuntimeError("uv build frontend identity cannot be secured")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    destination = directory / "uv"
    digest = hashlib.sha256()
    try:
        opened = os.fstat(fd)
        named = source.lstat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RuntimeError("uv build frontend failed identity verification")
        with os.fdopen(os.dup(fd), "rb") as reader, destination.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(fd)
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("uv build frontend failed identity verification")
    finally:
        os.close(fd)
    if digest.hexdigest() != REQUIRED_UV_SHA256:
        destination.unlink(missing_ok=True)
        raise RuntimeError("uv build frontend failed identity verification")
    destination.chmod(0o500)
    if _sha256_file(destination) != REQUIRED_UV_SHA256:
        raise RuntimeError("uv build frontend failed identity verification")
    return destination


def _validate_wheel(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if any(
                name.startswith("/") or ".." in Path(name).parts or name.endswith("/")
                for name in names
            ):
                raise ValueError
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel = [name for name in names if name.endswith(".dist-info/WHEEL")]
            records = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(metadata) != 1 or len(wheel) != 1 or len(records) != 1:
                raise ValueError
            metadata_text = archive.read(metadata[0]).decode("utf-8")
            if "Name: thinkroom\n" not in metadata_text or "Version: 0.1.0\n" not in metadata_text:
                raise ValueError
            rows = list(csv.reader(io.StringIO(archive.read(records[0]).decode("utf-8"))))
            record = {row[0]: row[1:] for row in rows if len(row) == 3}
            if set(record) != set(names):
                raise ValueError
            for name in names:
                encoded_hash, encoded_size = record[name]
                if name == records[0]:
                    if encoded_hash or encoded_size:
                        raise ValueError
                    continue
                data = archive.read(name)
                expected = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                )
                if encoded_hash != f"sha256={expected}" or encoded_size != str(len(data)):
                    raise ValueError
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeError("invalid release artifact") from exc


def _validate_sdist(path: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise ValueError
            names = [member.name for member in members]
            roots = {Path(name).parts[0] for name in names if Path(name).parts}
            if roots != {"thinkroom-0.1.0"}:
                raise ValueError
            if any(
                name.startswith("/")
                or ".." in Path(name).parts
                or not (member.isdir() or member.isfile())
                for name, member in zip(names, members, strict=True)
            ):
                raise ValueError
            required = {
                "thinkroom-0.1.0/pyproject.toml",
                "thinkroom-0.1.0/PKG-INFO",
            }
            if not required.issubset(names):
                raise ValueError
            pkg = archive.extractfile("thinkroom-0.1.0/PKG-INFO")
            if pkg is None:
                raise ValueError
            metadata = pkg.read().decode("utf-8")
            if "Name: thinkroom\n" not in metadata or "Version: 0.1.0\n" not in metadata:
                raise ValueError
    except (OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        raise RuntimeError("invalid release artifact") from exc


def _close_release_output(output: Path) -> None:
    tool_marker = output / ".gitignore"
    if tool_marker.is_file():
        tool_marker.unlink()
    entries = sorted(path for path in output.iterdir())
    wheels = [path for path in entries if path.is_file() and path.suffix == ".whl"]
    sdists = [path for path in entries if path.is_file() and path.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("unexpected release build output")
    _validate_wheel(wheels[0])
    _validate_sdist(sdists[0])


def _verify_production_requirements(uv: Path, root: Path, environment: dict[str, str]) -> None:
    generated = subprocess.check_output(
        [
            str(uv),
            "--no-config",
            "export",
            "--project",
            str(root),
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--no-header",
            "--no-annotate",
        ],
        cwd=root,
        env=environment,
        text=True,
    )
    tracked = (root / "requirements-production.txt").read_text()
    if tracked != generated:
        raise RuntimeError("requirements-production.txt is stale")


def build_release(output_dir: Path, *, project_root: Path | None = None) -> None:
    """Build one wheel and sdist with pinned frontend, backend, and epoch inputs."""
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    output = output_dir.resolve()
    _outside_project(output, root)
    if output.exists() and any(output.iterdir()):
        raise ValueError("release output directory must be empty")

    environment = os.environ.copy()
    cache_dir = environment.get("UV_CACHE_DIR")
    for name in tuple(environment):
        if name.startswith("UV_"):
            environment.pop(name)
    if cache_dir:
        environment["UV_CACHE_DIR"] = cache_dir
    environment["UV_NO_CONFIG"] = "1"
    environment["SOURCE_DATE_EPOCH"] = RELEASE_SOURCE_DATE_EPOCH
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError(f"uv {REQUIRED_UV_VERSION} is required")
    uv_path = Path(uv).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="thinkroom-trusted-uv-") as trusted_directory:
        trusted_uv = _materialize_trusted_uv(uv_path, Path(trusted_directory))
        version_line = subprocess.check_output(
            [str(trusted_uv), "--version"], env=environment, text=True
        ).strip()
        actual_version = version_line.split()[1] if len(version_line.split()) >= 2 else "unknown"
        if actual_version != REQUIRED_UV_VERSION:
            raise RuntimeError(f"uv {REQUIRED_UV_VERSION} is required; found {actual_version}")
        _verify_production_requirements(trusted_uv, root, environment)

        output.mkdir(parents=True, exist_ok=True)
        constraints = (root / "build-constraints.txt").resolve(strict=True)
        subprocess.run(
            [
                str(trusted_uv),
                "--no-config",
                "build",
                str(root),
                "--build-constraints",
                str(constraints),
                "--require-hashes",
                "--out-dir",
                str(output),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
    _close_release_output(output)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="empty output directory outside the project tree",
    )
    args = parser.parse_args(argv)
    build_release(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
