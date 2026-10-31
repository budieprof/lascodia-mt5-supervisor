"""EA liveness beacons (contract C2).

Every EA writes ``MQL5/Files/Lascodia/beacon/<instanceId>.json`` at the end of OnInit (success
or failure), at most every 15 s from OnTimer, and on OnDeinit (``phase:"deinit"``).  It works
before the EA has signed in to the engine, so it is how the supervisor tells "EA alive" from
"terminal alive but the EA never got going" (the wedge).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lascodia.supervisor.beacons")

MAX_BEACONS = 64
MAX_BEACON_BYTES = 64 * 1024


def parse_utc(value) -> Optional[float]:
    """ISO-8601 (``...Z``) → epoch seconds; None when missing or unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:          # the EA always writes UTC; a bare stamp is UTC too
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def iso_utc(epoch: Optional[float]) -> str:
    if not epoch:
        return ""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    if raw and raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_beacons(mql5: Path) -> tuple[list[dict], str]:
    """All beacon files of one terminal, newest first, plus a read-error summary ("" = fine)."""
    folder = Path(mql5) / "Files" / "Lascodia" / "beacon"
    if not folder.is_dir():
        return [], ""
    beacons: list[dict] = []
    errors: list[str] = []
    try:
        files = sorted((p for p in folder.glob("*.json") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as ex:
        return [], f"cannot list {folder}: {type(ex).__name__}"
    for path in files[:MAX_BEACONS]:
        try:
            raw = path.read_bytes()[:MAX_BEACON_BYTES]
            data = json.loads(_decode(raw).strip().rstrip("\x00"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            beacons.append(data)
        except (OSError, ValueError) as ex:
            errors.append(f"{path.name}: {type(ex).__name__}")
    return beacons, "; ".join(errors)[:500]


@dataclass
class BeaconVerdict:
    count: int                  # beacons that describe a loaded EA (deinit ones excluded)
    fresh: bool                 # at least one of them ticked within the wedge window
    newest_tick: Optional[float]
    init_failed_since_launch: bool   # an init_failed beacon written after the last launch
    healthy_since_launch: bool       # an init/running beacon written after the last launch
    init_fail_reason: str = ""


def assess(beacons: list[dict], now: float, wedge_after_sec: int, launched_at: Optional[float]) -> BeaconVerdict:
    live = [b for b in beacons if str(b.get("phase", "")).lower() != "deinit"]
    newest: Optional[float] = None
    init_failed = healthy = False
    reason = ""
    for b in live:
        phase = str(b.get("phase", "")).lower()
        tick = parse_utc(b.get("lastTimerUtc")) or None
        written = parse_utc(b.get("writtenAtUtc"))
        # An EA still inside OnInit (C5 degraded mode retries from OnTimer, so lastTimerUtc moves)
        # or one that only got as far as writing its first beacon: writtenAt is the liveness sign.
        mark = max(t for t in (tick, written if phase in ("init",) else None, 0.0) if t is not None)
        if mark and (newest is None or mark > newest):
            newest = mark
        if launched_at and written and written >= launched_at - 1:
            if phase == "init_failed":
                init_failed = True
                reason = reason or str(b.get("initFailReason", ""))
            elif phase in ("init", "running"):
                healthy = True
    fresh = newest is not None and (now - newest) <= wedge_after_sec
    return BeaconVerdict(count=len(live), fresh=fresh, newest_tick=newest,
                         init_failed_since_launch=init_failed and not healthy,
                         healthy_since_launch=healthy, init_fail_reason=reason[:300])
