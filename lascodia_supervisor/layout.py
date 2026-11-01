"""Where a bundle install lives, and how the active version is chosen.

    <root>/                         macOS: ~/Library/Application Support/Lascodia/supervisor
                                    Windows: %ProgramData%\\Lascodia\\Supervisor
      versions/<version>/           one unpacked bundle per version (python/, VERSION, MANIFEST.sha256, launcher/)
      current                       macOS: symlink -> versions/<version>
      current.txt                   Windows: the active version's name (a pointer file, no junctions)
      bin/run.sh | bin/run.ps1      the launcher launchd / the Scheduled Task starts; it rolls back a
                                    pending update that never came up, then runs current's Python
      config/supervisor.yaml        + machine_credential.json, state/ (identity survives upgrades)
      logs/
      update.pending                key=value: from, to, deadline (epoch s) — set while a new version
                                    has not yet registered with the engine
      update.bad                    versions that were rolled back, one per line (never retried)

A developer checkout (``pip install -e``) is not a bundle install: :func:`install_root` returns None
and self-update stays off.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Optional

PENDING = "update.pending"
BAD = "update.bad"
LAST = "update.last"
ROLLED_BACK = "update.rolledback"


def platform_tag() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x64"}.get(machine, machine)
    if sys.platform == "darwin":
        return f"macos-{arch}"
    if os.name == "nt":
        return "windows-x64" if arch in ("x64", "x86_64") else f"windows-{arch}"
    return f"linux-{arch}"


def default_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Lascodia" / "Supervisor"
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "Lascodia" / "supervisor"


def install_root() -> Optional[Path]:
    """The bundle install this process runs from, or None (developer checkout)."""
    env = os.environ.get("LASCODIA_SUPERVISOR_HOME")
    if env:
        p = Path(env)
        return p if (p / "versions").is_dir() else None
    exe = Path(sys.executable)
    for parent in exe.parents:
        if parent.parent.name == "versions" and (parent / "VERSION").is_file():
            return parent.parent.parent
    return None


def bundle_python(version_dir: Path, windowed: bool = False) -> Path:
    if os.name == "nt" or (version_dir / "python" / "python.exe").exists():
        return version_dir / "python" / ("pythonw.exe" if windowed else "python.exe")
    return version_dir / "python" / "bin" / "python3"


def read_current(root: Path) -> Optional[str]:
    link = root / "current"
    if link.is_symlink():
        return Path(os.readlink(link)).name
    ptr = root / "current.txt"
    try:
        v = ptr.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def set_current(root: Path, version: str, style: Optional[str] = None) -> None:
    """Point ``current`` at ``versions/<version>`` atomically (symlink on macOS, pointer file on Windows)."""
    style = style or ("pointer" if os.name == "nt" else "symlink")
    if not (root / "versions" / version).is_dir():
        raise FileNotFoundError(f"versions/{version} does not exist")
    if style == "symlink":
        tmp = root / "current.new"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(os.path.join("versions", version), tmp)
        os.replace(tmp, root / "current")
    else:
        tmp = root / "current.txt.new"
        tmp.write_text(version + "\n", encoding="utf-8")
        os.replace(tmp, root / "current.txt")


def read_kv(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_kv(path: Path, values: dict) -> None:
    tmp = path.with_name(path.name + ".new")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    os.replace(tmp, path)


def bad_versions(root: Path) -> set[str]:
    try:
        return {l.strip() for l in (root / BAD).read_text(encoding="utf-8").splitlines() if l.strip()}
    except OSError:
        return set()
