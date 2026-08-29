"""Production-process smoke: completion, restart persistence, and exclusive lock."""

import json

from thinkroom.verification import verify_process

print(json.dumps(verify_process(), sort_keys=True))
