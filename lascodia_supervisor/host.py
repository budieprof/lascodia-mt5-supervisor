"""What is running on this host, and which install it belongs to.

Live terminals are started by the macOS MetaTrader wrapper app (``open -a "MetaTrader 5.app"``).
Wine re-parents ``terminal64.exe`` away from the app, psutil reports its name as ``wine``, and
its command line is the *Windows* path ``C:\\Program Files\\MetaTrader 5\\terminal64.exe`` for
every prefix.  So a terminal is recognised by its command line, and mapped to an install by its
Wine prefix: the ``WINEPREFIX`` environment variable when readable, else the files it holds open
(``lsof``: every terminal holds files under ``~/Library/Application Support/<prefix>/``), else the
app bundle its Wine binary lives in.

Everything that touches the OS goes through :class:`HostOps` so the logic above it is tested
with fakes.  Nothing in this module signals a process by itself.
"""
from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import psutil

logger = logging.getLogger("lascodia.supervisor.host")

APPLICATIONS_DIR = "/Applications"
_PREFIX_RE = re.compile(r"^(.*/Library/Application Support/net\.metaquotes\.wine[^/]*)(/|$)", re.IGNORECASE)


# ── process snapshot ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str
    exe: str
    cmdline: tuple[str, ...]
    create_time: float
    env_prefix: Optional[str] = None  # WINEPREFIX when the environment was readable


def is_terminal_process(p: ProcInfo) -> bool:
    """terminal64.exe, whatever psutil calls it ('wine', 'wine64-preloader', 'terminal64.exe')."""
    if p.name.lower() == "terminal64.exe":
        return True
    for arg in p.cmdline[:3]:
        a = arg.strip().lower().replace("\\", "/")
        if a.endswith("/terminal64.exe") or a == "terminal64.exe":
            return True
    return False


def _norm(path: Optional[str]) -> str:
    """Case-insensitive path key (APFS default is case-insensitive: the bundle id
    'net.metaquotes.wine.MetaTrader5' names the prefix directory '...metatrader5')."""
    return os.path.normpath(os.path.expanduser(path)).rstrip("/").lower() if path else ""


def prefix_from_path(path: str) -> Optional[str]:
    m = _PREFIX_RE.match(path or "")
    return m.group(1) if m else None


# ── install geometry ─────────────────────────────────────────────────────────

def install_prefix(install) -> Optional[str]:
    """The install's Wine prefix: ``wine_prefix``, else the parent of the ``drive_c`` above
    terminal64.exe."""
    if getattr(install, "wine_prefix", None):
        return os.path.expanduser(install.wine_prefix)
    exe = Path(os.path.expanduser(install.executable))
    for parent in exe.parents:
        if parent.name == "drive_c":
            return str(parent.parent)
    return None


def terminal_dir(install) -> Path:
    """Directory holding terminal64.exe (and logs/, MQL5/, config/)."""
    return Path(os.path.expanduser(install.executable)).parent


def mql5_dir(install) -> Path:
    if getattr(install, "mql5_dir", None):
        return Path(os.path.expanduser(install.mql5_dir))
    return terminal_dir(install) / "MQL5"


def bundle_identifier(app_path: str) -> Optional[str]:
    try:
        with open(Path(app_path) / "Contents" / "Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleIdentifier") or "") or None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def find_app_for_prefix(prefix: Optional[str], apps_dir: str = APPLICATIONS_DIR) -> Optional[str]:
    """The /Applications/MetaTrader 5*.app whose CFBundleIdentifier is the prefix's directory
    name (the wrapper provisions ~/Library/Application Support/<bundle id>)."""
    if not prefix:
        return None
    want = Path(prefix).name.lower()
    try:
        candidates = sorted(Path(apps_dir).glob("MetaTrader 5*.app"))
    except OSError:
        return None
    for app in candidates:
        bid = bundle_identifier(str(app))
        if bid and bid.lower() == want:
            return str(app)
    return None


def resolve_app_path(install, apps_dir: str = APPLICATIONS_DIR) -> Optional[str]:
    explicit = getattr(install, "app_path", None)
    if explicit:
        return os.path.expanduser(explicit)
    return find_app_for_prefix(install_prefix(install), apps_dir)


def effective_launch_mode(install, apps_dir: str = APPLICATIONS_DIR) -> str:
    mode = getattr(install, "launch_mode", None)
    if mode in ("app", "wine"):
        return mode
    return "app" if resolve_app_path(install, apps_dir) else "wine"


# ── discovery ────────────────────────────────────────────────────────────────

@dataclass
class Discovered:
    install_id: str
    terminal_pid: Optional[int] = None
    terminal_started_at: Optional[float] = None
    wrapper_pid: Optional[int] = None
    wineserver_pids: list[int] = field(default_factory=list)
    mapped_by: str = ""  # env | lsof | exe | wrapper

    @property
    def running(self) -> bool:
        return self.terminal_pid is not None


def discover(installs: Iterable, procs: list[ProcInfo],
             prefix_of_pid: Callable[[int], Optional[str]],
             apps_dir: str = APPLICATIONS_DIR) -> dict[str, Discovered]:
    """Map every terminal / wrapper / wineserver process to the install it belongs to.

    Pure apart from ``prefix_of_pid`` (lsof) and the app-bundle lookup; tests pass both in."""
    installs = list(installs)
    by_prefix: dict[str, str] = {}
    by_app: dict[str, str] = {}
    for inst in installs:
        pre = install_prefix(inst)
        if pre:
            by_prefix[_norm(pre)] = inst.install_id
        app = resolve_app_path(inst, apps_dir)
        if app:
            by_app[_norm(app)] = inst.install_id
    out = {inst.install_id: Discovered(inst.install_id) for inst in installs}

    def app_owner(path: str) -> Optional[str]:
        key = _norm(path)
        for app_key, iid in by_app.items():
            if key == app_key or key.startswith(app_key + "/"):
                return iid
        return None

    wrappers: dict[int, str] = {}
    for p in procs:
        exe = p.exe or (p.cmdline[0] if p.cmdline else "")
        iid = app_owner(exe) if "/contents/macos/" in exe.lower() else None
        if iid:
            wrappers[p.pid] = iid
            out[iid].wrapper_pid = p.pid
        elif p.name.lower() == "wineserver" and p.env_prefix:
            iid2 = by_prefix.get(_norm(p.env_prefix))
            if iid2:
                out[iid2].wineserver_pids.append(p.pid)

    for p in procs:
        if not is_terminal_process(p):
            continue
        iid, how = None, ""
        if p.env_prefix:
            iid, how = by_prefix.get(_norm(p.env_prefix)), "env"
        if iid is None:
            for arg in p.cmdline[:3]:
                pre = prefix_from_path(arg)
                if pre and _norm(pre) in by_prefix:
                    iid, how = by_prefix[_norm(pre)], "exe"
                    break
        if iid is None:
            pre = prefix_of_pid(p.pid)
            if pre:
                iid, how = by_prefix.get(_norm(pre)), "lsof"
        if iid is None and p.ppid in wrappers:
            iid, how = wrappers[p.ppid], "wrapper"
        if iid is None and p.exe:
            iid, how = app_owner(p.exe), "exe"
        if iid is None:
            continue
        d = out[iid]
        # Single-instance per prefix: if two show up, the older one is the real terminal.
        if d.terminal_pid is None or p.create_time < (d.terminal_started_at or float("inf")):
            d.terminal_pid, d.terminal_started_at, d.mapped_by = p.pid, p.create_time, how
    return out


# ── the real OS ──────────────────────────────────────────────────────────────

class HostOps:
    """Real process access.  Tests substitute a fake with the same methods."""

    def __init__(self) -> None:
        self._prefix_cache: dict[tuple[int, float], Optional[str]] = {}

    # snapshot
    def list_processes(self) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        for p in psutil.process_iter(attrs=["pid", "ppid", "name", "create_time"]):
            try:
                info = p.info
                name = info.get("name") or ""
                lname = name.lower()
                try:
                    cmd = tuple(p.cmdline())
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmd = ()
                interesting = (lname in ("wine", "wine64", "wine64-preloader", "wineserver", "terminal64.exe",
                                         "metatrader 5")
                               or any("terminal64.exe" in a.lower() for a in cmd[:3]))
                if not interesting:
                    continue
                try:
                    exe = p.exe() or ""
                except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    exe = ""
                env_prefix = None
                if lname == "wineserver" or lname == "terminal64.exe" or "terminal64.exe" in " ".join(cmd[:3]).lower():
                    try:
                        env_prefix = p.environ().get("WINEPREFIX") or None
                    except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                        env_prefix = None
                out.append(ProcInfo(pid=p.pid, ppid=info.get("ppid") or 0, name=name, exe=exe, cmdline=cmd,
                                    create_time=info.get("create_time") or 0.0, env_prefix=env_prefix))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def prefix_of_pid(self, pid: int) -> Optional[str]:
        """The Wine prefix whose files ``pid`` holds open (``lsof -p``), cached per process."""
        try:
            ct = psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        key = (pid, ct)
        if key in self._prefix_cache:
            return self._prefix_cache[key]
        prefix = None
        try:
            res = subprocess.run(["lsof", "-a", "-p", str(pid), "-Fn"], capture_output=True, text=True,
                                 timeout=10, errors="replace")
            counts: dict[str, int] = {}
            for line in res.stdout.splitlines():
                if line.startswith("n"):
                    pre = prefix_from_path(line[1:])
                    if pre:
                        counts[pre] = counts.get(pre, 0) + 1
            if counts:
                prefix = max(counts.items(), key=lambda kv: kv[1])[0]
        except (OSError, subprocess.SubprocessError):
            prefix = None
        if len(self._prefix_cache) > 256:
            self._prefix_cache.clear()
        self._prefix_cache[key] = prefix
        return prefix

    # signals
    def alive(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def terminate(self, pid: int) -> None:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            pass

    def kill(self, pid: int) -> None:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass

    def wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive(pid):
                return True
            time.sleep(0.5)
        return not self.alive(pid)

    # launch
    def open_app(self, app_path: str, args: list[str]) -> None:
        cmd = ["open", "-a", app_path]
        if args:
            cmd += ["--args", *args]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"open -a failed ({res.returncode}): {res.stderr.strip()[:300]}")

    def spawn(self, argv: list[str], env: dict) -> int:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=env)
        return proc.pid

    def sleep(self, sec: float) -> None:
        time.sleep(sec)

    def now(self) -> float:
        return time.time()
