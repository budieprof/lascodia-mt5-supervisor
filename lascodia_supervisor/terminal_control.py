"""Start, stop and restart one install's terminal — the only code that signals MT5 processes.

Stop order (learned live, see the self-heal brief): SIGTERM terminal64 → wait → SIGKILL if it
ignored us; then SIGTERM the wrapper app (``<app>/Contents/MacOS/MetaTrader 5``) → wait →
SIGKILL; then, if the prefix's wineserver lingers, SIGTERM it.  Killing only terminal64 leaves
the wrapper running and a later ``open -a`` then launches nothing.  A wedged terminal ignores
``osascript quit`` but exits on SIGTERM in ~4 s.

Every call for an install holds that install's lock, so a job and the watchdog never act on
the same terminal at once.
"""
from __future__ import annotations

import logging
import os
import shlex
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import host
from .wine import build_wine_command, build_wine_env

logger = logging.getLogger("lascodia.supervisor.control")


@dataclass
class ActionResult:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)


class TerminalController:
    def __init__(self, ops: host.HostOps, get_installs: Callable[[], list],
                 stop_grace_sec: float = 20.0, start_timeout_sec: float = 90.0,
                 apps_dir: str = host.APPLICATIONS_DIR,
                 before_launch: Optional[Callable[[object], None]] = None) -> None:
        self.ops = ops
        self._get_installs = get_installs
        self.stop_grace_sec = stop_grace_sec
        self.start_timeout_sec = start_timeout_sec
        self.apps_dir = apps_dir
        self.before_launch = before_launch
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── helpers ─────────────────────────────────────────────────────────
    def lock_for(self, install_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(install_id, threading.Lock())

    def busy(self, install_id: str) -> bool:
        return self.lock_for(install_id).locked()

    def discover_all(self) -> dict[str, host.Discovered]:
        return host.discover(self._get_installs(), self.ops.list_processes(), self.ops.prefix_of_pid, self.apps_dir)

    def discover(self, install) -> host.Discovered:
        return host.discover([install], self.ops.list_processes(), self.ops.prefix_of_pid,
                             self.apps_dir)[install.install_id]

    def _term_then_kill(self, pid: int, what: str, grace: float) -> str:
        if not self.ops.alive(pid):
            return f"{what} {pid} already gone"
        logger.info("stopping %s pid=%d (SIGTERM, grace %.0fs)", what, pid, grace)
        self.ops.terminate(pid)
        if self.ops.wait_gone(pid, grace):
            return f"{what} {pid} exited"
        logger.warning("%s pid=%d ignored SIGTERM — SIGKILL", what, pid)
        self.ops.kill(pid)
        if self.ops.wait_gone(pid, 5):
            return f"{what} {pid} killed"
        return f"{what} {pid} STILL RUNNING after SIGKILL"

    # ── stop ────────────────────────────────────────────────────────────
    def stop(self, install, reason: str = "") -> ActionResult:
        with self.lock_for(install.install_id):
            return self._stop_locked(install, reason)

    def _stop_locked(self, install, reason: str) -> ActionResult:
        d = self.discover(install)
        if not d.running and d.wrapper_pid is None:
            return ActionResult(True, "terminal was not running", {"wasRunning": False})
        logger.info("install %s: stopping terminal (%s)", install.install_id, reason or "no reason given")
        steps: list[str] = []
        if d.terminal_pid is not None:
            steps.append(self._term_then_kill(d.terminal_pid, "terminal64", self.stop_grace_sec))
        if d.wrapper_pid is not None:
            steps.append(self._term_then_kill(d.wrapper_pid, "wrapper app", 10))
        # A wineserver normally exits on its own a few seconds after its last client.
        for ws in d.wineserver_pids:
            if self.ops.alive(ws) and not self.ops.wait_gone(ws, 10):
                steps.append(self._term_then_kill(ws, "wineserver", 5))
        after = self.discover(install)
        ok = not after.running and after.wrapper_pid is None
        msg = "; ".join(steps) if ok else "terminal did not stop: " + "; ".join(steps)
        return ActionResult(ok, msg, {"wasRunning": True, "previousTerminalPid": d.terminal_pid,
                                      "previousWrapperPid": d.wrapper_pid})

    # ── start ───────────────────────────────────────────────────────────
    def start(self, install, reason: str = "", wait: bool = True) -> ActionResult:
        with self.lock_for(install.install_id):
            return self._start_locked(install, reason, wait)

    def _start_locked(self, install, reason: str, wait: bool) -> ActionResult:
        d = self.discover(install)
        if d.running:
            return ActionResult(True, f"terminal already running (pid {d.terminal_pid})",
                                {"terminalPid": d.terminal_pid, "wrapperPid": d.wrapper_pid,
                                 "alreadyRunning": True})
        if self.before_launch is not None:
            try:
                self.before_launch(install)
            except Exception:  # never block a launch over the enrollment token
                logger.exception("install %s: pre-launch step failed; launching anyway", install.install_id)
        mode = host.effective_launch_mode(install, self.apps_dir)
        try:
            if mode == "app":
                app = host.resolve_app_path(install, self.apps_dir)
                if not app or not os.path.isdir(app):
                    return ActionResult(False, f"cannot start: wrapper app not found ({app or 'no app_path'})")
                if d.wrapper_pid is not None:
                    # A wrapper with no terminal: `open -a` would just focus it and start nothing.
                    self._term_then_kill(d.wrapper_pid, "orphaned wrapper app", 10)
                logger.info("install %s: open -a %s (%s)", install.install_id, shlex.quote(app), reason)
                self.ops.open_app(app, list(install.extra_args))
            elif mode == "native":
                # Windows: terminal64.exe straight in this user's session — no wrapper, no wineserver.
                exe = os.path.expanduser(install.executable)
                if not os.path.isfile(exe):
                    return ActionResult(False, f"cannot start: {exe} not found")
                argv = [exe, *install.extra_args]
                logger.info("install %s: starting %s (%s)", install.install_id, exe, reason)
                self.ops.spawn(argv, dict(os.environ), os.path.dirname(exe))
            else:
                argv = build_wine_command(install)
                logger.info("install %s: spawning %s (%s)", install.install_id,
                            " ".join(shlex.quote(a) for a in argv), reason)
                self.ops.spawn(argv, build_wine_env(install))
        except (RuntimeError, OSError) as ex:
            return ActionResult(False, f"cannot start: {ex}")
        if not wait:
            return ActionResult(True, f"launch requested ({mode} mode)", {"launchMode": mode})
        waited = 0.0
        while waited < self.start_timeout_sec:
            self.ops.sleep(2)
            waited += 2
            d = self.discover(install)
            if d.running:
                return ActionResult(True, f"terminal started (pid {d.terminal_pid}, {mode} mode)",
                                    {"terminalPid": d.terminal_pid, "wrapperPid": d.wrapper_pid, "launchMode": mode})
        return ActionResult(False, f"terminal did not appear within {int(self.start_timeout_sec)} s of launching "
                                   f"({mode} mode)", {"launchMode": mode})

    # ── restart ─────────────────────────────────────────────────────────
    def restart(self, install, reason: str = "") -> ActionResult:
        with self.lock_for(install.install_id):
            stopped = self._stop_locked(install, reason)
            if not stopped.ok:
                return ActionResult(False, f"restart aborted: {stopped.message}", stopped.data)
            started = self._start_locked(install, reason, wait=True)
            return ActionResult(started.ok, f"{stopped.message}; {started.message}", {**stopped.data, **started.data})
