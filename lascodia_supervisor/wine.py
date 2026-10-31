"""Wine-mode launch helpers (``launch_mode: wine``) — moved out of process_manager.py unchanged
so the terminal controller and the legacy process manager share them."""
from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lascodia.supervisor.process")



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



def build_wine_env(install) -> dict:
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


def build_wine_command(install) -> list[str]:
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
