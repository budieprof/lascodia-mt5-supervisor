"""``<state_dir>/status.json`` — what the installer reads to say "connected" or exactly what failed.

The daemon rewrites it on start, after each register / heartbeat attempt.  ``lastError`` is a plain
sentence an operator can act on.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

FILE = "status.json"


def write_status(state_dir: Optional[str], **fields) -> None:
    if not state_dir:
        return
    path = Path(state_dir) / FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = read_status(state_dir) or {}
        cur.update(fields)
        cur["updatedAt"] = time.time()
        tmp = path.with_name(FILE + ".new")
        tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_status(state_dir) -> Optional[dict]:
    try:
        return json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
