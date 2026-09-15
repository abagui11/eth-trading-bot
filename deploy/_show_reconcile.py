"""Read-only: print the last reconcile snapshot and the freeze flag."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pool  # noqa: E402

print(json.dumps(pool.last_reconcile(), indent=2))
print("frozen:", pool.intents_frozen())
