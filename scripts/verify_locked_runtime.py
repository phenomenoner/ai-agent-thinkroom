from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DistributionKey = tuple[str, str]


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def installed_counter(
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
) -> Counter[DistributionKey]:
    result: Counter[DistributionKey] = Counter()
    for distribution in distributions or importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            result[(canonical_name(name), distribution.version)] += 1
    return result


def _marker_enabled(dependency: dict[str, Any]) -> bool:
    marker = dependency.get("marker")
    if marker is None:
        return True
    if not isinstance(marker, str):
        raise RuntimeError("uv.lock dependency marker is invalid")
    environment = {
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }
    clauses = marker.split(" and ")
    for clause in clauses:
        match = re.fullmatch(
            r"(implementation_name|platform_python_implementation|python_full_version|python_version|sys_platform)\s*(==|!=|<=|>=|<|>)\s*(['\"])([^'\"]+)\3",
            clause.strip(),
        )
        if match is None:
            raise RuntimeError("uv.lock dependency marker is unsupported")
        key, operation, _, expected = match.groups()
        actual = environment[key]
        if key in {"python_full_version", "python_version"}:
            try:
                left: object = tuple(int(part) for part in actual.split("."))
                right: object = tuple(int(part) for part in expected.split("."))
            except ValueError as exc:
                raise RuntimeError("uv.lock Python-version marker is invalid") from exc
        else:
            left, right = actual, expected
        comparisons = {
            "==": left == right,
            "!=": left != right,
            "<": left < right,  # type: ignore[operator]
            "<=": left <= right,  # type: ignore[operator]
            ">": left > right,  # type: ignore[operator]
            ">=": left >= right,  # type: ignore[operator]
        }
        if not comparisons[operation]:
            return False
    return True


def lock_contract(lock_path: Path) -> tuple[dict[str, set[str]], set[str]]:
    lock: dict[str, Any] = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock package table is missing")

    locked: dict[str, set[str]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            canonical = canonical_name(name)
            locked.setdefault(canonical, set()).add(version)
            by_name.setdefault(canonical, []).append(package)
    if "thinkroom" not in by_name:
        raise RuntimeError("uv.lock does not contain the thinkroom project")

    required: set[str] = set()
    active_extras: dict[str, set[str]] = {}
    processed: dict[str, frozenset[str]] = {}
    pending: list[tuple[str, set[str]]] = [("thinkroom", set())]
    while pending:
        name, extras = pending.pop()
        active = active_extras.setdefault(name, set())
        active.update(extras)
        frozen = frozenset(active)
        if processed.get(name) == frozen:
            continue
        candidates = by_name.get(name, [])
        if len(candidates) != 1:
            raise RuntimeError(f"uv.lock selected dependency is ambiguous: {name}")
        processed[name] = frozen
        required.add(name)
        package = candidates[0]
        dependencies = list(package.get("dependencies") or [])
        optional = package.get("optional-dependencies") or {}
        if not isinstance(optional, dict):
            raise RuntimeError("uv.lock optional dependency table is invalid")
        for extra in frozen:
            extra_dependencies = optional.get(extra)
            if not isinstance(extra_dependencies, list):
                raise RuntimeError(f"uv.lock selected extra is missing: {name}[{extra}]")
            dependencies.extend(extra_dependencies)
        for dependency in dependencies:
            if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
                raise RuntimeError("uv.lock dependency entry is invalid")
            if not _marker_enabled(dependency):
                continue
            dep_extras = dependency.get("extra") or []
            if not isinstance(dep_extras, list) or not all(
                isinstance(extra, str) for extra in dep_extras
            ):
                raise RuntimeError("uv.lock dependency extras are invalid")
            pending.append((canonical_name(dependency["name"]), set(dep_extras)))
    return locked, required


def verify_lock_membership(
    installed: Counter[DistributionKey], locked: dict[str, set[str]], required: set[str]
) -> None:
    allowed_bootstrap = {"pip", "setuptools", "wheel"}
    unlocked = sorted(
        f"{name}=={version}"
        for (name, version), count in installed.items()
        if count and name not in allowed_bootstrap and version not in locked.get(name, set())
    )
    names = {name for name, _ in installed}
    missing_required = sorted(required - names)
    unexpected_selected = sorted(name for name in names - required if name not in allowed_bootstrap)
    duplicates = sorted(
        f"{name}=={version} x{count}" for (name, version), count in installed.items() if count != 1
    )
    installed_versions: dict[str, set[str]] = {}
    for (name, version), count in installed.items():
        if count:
            installed_versions.setdefault(name, set()).add(version)
    ambiguous = sorted(
        f"{name}: {', '.join(sorted(versions))}"
        for name, versions in installed_versions.items()
        if len(versions) != 1
    )
    if unlocked or missing_required or unexpected_selected or duplicates or ambiguous:
        raise RuntimeError(
            json.dumps(
                {
                    "ambiguous_distributions": ambiguous,
                    "duplicate_distributions": duplicates,
                    "missing_required": missing_required,
                    "unexpected_selected": unexpected_selected,
                    "unlocked_or_mismatched": unlocked,
                },
                sort_keys=True,
            )
        )


def write_manifest(path: Path, installed: Counter[DistributionKey]) -> None:
    payload = {
        "schema_version": 1,
        "distributions": [
            {"name": name, "version": version, "count": count}
            for (name, version), count in sorted(installed.items())
        ],
    }
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def read_manifest(path: Path) -> Counter[DistributionKey]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("runtime lock manifest schema is invalid")
    rows = payload.get("distributions")
    if not isinstance(rows, list):
        raise RuntimeError("runtime lock manifest distributions are invalid")
    result: Counter[DistributionKey] = Counter()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("version"), str)
            or not isinstance(row.get("count"), int)
            or row["count"] < 1
        ):
            raise RuntimeError("runtime lock manifest entry is invalid")
        key = (canonical_name(row["name"]), row["version"])
        if key in result:
            raise RuntimeError("runtime lock manifest contains duplicate entries")
        result[key] = row["count"]
    return result


def verify_manifest(
    installed: Counter[DistributionKey], expected: Counter[DistributionKey]
) -> None:
    missing = sorted(
        f"{name}=={version} x{count}" for (name, version), count in (expected - installed).items()
    )
    unexpected = sorted(
        f"{name}=={version} x{count}" for (name, version), count in (installed - expected).items()
    )
    if missing or unexpected:
        raise RuntimeError(
            json.dumps({"missing_distributions": missing, "unexpected_distributions": unexpected})
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", nargs="?", default="/app/uv.lock")
    parser.add_argument("--write-manifest")
    parser.add_argument("--manifest")
    args = parser.parse_args()

    installed = installed_counter()
    locked, required = lock_contract(Path(args.lock))
    verify_lock_membership(installed, locked, required)
    if args.write_manifest:
        write_manifest(Path(args.write_manifest), installed)
    if args.manifest:
        verify_manifest(installed, read_manifest(Path(args.manifest)))
    print(
        json.dumps(
            {
                "locked_distributions": sum(installed.values()),
                "manifest_verified": bool(args.manifest),
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
