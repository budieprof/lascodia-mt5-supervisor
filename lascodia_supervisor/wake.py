"""Did this Mac just wake from sleep?

After a sleep every EA beacon looks minutes or hours stale although nothing is wrong — the
terminal simply was not running.  Restarting it then would be pointless churn on a real-money
terminal, so wedge decisions wait out a grace period after a wake.

Two independent signals:
  * wall clock vs. monotonic clock: on macOS ``time.monotonic()`` (mach_absolute_time) stops
    while the machine sleeps and the wall clock does not — a gap between two samples is sleep;
  * ``sysctl kern.waketime`` — the kernel's last wake (covers a supervisor started after a wake).
"""
from __future__ import annotations

import platform
import re
import subprocess
import time
from typing import Callable, Optional

_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")


def kern_waketime() -> Optional[float]:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.waketime"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _SEC_RE.search(out or "")
    return float(m.group(1)) if m else None


class WakeDetector:
    def __init__(self, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 waketime: Callable[[], Optional[float]] = kern_waketime, gap_sec: float = 30.0) -> None:
        self._wall, self._mono, self._waketime = wall, mono, waketime
        self._gap = gap_sec
        self._last: Optional[tuple[float, float]] = None
        self.last_wake: Optional[float] = None

    def tick(self) -> None:
        wall, mono = self._wall(), self._mono()
        if self._last is not None:
            jump = (wall - self._last[0]) - (mono - self._last[1])
            if jump > self._gap:
                self.last_wake = wall
        self._last = (wall, mono)
        kt = self._waketime()
        if kt and (self.last_wake is None or kt > self.last_wake):
            self.last_wake = kt

    def woke_within(self, sec: float, now: Optional[float] = None) -> bool:
        now = now if now is not None else self._wall()
        return self.last_wake is not None and 0 <= now - self.last_wake < sec
