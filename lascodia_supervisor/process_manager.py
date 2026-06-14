"""MT5 terminal process lifecycle.

Tracks one in-memory ``TerminalSession`` per launched ``terminal64.exe``
process — start, observe via psutil, request graceful close via SIGTERM
then SIGKILL after grace.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import shlex
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import psutil


# Auto-discovery candidates for a Wine binary on macOS / Linux when the
# operator left `wine_binary` unset and we need to wrap a .exe.  Order
# matters: PATH first (so a user-installed wine takes precedence), then
# the MetaTrader 5.app bundled wine64 (which already owns the prefix
# MT5 uses), then CrossOver, then Homebrew Cellar fallback.
_WINE_DISCOVERY_PATHS = [
    "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64",
    "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine",
    "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine",
    "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine64",
    "/opt/homebrew/bin/wine",
    "/opt/homebrew/bin/wine64",
    "/usr/local/bin/wine",
    "/usr/local/bin/wine64",
]


def _derive_wine_prefix(executable: str) -> Optional[str]:
    """Walk up from `terminal64.exe` until we find the Wine prefix root.

    A Wine prefix root has a ``drive_c`` child directory.  The parent
    of ``drive_c`` is the prefix.  For the standard MT5 macOS layout
    this resolves to ``~/Library/Application Support/<bundleId>/``.
    Returns None when the executable doesn't live under a drive_c
    tree — operators with bespoke setups should set ``wine_prefix``
    explicitly in supervisor.yaml.
    """
    p = Path(executable).resolve()
    for parent in p.parents:
        if parent.name == "drive_c":
            return str(parent.parent)
    return None


def _resolve_wine_binary(explicit: Optional[str]) -> Optional[str]:
    """Find the Wine binary to use.

    Returns the absolute path when found, or ``None`` when no Wine is
    installed (caller should surface a clear error).  Resolution
    order: explicit config field → PATH → known macOS bundle paths.
    """
    if explicit:
        if Path(explicit).is_file() and os.access(explicit, os.X_OK):
            return explicit
        # Operator set a path that doesn't exist — fall through to
        # auto-discovery so a stale config doesn't permanently break
        # launches.  We log the override-miss at the call site.
        logger.warning("wine_binary='%s' not executable — falling back to auto-discovery", explicit)
    found = shutil.which("wine") or shutil.which("wine64")
    if found:
        return found
    for candidate in _WINE_DISCOVERY_PATHS:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None

from .config import MT5Install

logger = logging.getLogger("lascodia.supervisor.process")


@dataclass
class TerminalSession:
    session_id: str
    install_id: str
    pid: Optional[int]
    status: str  # Starting / Running / Stopping / Stopped / Crashed / Unknown
    launched_at: str  # ISO-8601 UTC
    last_seen_at: str  # ISO-8601 UTC
    stopped_at: Optional[str] = None
    reason: Optional[str] = None
    # Process handle is intentionally NOT serialised — it's runtime-only state.
    _process: Optional[subprocess.Popen] = field(default=None, repr=False, compare=False)

    def to_payload(self) -> dict:
        # Phase-16 fix: build the payload directly instead of via
        # asdict(self).  asdict() deep-copies every field — including
        # _process: subprocess.Popen — and Popen holds a threading.Lock
        # internally which can't be deep-copied.  The .pop("_process")
        # below would have stripped it from the result, but asdict
        # blows up on the deepcopy BEFORE it returns, so we never get
        # there.  Observed live: every /terminals heartbeat raised
        # "TypeError: cannot pickle '_thread.lock' object", which
        # cascaded into launch responses returning 500.
        return {
            "daemonSessionId": self.session_id,
            "installId":       self.install_id,
            "pid":             self.pid,
            "status":          self.status,
            "stoppedAt":       self.stopped_at,
        }


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ProcessManager:
    """Thread-safe registry of running MT5 processes the daemon supervises."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    # ── Launch / close ──────────────────────────────────────────────────

    def launch(self, install: MT5Install, reason: Optional[str] = None) -> TerminalSession:
        """Spawn the configured executable and track the resulting process.

        Returns the new session with status ``Starting`` (immediately) or
        ``Running`` if the process is already alive by the time we sample
        it.  Raises ``RuntimeError`` on spawn failure.
        """
        cmd = self._build_command(install)
        spawn_env = self._build_env(install)
        wine_prefix = spawn_env.get("WINEPREFIX")
        logger.info(
            "launching install_id=%s wine_prefix=%s cmd=%s",
            install.install_id, wine_prefix or "(default)",
            " ".join(shlex.quote(c) for c in cmd),
        )
        try:
            # close_fds=True + start_new_session=True so a daemon shutdown
            # doesn't drag the MT5 process with it.  We want the MT5
            # terminal to keep running across daemon restarts; reconcile via
            # psutil scan instead.
            #
            # env carries WINEPREFIX so each broker's MT5 runs in its
            # own Wine prefix — without this, MetaQuotes' bundle-aware
            # wine64 defaults the prefix to whichever bundle the wine
            # binary lives in, causing every clone-mt5 broker to
            # collide on the original prefix.  See `_build_env`.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=spawn_env,
            )
        except (FileNotFoundError, PermissionError) as ex:
            raise RuntimeError(f"Spawn failed: {ex}") from ex

        now = _utcnow_iso()
        sess = TerminalSession(
            session_id=str(uuid.uuid4()),
            install_id=install.install_id,
            pid=proc.pid,
            status="Starting",
            launched_at=now,
            last_seen_at=now,
            reason=reason,
            _process=proc,
        )
        with self._lock:
            self._sessions[sess.session_id] = sess
        return sess

    def close(self, session_id: str, grace_seconds: int = 10) -> bool:
        """Request a graceful close.  Returns True if a process was signalled,
        False when the session id is unknown or already stopped."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if not sess or sess.pid is None:
            return False
        if sess.status in ("Stopped", "Crashed"):
            return False
        try:
            p = psutil.Process(sess.pid)
            logger.info("closing session=%s pid=%d grace=%ds", session_id, sess.pid, grace_seconds)
            p.terminate()
            try:
                p.wait(timeout=grace_seconds)
            except psutil.TimeoutExpired:
                logger.warning("session=%s pid=%d ignored SIGTERM — sending SIGKILL", session_id, sess.pid)
                p.kill()
        except psutil.NoSuchProcess:
            logger.info("session=%s pid=%d already gone", session_id, sess.pid)
        with self._lock:
            sess.status = "Stopping"  # the next refresh() will flip to Stopped
            sess.last_seen_at = _utcnow_iso()
        return True

    # ── Read ────────────────────────────────────────────────────────────

    def list_sessions(self) -> list[TerminalSession]:
        with self._lock:
            return list(self._sessions.values())

    def get(self, session_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._sessions.get(session_id)

    # ── Orphan scan / direct kill ───────────────────────────────────────

    def scan_orphans(self, installs: "list[MT5Install] | None" = None) -> list[dict]:
        """List MT5 terminal64.exe processes the daemon is not tracking.

        Returns one dict per orphan with enough information for an
        operator to recognise which terminal it is and decide whether
        to kill it: pid, cmdline tail, started-at, broker hint
        (matched against the configured installs' executable paths
        when ``installs`` is supplied).

        Pre-existing MT5 instances launched by the operator outside the
        daemon (the common case after install) show up here — they are
        NOT broken, just untracked.  Useful for cleanup when a stale
        MT5 is holding a Wine prefix lock and blocking a fresh launch.
        """
        with self._lock:
            tracked_pids = {s.pid for s in self._sessions.values() if s.pid is not None}

        # Build a path → install_id index up front so we don't iterate
        # installs once per process.  Path comparison is exact because
        # MT5's terminal64.exe lives at a deterministic location per
        # Wine prefix and operators don't usually symlink it.
        install_by_exe: dict[str, str] = {}
        for i in (installs or []):
            if i.executable:
                install_by_exe[i.executable] = i.install_id

        orphans: list[dict] = []
        for p in psutil.process_iter(attrs=["pid", "name", "create_time"]):
            try:
                name = (p.info.get("name") or "").lower()
                if name != "terminal64.exe":
                    continue
                if p.pid in tracked_pids:
                    continue
                # cmdline + cwd require additional ACL on macOS — guard
                # each call so a permission error on one process doesn't
                # break the whole scan.
                try:
                    cmdline = p.cmdline()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    cmdline = []
                # The cmdline of a wine-wrapped MT5 starts with the wine
                # binary; the *.exe path is the first .exe-suffixed arg.
                exe_path = next((a for a in cmdline if a.lower().endswith("terminal64.exe")), None)
                install_id = install_by_exe.get(exe_path) if exe_path else None
                started_at = dt.datetime.fromtimestamp(
                    p.info.get("create_time") or 0, dt.timezone.utc
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                orphans.append({
                    "pid":        p.pid,
                    "name":       p.info.get("name"),
                    "exePath":    exe_path,
                    "cmdline":    " ".join(shlex.quote(c) for c in cmdline[:8]),
                    "startedAt":  started_at,
                    "installHint": install_id,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return orphans

    def kill_pid(self, pid: int, grace_seconds: int = 10, force: bool = False) -> dict:
        """Terminate a PID that isn't tracked as a daemon session.

        Refuses to act on PIDs that ARE tracked — the operator must
        use the regular session close endpoint for those so the
        session state machine stays consistent.  Returns a dict
        describing what happened: {ok, method, exitedWithinGrace}.
        ``method`` is ``SIGKILL`` when ``force=True`` (skip the
        graceful pass), else ``SIGTERM->SIGKILL`` when grace elapsed,
        else ``SIGTERM``.  Raises ``LookupError`` for unknown PID,
        ``PermissionError`` when the OS rejects the signal, and
        ``ValueError`` when the PID is daemon-tracked.
        """
        with self._lock:
            tracked_pids = {s.pid for s in self._sessions.values() if s.pid is not None}
        if pid in tracked_pids:
            raise ValueError(
                f"PID {pid} belongs to a tracked session — use the close-session endpoint instead "
                "so the daemon's session state stays consistent")

        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess as ex:
            raise LookupError(f"no process with pid {pid}") from ex

        # Sanity check: only kill MT5 terminals.  Operator passing a
        # random PID through this endpoint shouldn't be able to take
        # down arbitrary host processes.
        try:
            name = (p.name() or "").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            name = ""
        if name != "terminal64.exe":
            raise ValueError(
                f"PID {pid} is not an MT5 terminal (name={name!r}); refusing to signal non-MT5 processes")

        if force:
            logger.info("force-killing orphan pid=%d", pid)
            try:
                p.kill()
            except psutil.NoSuchProcess:
                return {"ok": True, "method": "SIGKILL", "exitedWithinGrace": True}
            try:
                p.wait(timeout=5)
                return {"ok": True, "method": "SIGKILL", "exitedWithinGrace": True}
            except psutil.TimeoutExpired:
                return {"ok": True, "method": "SIGKILL", "exitedWithinGrace": False}

        logger.info("terminating orphan pid=%d grace=%ds", pid, grace_seconds)
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            return {"ok": True, "method": "SIGTERM", "exitedWithinGrace": True}
        try:
            p.wait(timeout=grace_seconds)
            return {"ok": True, "method": "SIGTERM", "exitedWithinGrace": True}
        except psutil.TimeoutExpired:
            logger.warning("orphan pid=%d ignored SIGTERM — sending SIGKILL", pid)
            try:
                p.kill()
                p.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
            return {"ok": True, "method": "SIGTERM->SIGKILL", "exitedWithinGrace": False}

    # ── Periodic reconcile ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Inspect each tracked process and update its status.  Called
        periodically from the heartbeat loop so the next heartbeat to the
        engine carries fresh state."""
        now = _utcnow_iso()
        with self._lock:
            sessions = list(self._sessions.values())
        for sess in sessions:
            if sess.pid is None:
                continue
            try:
                p = psutil.Process(sess.pid)
                if p.is_running():
                    # MT5 may briefly be a zombie on graceful exit; treat
                    # zombies as Stopped immediately.
                    if p.status() == psutil.STATUS_ZOMBIE:
                        sess.status = "Stopped"
                        sess.stopped_at = sess.stopped_at or now
                    else:
                        # Transition Starting → Running once the process has
                        # actually been observed alive at least once.
                        if sess.status == "Starting":
                            sess.status = "Running"
                else:
                    sess.status = "Stopped"
                    sess.stopped_at = sess.stopped_at or now
            except psutil.NoSuchProcess:
                # Either crashed or normal exit — we can't tell from here.
                # Heuristic: if previous status was Stopping (we asked to
                # close), call it Stopped; otherwise Crashed so operators
                # can spot abnormal exits.
                if sess.status == "Stopping":
                    sess.status = "Stopped"
                else:
                    sess.status = "Crashed"
                sess.stopped_at = sess.stopped_at or now
                sess.pid = None
            sess.last_seen_at = now

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_env(install: MT5Install) -> dict:
        """Build the env vector for the spawn.

        Inherits the daemon's env then overlays ``WINEPREFIX`` so each
        install's MT5 runs in its own Wine prefix.  ``WINEPREFIX``
        resolution order:
          1. explicit ``install.wine_prefix`` (operator override)
          2. derived from the executable path (parent of ``drive_c``)
          3. leave the daemon's existing ``WINEPREFIX`` alone (None →
             Wine falls back to its compiled-in default).

        This is THE fix for the multi-broker collision bug: when a
        second clone-mt5 broker launched, MetaQuotes' bundle-aware
        wine64 would default WINEPREFIX to the *original* MT5 bundle's
        prefix, and MT5's single-instance check inside that prefix
        would kill the broker-A terminal as soon as broker-B started.
        Setting WINEPREFIX explicitly per install isolates them
        completely.
        """
        env = dict(os.environ)
        prefix = install.wine_prefix or _derive_wine_prefix(install.executable)
        if prefix:
            env["WINEPREFIX"] = prefix
        return env

    @staticmethod
    def _build_command(install: MT5Install) -> list[str]:
        """Build the subprocess command vector from the install config.

        Honours ``use_wine`` for non-Windows hosts so the same config
        can run on macOS dev boxes (which wrap terminal64.exe in Wine)
        and on Windows VPSes (which run it natively).

        Wine resolution: when ``use_wine=True`` and the operator didn't
        write a Wine binary into ``install.executable`` themselves, we
        call ``_resolve_wine_binary`` to find one.  On macOS the
        official MetaTrader 5.app bundles its own ``wine64`` at
        ``/Applications/MetaTrader 5.app/Contents/SharedSupport/
        wine/bin/wine64`` — using that same binary keeps the Wine
        prefix consistent with what MT5 created.  Falling back to a
        system Homebrew Wine is a last resort.

        Raises ``RuntimeError`` when Wine is required but absent,
        bubbling a clear "Spawn failed: no Wine binary available"
        instead of the cryptic ``Errno 2 No such file: 'wine'`` the
        bare ``Popen`` would surface.
        """
        is_windows = platform.system() == "Windows"
        # `executable` is a single filesystem path.  Splitting it with
        # shlex was a pre-v0.2 footgun: any path with spaces (e.g. the
        # standard macOS Wine MT5 location at
        # `/Users/.../Library/Application Support/.../terminal64.exe`)
        # got shattered into four wrong args.  Treat it as one path,
        # always; operators who need to wrap with a custom command use
        # the `wine_binary` field or `extra_args`.
        exe_path = install.executable
        argv: list[str] = []

        if install.use_wine and not is_windows:
            # If the operator already pre-wrapped the executable as a
            # shell-style command (legacy `wine /path/to/x.exe`), don't
            # double-wrap.  Detection: starts with a Wine token and
            # contains whitespace.
            stripped = exe_path.strip()
            if (stripped.startswith("wine ") or stripped.startswith("wine64 ")
                    or "/wine " in stripped or "/wine64 " in stripped):
                argv = shlex.split(stripped)
            else:
                wine_bin = _resolve_wine_binary(install.wine_binary)
                if wine_bin is None:
                    raise RuntimeError(
                        "no Wine binary available for use_wine=true install "
                        f"'{install.install_id}'. Install Wine (`brew install --cask wine-stable`) "
                        "or point `wine_binary` at a specific Wine — on macOS the "
                        "MetaTrader 5.app at /Applications/MetaTrader 5.app/Contents/"
                        "SharedSupport/wine/bin/wine64 is the conventional choice."
                    )
                argv = [wine_bin, exe_path]
        else:
            argv = [exe_path]

        return argv + list(install.extra_args)
