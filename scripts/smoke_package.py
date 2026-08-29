"""Smoke-test an installed Thinkroom wheel (run after pip/uv install)."""

import json

from thinkroom.verification import verify_package

print(json.dumps(verify_package(), sort_keys=True))
