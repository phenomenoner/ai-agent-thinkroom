"""Smoke-test an installed Thinkroom wheel (run after pip/uv install)."""

from pathlib import Path

import thinkroom

bundle = Path(thinkroom.__file__).parent / "bundled_skills"
if not (bundle / "manifest.json").is_file():
    raise RuntimeError("package smoke: bundled Skills manifest is missing")
if len(list(bundle.glob("*/SKILL.md"))) != 3:
    raise RuntimeError("package smoke: bundled Skills count mismatch")
print("package smoke: ok")
