"""MT5 terminal sessions — persistent and adoptable.

One ``TerminalSession`` per terminal run of an install.  Sessions are written to
``<state_dir>/sessions.json`` so a supervisor restart keeps the same session ids (the engine keys
its TerminalSession rows on them), and every ``refresh()`` discovers what is actually running
(:mod:`.host`) and **adopts** terminals the supervisor did not start itself — the live terminals
are hand-launched with ``open -a``, and they must be tracked, restartable and reported anyway.

Starting and stopping is delegated to :class:`.terminal_control.TerminalController`.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shlex
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Optional

import psutil

from . import host
from .wine import _derive_wine_prefix, _resolve_wine_binary, build_wine_command, build_wine_env  # noqa: F401 (re-exported)

logger = logging.getLogger("lascodia.supervisor.process")

ACTIVE = ("Starting", "Running", "Stopping")
STARTING_GRACE_SEC = 120
MAX_ENDED_SESSIONS = 30


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow_iso() -> str:
    return _iso(time.time())


def _epoch(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class TerminalSession:
    session_id: str
    install_id: str
    pid: Optional[int]
    status: str  # Starting / Running / Stopping / Stopped / Crashed
    launched_at: str
    last_seen_at: str
    stopped_at: Optional[str] = None
    reason: Optional[str] = None
    wrapper_pid: Optional[int] = None
    adopted: bool = False
    launch_mode: Optional[str] = None

    def to_payload(self) -> dict:
        # Engine HeartbeatSessionItem shape (extra fields are ignored by older engines).
        return {
            "daemonSessionId": self.session_id,
            "installId":       self.install_id,
            "pid":             self.pid,
            "status":          self.status,
            "stoppedAt":       self.stopped_at,
            "wrapperPid":      self.wrapper_pid,
            "adopted":         self.adopted,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TerminalSession":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


class ProcessManager:
    """Thread-safe registry of the install terminals this daemon supervises."""

    def __init__(self, state_dir: Optional[str] = None, controller=None,
                 get_installs: Optional[Callable[[], list]] = None) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()
        self._path = Path(state_dir) / "sessions.json" if state_dir else None
        self.controller = controller
        self._get_installs = get_installs or (lambda: [])
        self.last_discovery: dict[str, host.Discovered] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
            for raw in rows if isinstance(rows, list) else []:
                s = TerminalSession.from_dict(raw)
                self._sessions[s.session_id] = s
            logger.info("loaded %d persisted session(s) from %s", len(self._sessions), self._path)
        except (OSError, ValueError, TypeError) as ex:
            logger.warning("sessions file %s unreadable (%s) — starting empty", self._path, type(ex).__name__)

    def _save_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([asdict(s) for s in self._sessions.values()], indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as ex:
            logger.warning("could not persist sessions: %s", ex)

    # ── read ────────────────────────────────────────────────────────────
    def list_sessions(self) -> list[TerminalSession]:
        with self._lock:
            return list(self._sessions.values())

    def get(self, session_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def active_session(self, install_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._active_locked(install_id)

    def _active_locked(self, install_id: str) -> Optional[TerminalSession]:
        act = [s for s in self._sessions.values() if s.install_id == install_id and s.status in ACTIVE]
        return max(act, key=lambda s: s.launched_at) if act else None

    def _install(self, install_id: str):
        return next((i for i in self._get_installs() if i.install_id == install_id), None)

    # ── reconcile with reality ─────────────────────────────────────────
    def refresh(self, discovered: Optional[dict[str, host.Discovered]] = None) -> dict[str, host.Discovered]:
        """Discover what runs, adopt what we did not start, close sessions whose terminal died."""
        if discovered is None:
            discovered = self.controller.discover_all() if self.controller else {}
        self.last_discovery = discovered
        now = time.time()
        now_iso = _iso(now)
        installs = {i.install_id for i in self._get_installs()}
        with self._lock:
            for iid, d in discovered.items():
                sess = self._active_locked(iid)
                if d.running:
                    if sess is None:
                        sess = TerminalSession(
                            session_id=str(uuid.uuid4()), install_id=iid, pid=d.terminal_pid, status="Running",
                            launched_at=_iso(d.terminal_started_at or now), last_seen_at=now_iso,
                            reason=f"adopted: terminal already running (mapped by {d.mapped_by or '?'})",
                            wrapper_pid=d.wrapper_pid, adopted=True)
                        self._sessions[sess.session_id] = sess
                        logger.info("install %s: adopted running terminal pid=%s wrapper=%s",
                                    iid, d.terminal_pid, d.wrapper_pid)
                    elif sess.pid not in (None, d.terminal_pid):
                        # Restarted behind our back: the old run ended, this is a new one.
                        sess.status, sess.stopped_at = "Stopped", now_iso
                        sess = TerminalSession(
                            session_id=str(uuid.uuid4()), install_id=iid, pid=d.terminal_pid, status="Running",
                            launched_at=_iso(d.terminal_started_at or now), last_seen_at=now_iso,
                            reason="adopted: terminal restarted outside the supervisor",
                            wrapper_pid=d.wrapper_pid, adopted=True)
                        self._sessions[sess.session_id] = sess
                    else:
                        sess.pid, sess.wrapper_pid, sess.status = d.terminal_pid, d.wrapper_pid, "Running"
                    sess.last_seen_at = now_iso
                elif sess is not None:
                    if sess.status == "Starting" and now - _epoch(sess.launched_at) < STARTING_GRACE_SEC:
                        sess.last_seen_at = now_iso
                        continue
                    sess.status = "Stopped" if sess.status == "Stopping" else "Crashed"
                    sess.stopped_at = sess.stopped_at or now_iso
                    sess.pid = None
                    sess.last_seen_at = now_iso
            for s in self._sessions.values():
                if s.install_id not in installs and s.status in ACTIVE:
                    s.status, s.stopped_at, s.pid = "Stopped", now_iso, None
            ended = sorted((s for s in self._sessions.values() if s.status not in ACTIVE),
                           key=lambda s: s.stopped_at or s.last_seen_at, reverse=True)
            for s in ended[MAX_ENDED_SESSIONS:]:
                del self._sessions[s.session_id]
            self._save_locked()
        return discovered

    def note_launch(self, install_id: str, reason: Optional[str], mode: Optional[str]) -> TerminalSession:
        """Record a launch we just triggered (pid unknown until the next refresh in app mode)."""
        now_iso = _utcnow_iso()
        with self._lock:
            prev = self._active_locked(install_id)
            if prev is not None and prev.status in ("Running", "Starting") and prev.pid is not None:
                return prev
            if prev is not None:
                prev.status, prev.stopped_at = "Stopped", now_iso
            sess = TerminalSession(session_id=str(uuid.uuid4()), install_id=install_id, pid=None,
                                   status="Starting", launched_at=now_iso, last_seen_at=now_iso,
                                   reason=reason, launch_mode=mode)
            self._sessions[sess.session_id] = sess
            self._save_locked()
            return sess

    def note_stopping(self, install_id: str) -> None:
        with self._lock:
            s = self._active_locked(install_id)
            if s is not None:
                s.status = "Stopping"
                self._save_locked()

    # ── launch / close (legacy engine-push endpoints) ───────────────────
    def launch(self, install, reason: Optional[str] = None) -> TerminalSession:
        """Start the install's terminal without waiting for it.  Raises RuntimeError on failure."""
        if self.controller is None:
            raise RuntimeError("no terminal controller configured")
        res = self.controller.start(install, reason=reason or "launch requested", wait=False)
        if not res.ok:
            raise RuntimeError(res.message)
        if res.data.get("alreadyRunning"):
            self.refresh()
            sess = self.active_session(install.install_id)
            if sess is not None:
                return sess
        return self.note_launch(install.install_id, reason, res.data.get("launchMode"))

    def close(self, session_id: str, grace_seconds: int = 20) -> bool:
        sess = self.get(session_id)
        if not sess or sess.status not in ACTIVE or self.controller is None:
            return False
        install = self._install(sess.install_id)
        if install is None:
            return False
        self.note_stopping(sess.install_id)
        self.controller.stop(install, reason=f"close session {session_id}")
        self.refresh()
        return True

    # ── orphan scan / direct kill ───────────────────────────────────────
    def _tracked_pids(self) -> set[int]:
        with self._lock:
            out: set[int] = set()
            for s in self._sessions.values():
                if s.status in ACTIVE:
                    out.update(p for p in (s.pid, s.wrapper_pid) if p is not None)
            return out

    def scan_orphans(self, installs: "list | None" = None) -> list[dict]:
        """terminal64.exe processes that belong to no configured install (adopted ones are tracked)."""
        tracked = self._tracked_pids()
        ops = self.controller.ops if self.controller else host.HostOps()
        orphans: list[dict] = []
        for p in ops.list_processes():
            if not host.is_terminal_process(p) or p.pid in tracked:
                continue
            exe_path = next((a for a in p.cmdline if a.lower().endswith("terminal64.exe")), None)
            orphans.append({
                "pid": p.pid, "name": p.name, "exePath": exe_path,
                "cmdline": " ".join(shlex.quote(c) for c in p.cmdline[:8]),
                "startedAt": _iso(p.create_time or 0), "installHint": None,
            })
        return orphans

    def kill_pid(self, pid: int, grace_seconds: int = 10, force: bool = False) -> dict:
        """Terminate an untracked MT5 terminal.  Raises LookupError / ValueError / PermissionError."""
        if pid in self._tracked_pids():
            raise ValueError(f"PID {pid} belongs to a tracked session — stop it through its install instead")
        try:
            p = psutil.Process(pid)
            info = host.ProcInfo(pid=pid, ppid=p.ppid(), name=p.name() or "", exe="",
                                 cmdline=tuple(p.cmdline()), create_time=p.create_time())
        except psutil.NoSuchProcess as ex:
            raise LookupError(f"no process with pid {pid}") from ex
        except psutil.AccessDenied as ex:
            raise PermissionError(str(ex)) from ex
        if not host.is_terminal_process(info):
            raise ValueError(f"PID {pid} is not an MT5 terminal (name={info.name!r}); refusing to signal it")
        try:
            if force:
                p.kill()
                try:
                    p.wait(timeout=5)
                    return {"ok": True, "method": "SIGKILL", "exitedWithinGrace": True}
                except psutil.TimeoutExpired:
                    return {"ok": True, "method": "SIGKILL", "exitedWithinGrace": False}
            p.terminate()
            try:
                p.wait(timeout=grace_seconds)
                return {"ok": True, "method": "SIGTERM", "exitedWithinGrace": True}
            except psutil.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=5)
                except psutil.TimeoutExpired:
                    pass
                return {"ok": True, "method": "SIGTERM->SIGKILL", "exitedWithinGrace": False}
        except psutil.NoSuchProcess:
            return {"ok": True, "method": "SIGKILL" if force else "SIGTERM", "exitedWithinGrace": True}

    # ── compatibility shims ─────────────────────────────────────────────
    _build_env = staticmethod(build_wine_env)
    _build_command = staticmethod(build_wine_command)
