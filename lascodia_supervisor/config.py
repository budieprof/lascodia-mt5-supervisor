"""Load + validate the daemon's YAML config.

Config lives at ``~/.lascodia/supervisor.yaml`` by default; override with
``LASCODIA_SUPERVISOR_CONFIG=/path/to/file.yaml``.  See ``config.example.yaml``
for the full schema with comments.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class MT5Install(BaseModel):
    """One pre-installed MT5 terminal the daemon can launch."""

    install_id: str = Field(..., description="Operator-assigned slug, e.g. 'demo-metaquotes'.")
    name: str = Field(..., description="Human-readable display label.")
    executable: str = Field(..., description="Absolute path or shell command to launch the terminal.")
    extra_args: list[str] = Field(default_factory=list, description="Extra CLI flags for terminal64.exe.")
    broker_name: Optional[str] = None
    account_login: Optional[str] = None
    # If true, prepend a Wine binary to the executable when running on
    # macOS / Linux.  The daemon detects platform and inserts wine
    # automatically — useful for the local dev box where the Mac runs
    # MT5 inside Wine.  See `wine_binary` for which wine gets picked.
    use_wine: bool = False

    # Optional explicit Wine binary path.  When set, overrides the
    # daemon's auto-discovery (PATH lookup → MetaTrader 5.app bundled
    # wine64 → CrossOver).  Use this when you have multiple Wine
    # installs and want to pin one — e.g. CrossOver vs vanilla Wine
    # vs the MT5-bundled wine64.  Empty/None = auto-discover.
    #
    # macOS auto-discovery hits the MetaTrader app bundle first
    # because the official MT5 macOS distribution ships a self-
    # contained `/Applications/MetaTrader 5.app/Contents/SharedSupport
    # /wine/bin/wine64` — using it means we share the same Wine
    # prefix MT5 already populated.
    wine_binary: Optional[str] = None

    # Per-install Wine prefix.  Critical for multi-broker setups
    # where each broker's MT5 lives in its own clone-mt5 bundle
    # with a unique CFBundleIdentifier — the matching prefix is
    # `~/Library/Application Support/<CFBundleIdentifier>/`.  When
    # set, the daemon spawns terminal64.exe with `WINEPREFIX=<this>`
    # so Wine isolates each broker's runtime + single-instance
    # behaviour.  When unset, the daemon auto-derives by walking
    # up from the executable until it finds a `drive_c` directory
    # — the parent of `drive_c` is the prefix.  Without this fix,
    # launching a second broker's MT5 collides on the original
    # prefix and MT5's single-instance check kills the prior
    # terminal.
    wine_prefix: Optional[str] = None


class SupervisorConfig(BaseModel):
    daemon_id: str = Field(..., description="Globally-unique identifier for this daemon.")
    name: str = Field(..., description="Human-readable daemon label.")
    bind_host: str = Field(default="127.0.0.1")
    bind_port: int = Field(default=7770)
    # URL the engine uses to call BACK into this daemon for /terminals/launch
    # etc.  Usually http://<host where daemon runs>:<bind_port>.  Defaults to
    # localhost.
    callback_base_url: str = Field(default="http://127.0.0.1:7770")
    # The engine's API root.  Daemon uses this for register + heartbeat.
    engine_url: str = Field(
        ..., description="Engine base URL, e.g. 'http://localhost:5081/api/v1/lascodia-trading-engine'.")
    # OPTIONAL after v0.2.  Pre-v0.2 builds required an operator to mint
    # a JWT manually and paste it here.  v0.2+ daemons bootstrap their
    # own JWT via POST /admin/terminals/daemons/auth using `daemon_api_key`
    # as the proof-of-identity (TOFU on first contact).  Leave blank for
    # the auto-bootstrap path; only set this if you want to override the
    # auto-issued token with a longer-lived operator-minted one.
    engine_token: Optional[str] = Field(default=None, description="Optional pre-provisioned JWT; daemon auto-bootstraps when blank.")
    # Random secret the daemon generates and sends to the engine on register.
    # Engine uses it as X-Daemon-Auth on every engine→daemon call.  We keep it
    # in config so a restart doesn't invalidate engine-side state — the engine
    # treats re-registration as authoritative anyway.
    daemon_api_key: str = Field(..., min_length=16, description="Shared secret for engine→daemon calls.")
    # Interval between heartbeats (seconds).  Engine treats a daemon as offline
    # when LastSeenAt is older than 3x this.
    heartbeat_interval_sec: int = Field(default=30, ge=5, le=300)
    installs: list[MT5Install] = Field(default_factory=list)


def default_config_path() -> Path:
    return Path(os.path.expanduser("~/.lascodia/supervisor.yaml"))


def _read_mt5_account_from_common_ini(executable_path: str) -> tuple[str, str]:
    """Return (broker_server, account_login) parsed from the install's
    ``config/common.ini`` — or ``("", "")`` when the file is missing
    or unreadable.

    MT5 writes the last-used Login + Server here in plain key=value
    form on every shutdown.  This is the same path the installer
    parses; running the lookup at daemon-load time covers two cases
    the installer can't:
      1. Operator hand-added an install entry to supervisor.yaml
         without filling broker_name / account_login.
      2. MT5 wasn't open when the installer ran, so common.ini didn't
         exist yet.  By the time the daemon next restarts the user
         has typically logged in once and the file exists.
    """
    try:
        install_dir = Path(executable_path).parent
        ini = install_dir / "config" / "common.ini"
        if not ini.exists():
            return ("", "")
        # MT5 writes common.ini as plain ASCII or UTF-16 depending on
        # build; binary read + decode-or-fallback keeps us robust to
        # both.  Lines we care about look like:
        #   Login=5049397557
        #   Server=MetaQuotes-Demo
        raw = ini.read_bytes()
        # Detect UTF-16 (with or without BOM).  MT5's official builds
        # write the file with a BOM (`FF FE`) so a UTF-8 decode fails
        # fast and the UTF-16 path runs.  iconv-produced UTF-16-LE
        # without BOM also exists in tests and ad-hoc setups; the
        # null-byte heuristic catches it because alternating nulls
        # would otherwise validate as UTF-8 single chars and the
        # fallback would never fire.
        if raw[:2] == b"\xff\xfe":
            text = raw.decode("utf-16-le", errors="ignore")
        elif raw[:2] == b"\xfe\xff":
            text = raw.decode("utf-16-be", errors="ignore")
        else:
            # Count null bytes — anything > 5% suggests UTF-16 sans BOM.
            null_ratio = raw.count(b"\x00") / max(1, len(raw))
            if null_ratio > 0.05:
                text = raw.decode("utf-16-le", errors="ignore")
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("utf-16-le", errors="ignore")
        login = ""
        server = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Login=") and not login:
                login = line.removeprefix("Login=").strip()
            elif line.startswith("Server=") and not server:
                server = line.removeprefix("Server=").strip()
            if login and server:
                break
        return (server, login)
    except Exception:
        # Never fail config load over a missing/garbled ini — the
        # daemon must come up even when MT5 is broken.
        return ("", "")


def load_config() -> SupervisorConfig:
    path = Path(os.environ.get("LASCODIA_SUPERVISOR_CONFIG", default_config_path()))
    if not path.exists():
        raise FileNotFoundError(
            f"Daemon config not found at {path}. "
            "Copy config.example.yaml to ~/.lascodia/supervisor.yaml and edit.")
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    cfg = SupervisorConfig(**raw)

    # Backfill broker_name / account_login from each install's
    # common.ini when the operator left them blank.  Runs on every
    # daemon boot so a freshly-launched MT5 (post-installer-run) gets
    # its identity picked up on the next restart without forcing a
    # re-run of install.sh.
    for install in cfg.installs:
        if install.broker_name and install.account_login:
            continue
        server, login = _read_mt5_account_from_common_ini(install.executable)
        if not install.broker_name and server:
            install.broker_name = server
        if not install.account_login and login:
            install.account_login = login

    return cfg
