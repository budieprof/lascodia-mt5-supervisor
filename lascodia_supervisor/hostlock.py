"""The engine host's maintenance lock (engine repo ``deploy/prod-host/host-lock.sh``), read-only.

2026-11-08: a session rebooted mac2 for a macOS update two seconds after another started a blue/green
swap.  Engine deploys, swaps, EA rollouts, reboots and OS updates now take ONE lock directory on the host.
The supervisor never takes it; it only refuses to STOP or RESTART a terminal while someone else holds it,
so auto-heal (engine EA:AutoHeal → terminal restart jobs) and the local watchdog never bounce terminals in
the middle of somebody's maintenance.  STARTING a terminal stays allowed: bringing terminals back after a
maintenance reboot is exactly what the supervisor is for.

The lock is a directory with ``holder``, ``action``, ``since`` and ``expires`` (unix seconds) files; a lock
past its expiry is treated as free (its holder died or the host rebooted).  Hosts without the engine
checkout simply have no lock directory.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_LOCK_DIR = os.path.join(os.path.expanduser("~"), "lascodia-prod", "state", "host-maintenance.lock")


@dataclass(frozen=True)
class HostLockState:
    held: bool
    holder: str = ""
    action: str = ""
    since: str = ""
    expires: int = 0

    def describe(self) -> str:
        if not self.held:
            return "free"
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires)) if self.expires else "?"
        return f"held by {self.holder or '?'} for \"{self.action or '?'}\" since {self.since or '?'} until {until}"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def read(lock_dir: Optional[str] = None, now: Optional[float] = None) -> HostLockState:
    d = Path(os.path.expanduser(lock_dir or os.environ.get("HOST_LOCK_DIR") or DEFAULT_LOCK_DIR))
    if not d.is_dir():
        return HostLockState(False)
    try:
        expires = int(_read(d / "expires") or "0")
    except ValueError:
        expires = 0
    if expires <= (now if now is not None else time.time()):
        return HostLockState(False)          # expired (or half-written with no expiry): treated as free
    return HostLockState(True, _read(d / "holder"), _read(d / "action"), _read(d / "since"), expires)
