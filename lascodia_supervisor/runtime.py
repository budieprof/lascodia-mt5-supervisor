"""The self-healing loop: discover → read beacons + journal → watchdog → report (contract C4).

``Supervisor.tick()`` runs every heartbeat interval.  It never blocks on a terminal action:
starts / restarts / stops run on a small thread pool, under the install's lock (shared with
engine jobs), and the next tick sees their effect.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from . import beacons as beacon_mod
from . import host
from .desired import DesiredStateStore, extract_desired
from .jobs import EA_BINARY_NAME, FAILED, JobRunner, Sha256Cache
from .journal import JournalWatcher
from .process_manager import ProcessManager
from .terminal_control import TerminalController
from .wake import WakeDetector
from .watchdog import Limits, Observation, Watchdog

logger = logging.getLogger("lascodia.supervisor.runtime")


class Supervisor:
    def __init__(self, get_cfg: Callable, ops: Optional[host.HostOps] = None, engine=None,
                 wake: Optional[WakeDetector] = None, apps_dir: str = host.APPLICATIONS_DIR,
                 before_launch: Optional[Callable] = None, supervisor_log: Callable[[], Optional[Path]] = lambda: None,
                 synchronous_actions: bool = False) -> None:
        self._get_cfg = get_cfg
        cfg = get_cfg()
        self.ops = ops or host.HostOps()
        self.engine = engine
        self.controller = TerminalController(
            self.ops, lambda: list(self._get_cfg().installs), stop_grace_sec=cfg.watchdog.stop_grace_sec,
            apps_dir=apps_dir, before_launch=before_launch)
        self.pm = ProcessManager(cfg.state_dir, self.controller, lambda: list(self._get_cfg().installs))
        self.watchdog = Watchdog(cfg.state_dir)
        self.desired = DesiredStateStore(cfg.state_dir)
        self.wake = wake or WakeDetector()
        self.sha_cache = Sha256Cache()
        self.jobs = JobRunner(lambda: list(self._get_cfg().installs), self.controller, self.pm, self.watchdog,
                              engine, supervisor_log)
        self._journals: dict[str, JournalWatcher] = {}
        self._runtime: dict[str, dict] = {}
        self._inflight: dict[str, int] = {}
        self._inflight_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lascodia-action")
        self._sync = synchronous_actions

    # ── helpers ─────────────────────────────────────────────────────────
    def _journal(self, install) -> JournalWatcher:
        w = self._journals.get(install.install_id)
        logs = host.journal_dir(install)
        if w is None or w.logs_dir != logs:
            w = self._journals[install.install_id] = JournalWatcher(logs)
        return w

    def busy(self, install_id: str) -> bool:
        with self._inflight_lock:
            if self._inflight.get(install_id, 0) > 0:
                return True
        return self.controller.busy(install_id)

    def any_busy(self) -> bool:
        """A job or terminal action is in flight on any install (self-update waits for this)."""
        with self._inflight_lock:
            if any(n > 0 for n in self._inflight.values()):
                return True
        return any(self.controller.busy(i.install_id) for i in self._get_cfg().installs)

    def _run_action(self, install, action: str, reason: str) -> None:
        iid = install.install_id

        def work():
            try:
                if action == "start":
                    res = self.controller.start(install, reason=f"watchdog: {reason}", wait=True)
                elif action == "restart":
                    self.pm.note_stopping(iid)
                    res = self.controller.restart(install, reason=f"watchdog: {reason}")
                else:
                    self.pm.note_stopping(iid)
                    res = self.controller.stop(install, reason=f"watchdog: {reason}")
                logger.warning("watchdog %s %s → %s: %s", iid, action, "ok" if res.ok else "FAILED", res.message)
                self.watchdog.note_action_result(iid, res.ok, res.message)
                self.pm.refresh()
            except Exception:
                logger.exception("watchdog action %s on %s crashed", action, iid)
            finally:
                self._leave(iid)

        self._enter(iid)
        if self._sync:
            work()
        else:
            self._pool.submit(work)

    # ── the loop body ──────────────────────────────────────────────────
    def tick(self, now: Optional[float] = None) -> None:
        cfg = self._get_cfg()
        self.wake.tick()
        now = now or time.time()
        discovered = self.pm.refresh()
        for install in cfg.installs:
            iid = install.install_id
            d = discovered.get(iid) or host.Discovered(iid)
            mql5 = host.mql5_dir(install)
            beacons, beacon_err = beacon_mod.read_beacons(mql5)
            st = self.watchdog.state(iid)
            launched = max(st.last_launch_at, d.terminal_started_at or 0.0) or None
            verdict = beacon_mod.assess(beacons, now, install.wedge_after_sec, launched)
            signals = self._journal(install).scan(now) if d.running else \
                {"oldTick": False, "invalidBook": False, "debuggerFound": False}
            desired, auto_heal, explicit = self.desired.get(install)
            lim = Limits.from_config(cfg.watchdog, install.wedge_after_sec)
            obs = Observation(now=now, desired_state=desired, auto_heal=auto_heal, desired_explicit=explicit,
                              process_alive=d.running, process_started_at=d.terminal_started_at,
                              beacons=verdict, woke_recently=self.wake.woke_within(lim.wake_grace_sec, now),
                              journal_signals=signals, busy=self.busy(iid))
            decision = self.watchdog.evaluate(iid, obs, lim)
            if decision.action:
                self._run_action(install, decision.action, decision.reason)
            self._runtime[iid] = self._build_runtime(install, d, beacons, beacon_err, signals)
        self.watchdog.save()

    def _build_runtime(self, install, d: host.Discovered, beacons: list, beacon_err: str, signals: dict) -> dict:
        sess = self.pm.active_session(install.install_id)
        mode = host.effective_launch_mode(install, self.controller.apps_dir)
        prefix = host.install_prefix(install) or str(host.terminal_dir(install))
        try:
            free = shutil.disk_usage(prefix).free
        except OSError:
            free = 0
        return {
            "running": d.running,
            "terminalPid": d.terminal_pid,
            "wrapperPid": d.wrapper_pid,
            "adopted": bool(sess.adopted) if sess else False,
            "launchMode": mode,
            "beacons": beacons,
            "beaconReadError": beacon_err,
            "watchdog": self.watchdog.state(install.install_id).to_payload(),
            "journalSignals": {k: bool(signals.get(k)) for k in ("oldTick", "invalidBook", "debuggerFound")},
            "eaBinarySha256": self.sha_cache.get(host.mql5_dir(install) / "Experts" / EA_BINARY_NAME),
            "diskFreeBytes": int(free),
        }

    # ── engine I/O ─────────────────────────────────────────────────────
    def installs_payload(self, base: list[dict]) -> list[dict]:
        """The lean install rows main.py already sends, each with its C4 ``runtime`` added."""
        out = []
        for row in base:
            rt = self._runtime.get(row.get("installId"))
            out.append({**row, "runtime": rt} if rt is not None else row)
        return out

    def apply_heartbeat_response(self, body: dict) -> None:
        self.desired.apply(extract_desired(body))

    def _enter(self, iid: str) -> None:
        with self._inflight_lock:
            self._inflight[iid] = self._inflight.get(iid, 0) + 1

    def _leave(self, iid: str) -> None:
        with self._inflight_lock:
            self._inflight[iid] = max(0, self._inflight.get(iid, 0) - 1)

    def run_job(self, job: dict) -> tuple[str, str, dict]:
        iid = job.get("installId")
        lifecycle = job.get("type") in ("StartTerminal", "StopTerminal", "RestartTerminal") and iid
        if lifecycle:
            self._enter(iid)
        try:
            return self.jobs.execute(job)
        finally:
            if lifecycle:
                self._leave(iid)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["Supervisor", "FAILED"]
