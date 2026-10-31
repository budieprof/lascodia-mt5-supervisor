"""The supervisor's own watchdog (contract C4 ``runtime.watchdog``).

It keeps an install's terminal running when — and only when — the engine wants it running
with auto-heal on.  ``decide()`` is a pure function of (persisted state, observation, limits)
so every rule is table-tested; :class:`Watchdog` persists the state and runs the actions.

Rules, in order:
  * watchdog disabled in supervisor.yaml → nothing (``disabled``).
  * desired ``Stopped`` → never start.  A running terminal is stopped once, at the moment the
    engine's desired state *changes* to Stopped (so an operator who opens the app by hand
    afterwards is not fought).  Status ``disabled``.
  * auto-heal off → nothing at all (``disabled``).
  * terminal absent → start, with backoff 30 s / 2 m / 5 m / 15 m between failed attempts.
  * terminal alive, but every EA beacon stopped ticking ``wedge_after_sec`` ago → restart; not
    within ``launch_grace_sec`` of a launch, not within ``wake_grace_sec`` of the Mac waking up,
    and never on "no beacons at all" (an EA build without beacons gives no evidence).
  * an ``init_failed`` beacon after ``init_failed_launches`` launches in a row → ``gave_up``: a
    restart does not fix bad inputs.  Cleared by an operator start/restart or a healthy beacon.
  * terminal journal corruption signal → restart, at most once per hour.
  * limits: ≤ max_restarts_per_hour and ≤ max_restarts_per_day automatic starts/restarts, then
    ``gave_up`` with one retry per ``gave_up_retry_sec``.  Operator jobs never count.
Real-money safety: a terminal is only ever restarted when its EA is already not working (absent,
wedged, init_failed, or a corrupt feed); positions keep their broker-side SL/TP meanwhile.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .beacons import BeaconVerdict, iso_utc

logger = logging.getLogger("lascodia.supervisor.watchdog")

START_CONFIRM_SEC = 120  # a launch has this long to produce a terminal before it counts as failed


@dataclass
class Limits:
    enabled: bool = True
    start_backoff_sec: list = field(default_factory=lambda: [30, 120, 300, 900])
    max_per_hour: int = 3
    max_per_day: int = 8
    gave_up_retry_sec: int = 3600
    launch_grace_sec: int = 300
    wake_grace_sec: int = 300
    init_failed_launches: int = 3
    journal_min_interval_sec: int = 3600
    wedge_after_sec: int = 300

    @classmethod
    def from_config(cls, wd, wedge_after_sec: int) -> "Limits":
        return cls(enabled=wd.enabled, start_backoff_sec=list(wd.start_backoff_sec) or [30],
                   max_per_hour=wd.max_restarts_per_hour, max_per_day=wd.max_restarts_per_day,
                   gave_up_retry_sec=wd.gave_up_retry_sec, launch_grace_sec=wd.launch_grace_sec,
                   wake_grace_sec=wd.wake_grace_sec, init_failed_launches=wd.init_failed_launches,
                   journal_min_interval_sec=wd.journal_restart_min_interval_sec, wedge_after_sec=wedge_after_sec)


@dataclass
class WatchState:
    status: str = "ok"                 # ok | restarting | backoff | gave_up | disabled
    auto_actions: list = field(default_factory=list)   # epochs of automatic starts/restarts
    pending_start: bool = False
    last_start_attempt: float = 0.0
    consecutive_start_failures: int = 0
    last_failure_at: float = 0.0       # when the last start attempt was judged failed
    last_launch_at: float = 0.0        # any launch, operator or automatic
    init_failed_launches: int = 0
    init_failed_counted_for: float = 0.0
    gave_up_kind: str = ""             # "" | limits | init_failed
    last_journal_restart: float = 0.0
    last_action_at: float = 0.0
    last_reason: str = ""
    next_attempt_at: float = 0.0
    desired_seen: str = ""

    def to_payload(self) -> dict:
        now = time.time()
        return {
            "state": self.status,
            "restartsLastHour": sum(1 for t in self.auto_actions if now - t < 3600),
            "lastActionUtc": iso_utc(self.last_action_at),
            "lastReason": self.last_reason,
            "nextAttemptUtc": iso_utc(self.next_attempt_at) if self.status in ("backoff", "gave_up") else "",
        }


@dataclass
class Observation:
    now: float
    desired_state: str               # Running | Stopped
    auto_heal: bool
    desired_explicit: bool           # came from the engine (not a local default)
    process_alive: bool
    process_started_at: Optional[float]
    beacons: BeaconVerdict
    woke_recently: bool
    journal_signals: dict
    busy: bool = False               # an action / job is already running for this install


@dataclass
class Decision:
    action: Optional[str]            # None | start | restart | stop
    reason: str = ""


def _record_launch(st: WatchState, now: float) -> None:
    st.pending_start = True
    st.last_start_attempt = now
    st.last_launch_at = now


def decide(st: WatchState, obs: Observation, lim: Limits) -> Decision:
    now = obs.now
    st.auto_actions = [t for t in st.auto_actions if now - t < 86400]
    st.next_attempt_at = 0.0 if st.status not in ("backoff", "gave_up") else st.next_attempt_at

    if not lim.enabled:
        st.status = "disabled"
        return Decision(None, "watchdog disabled in supervisor.yaml")

    if obs.desired_state == "Stopped":
        # Edge-triggered and only from an engine-confirmed Running: a first contact that says
        # Stopped (or a local default) never kills a terminal that is running right now.
        edge = obs.desired_explicit and st.desired_seen == "Running"
        if obs.desired_explicit:
            st.desired_seen = "Stopped"
        st.status, st.pending_start = "disabled", False
        if edge and obs.auto_heal and obs.process_alive and not obs.busy:
            st.last_action_at, st.last_reason = now, "the engine set this terminal to Stopped"
            return Decision("stop", st.last_reason)
        return Decision(None, "desired state is Stopped")
    if obs.desired_explicit:
        st.desired_seen = obs.desired_state

    if not obs.auto_heal:
        st.status = "disabled"
        return Decision(None, "auto-heal is off")
    if obs.busy:
        st.status = "restarting"
        return Decision(None, "an action is already running")

    want: Optional[str] = None
    reason = ""
    if not obs.process_alive:
        if st.pending_start and now - st.last_start_attempt < START_CONFIRM_SEC:
            st.status = "restarting"
            return Decision(None, "waiting for the terminal to appear")
        if st.pending_start:
            st.pending_start = False
            st.consecutive_start_failures += 1
            st.last_failure_at = now
        want, reason = "start", "the terminal is not running"
    else:
        if st.pending_start:
            st.pending_start = False
            st.consecutive_start_failures = 0
        b = obs.beacons
        # init_failed bookkeeping: one count per launch.
        if b.healthy_since_launch:
            st.init_failed_launches = 0
            if st.gave_up_kind == "init_failed":
                st.gave_up_kind = ""
        elif b.init_failed_since_launch and st.init_failed_counted_for != st.last_launch_at:
            st.init_failed_launches += 1
            st.init_failed_counted_for = st.last_launch_at
        if st.init_failed_launches >= lim.init_failed_launches:
            st.gave_up_kind = "init_failed"
            st.status = "gave_up"
            st.last_reason = (f"the EA failed to start {st.init_failed_launches} launches in a row"
                              f"{': ' + b.init_fail_reason if b.init_fail_reason else ''} — a restart will not "
                              "fix this; check the EA inputs")
            return Decision(None, st.last_reason)

        started = max(st.last_launch_at, obs.process_started_at or 0.0)
        in_grace = now - started < lim.launch_grace_sec
        if in_grace or obs.woke_recently:
            st.status = "ok" if st.gave_up_kind == "" else st.status
            return Decision(None, "just launched" if in_grace else "the Mac just woke up")

        signals = [k for k, v in (obs.journal_signals or {}).items() if v]
        if b.count > 0 and not b.fresh:
            age = int((now - b.newest_tick) / 60) if b.newest_tick else None
            want = "restart"
            reason = (f"the EA stopped ticking ({'no tick yet' if age is None else f'last tick {age} min ago'}) "
                      "while the terminal is running")
        elif signals and now - st.last_journal_restart >= lim.journal_min_interval_sec:
            want, reason = "restart", "the terminal journal shows a corrupted data feed (" + ", ".join(signals) + ")"
        else:
            if st.gave_up_kind == "limits" and (not st.auto_actions or now - st.auto_actions[-1] > lim.gave_up_retry_sec):
                st.gave_up_kind = ""
            st.status = "ok" if not st.gave_up_kind else "gave_up"
            st.consecutive_start_failures = 0
            return Decision(None, "healthy")

    if st.gave_up_kind == "init_failed":
        st.status = "gave_up"
        return Decision(None, st.last_reason)

    # Backoff between failed start attempts, counted from the moment the attempt was judged
    # failed (START_CONFIRM_SEC after it was made).
    if want == "start" and st.consecutive_start_failures > 0:
        idx = min(st.consecutive_start_failures - 1, len(lim.start_backoff_sec) - 1)
        wait = lim.start_backoff_sec[idx]
        since = st.last_failure_at or st.last_start_attempt
        if now - since < wait:
            st.status = "backoff"
            st.next_attempt_at = since + wait
            st.last_reason = f"{reason}; start attempt {st.consecutive_start_failures} failed, waiting"
            return Decision(None, st.last_reason)

    hour = sum(1 for t in st.auto_actions if now - t < 3600)
    day = len(st.auto_actions)
    if hour >= lim.max_per_hour or day >= lim.max_per_day:
        st.gave_up_kind = "limits"
        last = st.auto_actions[-1] if st.auto_actions else 0.0
        if now - last < lim.gave_up_retry_sec:
            st.status = "gave_up"
            st.next_attempt_at = last + lim.gave_up_retry_sec
            st.last_reason = (f"{reason}; gave up after {hour} restarts in the last hour / {day} today — "
                              "retrying hourly")
            return Decision(None, st.last_reason)
        # hourly retry while gave_up

    st.auto_actions.append(now)
    st.last_action_at, st.last_reason = now, reason
    st.status = "restarting"
    if want == "restart" and "journal" in reason:
        st.last_journal_restart = now
    _record_launch(st, now)
    return Decision(want, reason)


class Watchdog:
    """Holds one WatchState per install, persisted in ``<state_dir>/watchdog.json``."""

    def __init__(self, state_dir: Optional[str]) -> None:
        self._path = Path(state_dir) / "watchdog.json" if state_dir else None
        self._lock = threading.Lock()
        self.states: dict[str, WatchState] = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            known = set(WatchState.__dataclass_fields__)
            for iid, d in (raw or {}).items():
                self.states[iid] = WatchState(**{k: v for k, v in d.items() if k in known})
        except (OSError, ValueError, TypeError) as ex:
            logger.warning("watchdog state %s unreadable (%s) — starting fresh", self._path, type(ex).__name__)

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps({k: asdict(v) for k, v in self.states.items()}, indent=1), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError as ex:
                logger.warning("could not persist watchdog state: %s", ex)

    def state(self, install_id: str) -> WatchState:
        with self._lock:
            return self.states.setdefault(install_id, WatchState())

    def evaluate(self, install_id: str, obs: Observation, lim: Limits) -> Decision:
        st = self.state(install_id)
        with self._lock:
            d = decide(st, obs, lim)
        if d.action:
            logger.warning("watchdog %s: %s — %s", install_id, d.action, d.reason)
        return d

    def note_operator_launch(self, install_id: str, now: Optional[float] = None) -> None:
        """An operator job started / restarted the terminal: give it the launch grace, forget an
        init_failed give-up, and do not count it against the automatic limits."""
        st = self.state(install_id)
        with self._lock:
            now = now or time.time()
            _record_launch(st, now)
            st.consecutive_start_failures = 0
            st.init_failed_launches = 0
            if st.gave_up_kind == "init_failed":
                st.gave_up_kind = ""
            st.status = "restarting"
        self.save()

    def note_action_result(self, install_id: str, ok: bool, message: str) -> None:
        st = self.state(install_id)
        with self._lock:
            if not ok:
                st.last_reason = f"{st.last_reason} — failed: {message}"[:500]
        self.save()
