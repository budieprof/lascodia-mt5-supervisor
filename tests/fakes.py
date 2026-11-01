"""Fakes for the self-heal tests: a pretend host (processes, signals, `open -a`) and an install
layout in a temp directory.  Nothing here touches a real process."""
from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from lascodia_supervisor.config import MT5Install
from lascodia_supervisor.host import ProcInfo

WIN_EXE = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"


def make_layout(root: Path, slug: str, bundle_id: Optional[str] = None) -> SimpleNamespace:
    """<root>/Applications/MetaTrader 5 - <slug>.app + <root>/Support/net.metaquotes.wine.<slug>/drive_c/..."""
    bundle_id = bundle_id or f"net.metaquotes.wine.{slug}"
    apps = root / "Applications"
    app = apps / f"MetaTrader 5 - {slug}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    prefix = root / "Library" / "Application Support" / bundle_id.lower()
    term = prefix / "drive_c" / "Program Files" / "MetaTrader 5"
    for sub in ("logs", "MQL5/Experts", "MQL5/Logs", "MQL5/Files/Lascodia/beacon"):
        (term / sub).mkdir(parents=True, exist_ok=True)
    (term / "terminal64.exe").write_bytes(b"MZ")
    install = MT5Install(install_id=slug, name=slug, executable=str(term / "terminal64.exe"), use_wine=True,
                         wine_prefix=str(prefix), managed=True)
    return SimpleNamespace(apps=str(apps), app=str(app), prefix=str(prefix), term=term, mql5=term / "MQL5",
                           install=install)


class FakeHost:
    """Processes + the effects of signals.  ``stubborn`` pids ignore SIGTERM (need SIGKILL)."""

    def __init__(self) -> None:
        self.procs: dict[int, ProcInfo] = {}
        self.lsof: dict[int, str] = {}
        self.calls: list[tuple] = []
        self.stubborn: set[int] = set()
        self.unkillable: set[int] = set()
        self.next_pid = 1000
        self.clock = 1_000_000.0
        self.app_layouts: dict[str, SimpleNamespace] = {}   # app path → layout (what open -a starts)
        self.open_starts_terminal = True
        self.wrapper_exits_with_terminal = False

    # helpers for tests
    def add(self, name: str, exe: str = "", cmd: tuple = (), ppid: int = 1, env_prefix: Optional[str] = None,
            lsof_prefix: Optional[str] = None, created: Optional[float] = None) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = ProcInfo(pid=pid, ppid=ppid, name=name, exe=exe, cmdline=tuple(cmd),
                                   create_time=created if created is not None else self.clock, env_prefix=env_prefix)
        if lsof_prefix:
            self.lsof[pid] = lsof_prefix
        return pid

    def add_terminal(self, layout, created: Optional[float] = None) -> tuple[int, int, int]:
        wrapper = self.add("MetaTrader 5", exe=f"{layout.app}/Contents/MacOS/MetaTrader 5",
                           cmd=(f"{layout.app}/Contents/MacOS/MetaTrader 5",), created=created)
        ws = self.add("wineserver", exe=f"{layout.app}/Contents/SharedSupport/wine/bin/wineserver",
                      env_prefix=layout.prefix, created=created)
        term = self.add("wine", exe=f"{layout.app}/Contents/SharedSupport/wine/lib/wine/x86_64-unix/wine",
                        cmd=(WIN_EXE, ""), ppid=wrapper, lsof_prefix=layout.prefix, created=created)
        return term, wrapper, ws

    # HostOps surface
    def list_processes(self):
        return list(self.procs.values())

    def prefix_of_pid(self, pid):
        return self.lsof.get(pid)

    def alive(self, pid):
        return pid in self.procs

    def terminate(self, pid):
        self.calls.append(("TERM", pid, self.procs[pid].name if pid in self.procs else None))
        if pid in self.procs and pid not in self.stubborn and pid not in self.unkillable:
            self._exit(pid)

    def kill(self, pid):
        self.calls.append(("KILL", pid, self.procs[pid].name if pid in self.procs else None))
        if pid in self.procs and pid not in self.unkillable:
            self._exit(pid)

    def _exit(self, pid):
        info = self.procs.pop(pid)
        if self.wrapper_exits_with_terminal and info.name == "wine":
            self.procs.pop(info.ppid, None)

    def wait_gone(self, pid, timeout):
        return pid not in self.procs

    def open_app(self, app_path, args):
        self.calls.append(("OPEN", app_path, tuple(args)))
        if self.open_starts_terminal and app_path in self.app_layouts:
            self.add_terminal(self.app_layouts[app_path], created=self.clock)

    def spawn(self, argv, env, cwd=None):
        self.calls.append(("SPAWN", tuple(argv), env.get("WINEPREFIX")))
        return self.add("wine64", exe=argv[0], cmd=tuple(argv), env_prefix=env.get("WINEPREFIX"))

    def sleep(self, sec):
        self.clock += sec

    def now(self):
        return self.clock
