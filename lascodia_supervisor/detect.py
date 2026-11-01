"""Find the MetaTrader 5 installs on this computer (used by the one-line installer).

macOS (the logic install.sh's ``detect_installs`` had, moved here so the bootstrap scripts stay thin):
  * each ``/Applications/MetaTrader 5*.app`` whose prefix ``~/Library/Application Support/<bundle id>``
    holds a terminal64.exe → ``launch_mode: app``, ``managed: true``;
  * each remaining ``~/Library/Application Support/net.metaquotes.wine.*`` prefix with a
    terminal64.exe but no wrapper app → ``launch_mode: wine``, ``managed: false`` (without the wrapper
    the terminal cannot be started the way MT5 expects; the operator decides).
  The prefix ``net.metaquotes.wine.metatrader5`` keeps the protected id ``wine-mt5-default``.

Windows:
  * every ``terminal64.exe`` in ``<Program Files>\\*MetaTrader*\\`` (and one level deeper);
  * its data folder: the ``%APPDATA%\\MetaQuotes\\Terminal\\<hash>`` whose ``origin.txt`` names the
    install folder, else ``<install>\\MQL5`` for a portable install → ``mql5_dir``;
  * ``launch_mode: native``, ``managed: true``.

Nothing here starts or signals anything; it only reads directories and small text files.
"""
from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path
from typing import Iterable, Optional

from .config import _read_mt5_account_from_common_ini

MAC_TERMINAL_REL = Path("drive_c") / "Program Files" / "MetaTrader 5" / "terminal64.exe"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "mt5"


def _bundle_id(app: Path) -> Optional[str]:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleIdentifier") or "") or None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _account(data_dir: Path) -> tuple[str, str]:
    return _read_mt5_account_from_common_ini(str(data_dir))


def _unique(slug: str, used: set[str]) -> str:
    out, n = slug, 2
    while out in used:
        out = f"{slug}-{n}"
        n += 1
    used.add(out)
    return out


# ── macOS ────────────────────────────────────────────────────────────────────

def detect_macos(home: Optional[str] = None, apps_dir: str = "/Applications") -> list[dict]:
    home_p = Path(home or os.path.expanduser("~"))
    support = home_p / "Library" / "Application Support"
    out: list[dict] = []
    used: set[str] = set()
    seen: set[str] = set()
    try:
        apps = sorted(Path(apps_dir).glob("MetaTrader 5*.app"))
    except OSError:
        apps = []
    for app in apps:
        bid = _bundle_id(app)
        if not bid:
            continue
        prefix = support / bid.lower()
        if not prefix.is_dir():
            prefix = support / bid
        exe = prefix / MAC_TERMINAL_REL
        if not exe.is_file():
            continue
        slug = bid.lower()
        slug = slug[len("net.metaquotes.wine."):] if slug.startswith("net.metaquotes.wine.") else slug
        slug = "wine-mt5-default" if slug == "metatrader5" else slugify(slug)
        server, login = _account(exe.parent)
        out.append({
            "install_id": _unique(slug, used),
            "name": app.stem + (f" ({server})" if server else ""),
            "executable": str(exe),
            "use_wine": True,
            "extra_args": [],
            "broker_name": server,
            "account_login": login,
            "wine_prefix": str(prefix),
            "launch_mode": "app",
            "app_path": str(app),
            "managed": True,
        })
        seen.add(str(prefix).lower())
    try:
        prefixes = sorted(support.glob("net.metaquotes.wine.*"))
    except OSError:
        prefixes = []
    for prefix in prefixes:
        if not prefix.is_dir() or str(prefix).lower() in seen:
            continue
        exe = prefix / MAC_TERMINAL_REL
        if not exe.is_file():
            continue
        slug = slugify(prefix.name.lower()[len("net.metaquotes.wine."):])
        server, login = _account(exe.parent)
        out.append({
            "install_id": _unique(slug, used),
            "name": f"MT5 {slug}" + (f" ({server})" if server else ""),
            "executable": str(exe),
            "use_wine": True,
            "extra_args": [],
            "broker_name": server,
            "account_login": login,
            "wine_prefix": str(prefix),
            "launch_mode": "wine",
            "managed": False,
        })
    return out


# ── Windows ──────────────────────────────────────────────────────────────────

def read_origin(path: Path) -> str:
    """origin.txt holds the install folder, UTF-16LE with a BOM (some builds: UTF-8)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="ignore")
    elif raw.count(b"\x00") > len(raw) // 4:
        text = raw.decode("utf-16-le", errors="ignore")
    else:
        text = raw.decode("utf-8-sig", errors="ignore")
    return text.strip().strip("\x00").strip()


def _wkey(p: str) -> str:
    return p.replace("/", "\\").rstrip("\\").lower()


def windows_data_dirs(appdata: Optional[str]) -> dict[str, Path]:
    """install folder (lower-case, backslashes) → its data folder, from every origin.txt."""
    out: dict[str, Path] = {}
    if not appdata:
        return out
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    try:
        dirs = sorted(root.iterdir())
    except OSError:
        return out
    for d in dirs:
        origin = d / "origin.txt"
        if d.is_dir() and origin.is_file():
            target = read_origin(origin)
            if target:
                out[_wkey(target)] = d
    return out


def _program_files_roots(env: dict) -> list[Path]:
    roots = []
    for key in ("PROGRAMW6432", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        v = env.get(key)
        if v and Path(v) not in roots:
            roots.append(Path(v))
    return roots


def _windows_terminals(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        try:
            tops = sorted(root.iterdir())
        except OSError:
            continue
        for top in tops:
            if not top.is_dir():
                continue
            # "MetaTrader 5", "Exness MetaTrader 5", "MetaTrader 5 IC Markets"...; also one level down
            # ("<Broker>\\MetaTrader 5").
            candidates = [top] if "metatrader" in top.name.lower() else []
            try:
                candidates += [c for c in top.iterdir() if c.is_dir() and "metatrader" in c.name.lower()]
            except OSError:
                pass
            for c in candidates:
                exe = c / "terminal64.exe"
                if exe.is_file() and exe not in found:
                    found.append(exe)
    return found


def detect_windows(env: Optional[dict] = None) -> list[dict]:
    # Windows variable names ignore case, but a dict copy of os.environ holds them UPPER-cased
    # (PROGRAMFILES): look every name up upper-cased.
    env = {k.upper(): v for k, v in (os.environ if env is None else env).items()}
    data_dirs = windows_data_dirs(env.get("APPDATA"))
    out: list[dict] = []
    used: set[str] = set()
    for exe in _windows_terminals(_program_files_roots(env)):
        install_dir = exe.parent
        data = data_dirs.get(_wkey(str(install_dir)))
        if data is None:
            data = install_dir   # portable install, or a terminal never opened yet
        server, login = _account(data)
        out.append({
            "install_id": _unique(slugify(install_dir.name), used),
            "name": install_dir.name + (f" ({server})" if server else ""),
            "executable": str(exe),
            "use_wine": False,
            "extra_args": [],
            "broker_name": server,
            "account_login": login,
            "mql5_dir": str(data / "MQL5"),
            "launch_mode": "native",
            "managed": True,
        })
    return out


def detect(home: Optional[str] = None, apps_dir: Optional[str] = None, env: Optional[dict] = None) -> list[dict]:
    if os.name == "nt":
        return detect_windows(env)
    return detect_macos(home, apps_dir or os.environ.get("LASCODIA_APPS_DIR") or "/Applications")


def merge_installs(existing: list[dict], detected: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep every existing install exactly as configured (an operator's ``managed`` / ids are never
    rewritten on an upgrade); append detected installs whose terminal is not configured yet.
    Returns (merged, added)."""
    def key(row: dict) -> str:
        return _wkey(os.path.expanduser(str(row.get("executable") or "")))
    have = {key(r) for r in existing}
    used = {str(r.get("install_id")) for r in existing}
    added = []
    for row in detected:
        if key(row) in have:
            continue
        row = dict(row)
        row["install_id"] = _unique(str(row["install_id"]), used)
        added.append(row)
    return list(existing) + added, added
