"""Terminal journal watcher — spots the corruption signatures that mean "this terminal's data
feed is garbage and only a restart fixes it".

The journal is ``<terminal dir>/logs/YYYYMMDD.log``, UTF-16LE.  Only the bytes added since the
last scan are read, so a 300 MB journal costs nothing per cycle.

Signatures (seen live 2026-10-27/28, see memory "Corrupt feed → TimeCurrent garbage"):
  * ``old tick ... (tick: 1971.07.14 ...`` — tick stamps before 2000 (ordinary "old tick" lines
    with a current year are routine and ignored);
  * ``invalid book item`` / ``invalid book transaction`` in bulk (a stray line happens; thousands
    came with the corrupt feed);
  * ``debugger has been found`` — build >= 6290 under an old Wine wrapper.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_OLD_TICK_RE = re.compile(r"old tick .*\(tick: (\d{4})\.")
_INVALID_BOOK_RE = re.compile(r"invalid book (item|transaction)", re.IGNORECASE)
_DEBUGGER_RE = re.compile(r"debugger (has been|was) found", re.IGNORECASE)
_DATE_LOG_RE = re.compile(r"^\d{8}\.log$")

MAX_SCAN_BYTES = 8 * 1024 * 1024


@dataclass
class JournalThresholds:
    old_tick_lines: int = 20
    invalid_book_lines: int = 200
    debugger_lines: int = 1
    window_sec: int = 900  # a signal stays raised this long after the last burst


@dataclass
class _Track:
    path: Optional[Path] = None
    offset: int = 0
    raised_at: dict = field(default_factory=dict)  # signal -> epoch it was last raised


def newest_journal(logs_dir: Path) -> Optional[Path]:
    try:
        files = [p for p in logs_dir.iterdir() if _DATE_LOG_RE.match(p.name)]
    except OSError:
        return None
    return max(files, key=lambda p: p.name) if files else None


def count_signatures(text: str) -> dict:
    old = sum(1 for m in _OLD_TICK_RE.finditer(text) if int(m.group(1)) < 2000)
    return {"oldTick": old,
            "invalidBook": len(_INVALID_BOOK_RE.findall(text)),
            "debuggerFound": len(_DEBUGGER_RE.findall(text))}


class JournalWatcher:
    """One per install.  ``scan()`` returns the C4 ``journalSignals`` object."""

    def __init__(self, logs_dir: Path, thresholds: Optional[JournalThresholds] = None) -> None:
        self.logs_dir = Path(logs_dir)
        self.t = thresholds or JournalThresholds()
        self._track = _Track()
        self.last_counts: dict = {}

    def _read_new(self) -> str:
        path = newest_journal(self.logs_dir)
        if path is None:
            return ""
        tr = self._track
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if tr.path != path:
            # First look after a supervisor start: begin at the end.  Lines already there may
            # describe a corruption that a restart has since fixed, and must not trigger another.
            # A new day's file (the previous one was being followed) is read from the start.
            start = 0 if tr.path is not None else size
            tr.path, tr.offset = path, start
        if size < tr.offset:          # truncated / replaced
            tr.offset = 0
        if size - tr.offset > MAX_SCAN_BYTES:
            tr.offset = size - MAX_SCAN_BYTES
        if size == tr.offset:
            return ""
        start = tr.offset - (tr.offset % 2)   # UTF-16 code units are 2 bytes
        try:
            with path.open("rb") as f:
                f.seek(start)
                raw = f.read(size - start)
        except OSError:
            return ""
        tr.offset = start + len(raw) - (len(raw) % 2)
        raw = raw[: len(raw) - (len(raw) % 2)]
        if raw[:2] == b"\xff\xfe":
            raw = raw[2:]
        return raw.decode("utf-16-le", errors="replace")

    def scan(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        counts = count_signatures(self._read_new())
        self.last_counts = counts
        limits = {"oldTick": self.t.old_tick_lines, "invalidBook": self.t.invalid_book_lines,
                  "debuggerFound": self.t.debugger_lines}
        for key, n in counts.items():
            if n >= limits[key]:
                self._track.raised_at[key] = now
        return {key: (now - self._track.raised_at.get(key, -1e18)) <= self.t.window_sec for key in limits}
