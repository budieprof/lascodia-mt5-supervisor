"""Load + validate the daemon's YAML config.

Config lives at ``~/.lascodia/supervisor.yaml`` by default; override with
``LASCODIA_SUPERVISOR_CONFIG=/path/to/file.yaml``.  See ``config.example.yaml``
for the full schema with comments.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

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

    # The terminal's MQL5 data directory, where the daemon drops an EA enrollment token
    # (Files/Lascodia/enrollment_token.txt) before launching it.  Unset = the MQL5 folder next
    # to terminal64.exe (portable installs and the macOS Wine layout).  Set it for a Windows
    # install whose data lives under %APPDATA%\MetaQuotes\Terminal\<hash>\MQL5.
    mql5_dir: Optional[str] = None

    # ── Self-heal (2026-10-31) ─────────────────────────────────────────
    # How the terminal is started.  ``app`` = ``open -a <app_path>`` — the macOS MetaTrader
    # wrapper app, which is how the live terminals run (and the only way builds >= 6290 start
    # under the Wine 11 wrapper).  ``wine`` = spawn ``wine64 terminal64.exe`` directly (cloned
    # installs, Linux).  Unset = ``app`` when an app bundle is configured or found, else ``wine``.
    # ``native`` = Windows: start terminal64.exe directly in the user's session (unset on Windows
    # means native).
    launch_mode: Optional[Literal["app", "wine", "native"]] = None
    # The wrapper app bundle, e.g. "/Applications/MetaTrader 5 - exness.app".  Unset = the
    # /Applications/MetaTrader 5*.app whose CFBundleIdentifier names this install's prefix.
    app_path: Optional[str] = None
    # True = the supervisor may keep this terminal running on its own (watchdog) before the
    # engine has ever said what it wants.  False (default for hand-added entries) = hands off
    # until the engine sends a desired state with autoHeal on.
    managed: bool = False
    # A live terminal whose every EA beacon stopped ticking this long ago counts as wedged.
    wedge_after_sec: int = Field(default=300, ge=60, le=3600)


class OpsConfig(BaseModel):
    """Engine-host operations (gaps program, zero-downtime): the console's Operations → Platform host jobs —
    status, back up now, restore drill, blue/green deploy.  Only the supervisor on the ENGINE host enables this
    (and the engine sends host jobs only to the daemon named in its EngineConfig Ops:HostDaemonId)."""
    enabled: bool = False
    # The live engine checkout (docker-compose.yml, scripts/redeploy-api-bluegreen.sh, scripts/backup/).
    engine_repo: Optional[str] = None
    # Blue/green deploys from the console run only when this is ALSO true (defence in depth beside the engine's
    # Ops:ConsoleDeploy:Enabled, default false).
    allow_deploy: bool = False
    backup_dir: Optional[str] = None            # default ~/LascodiaBackups
    bluegreen_state_dir: Optional[str] = None   # default ~/.lascodia/bluegreen (the swap script's STATE_DIR)
    caddyfile: str = "/opt/homebrew/etc/Caddyfile"
    compose_project: str = "lascodia-trading-engine"
    docker: str = "docker"
    deploy_timeout_sec: int = Field(default=2400, ge=60, le=4 * 3600)
    backup_timeout_sec: int = Field(default=6 * 3600, ge=60, le=24 * 3600)


class WatchdogConfig(BaseModel):
    """Local auto-restart limits.  They mirror the engine's EA:AutoHeal limits (C8) so a host
    cut off from the engine still behaves the same way."""
    enabled: bool = True
    # Waits between consecutive failed start attempts; the last one repeats.
    start_backoff_sec: list[int] = Field(default_factory=lambda: [30, 120, 300, 900])
    max_restarts_per_hour: int = Field(default=3, ge=1)
    max_restarts_per_day: int = Field(default=8, ge=1)
    # After the limits are hit (gave_up): one more try this often.
    gave_up_retry_sec: int = Field(default=3600, ge=300)
    # No wedge decision this soon after a launch or a host wake-up.
    launch_grace_sec: int = Field(default=300, ge=30)
    wake_grace_sec: int = Field(default=300, ge=0)
    # A beacon saying init_failed after this many launches in a row = bad inputs: stop trying.
    init_failed_launches: int = Field(default=3, ge=1)
    # Journal corruption signals restart a terminal at most this often.
    journal_restart_min_interval_sec: int = Field(default=3600, ge=600)
    # Seconds to wait for each process to exit after SIGTERM before SIGKILL.
    stop_grace_sec: int = Field(default=20, ge=1, le=120)


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
    # How the engine hostname is resolved.  "system" = the OS resolver (honours /etc/hosts).
    # "public" = DNS-over-HTTPS, ignoring /etc/hosts — for a host whose /etc/hosts pins the
    # engine name to a LAN address that macOS Local Network privacy blocks for launchd agents
    # (symptom: every engine call fails with "[Errno 65] No route to host").  See public_dns.py.
    # "auto" (default) = the system resolver, switching to "public" for the rest of the process
    # when a connection to a private / link-local address fails with no-route / not-permitted.
    engine_resolver: Literal["auto", "system", "public"] = Field(default="auto")
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
    # Multi-tenancy Phase 5 (EA ADR-0017): a single-use Daemon enrollment token ("Connect a
    # supervisor" in the console, lsc_enr_…).  Used once, only while no machine credential is
    # stored; the credential it is exchanged for signs the daemon in from then on (and lets it
    # hand terminals it launches their own EA enrollment tokens).  Without it the daemon signs
    # in with daemon_api_key for as long as the engine allows that.
    enrollment_token: Optional[str] = Field(default=None, description="Single-use Daemon enrollment token (lsc_enr_…).")
    # Where the machine credential is kept (mode 0600).  Default: machine_credential.json next
    # to supervisor.yaml.
    credential_path: Optional[str] = Field(default=None, description="Machine credential file (0600).")
    # Interval between heartbeats (seconds).  Engine treats a daemon as offline
    # when LastSeenAt is older than 3x this.
    heartbeat_interval_sec: int = Field(default=30, ge=5, le=300)
    installs: list[MT5Install] = Field(default_factory=list)

    # ── Self-heal (2026-10-31) ─────────────────────────────────────────
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    # Pull job channel (C4): long-poll the engine for StartTerminal / DeployEaBinary / ... jobs.
    # Works from behind NAT — the engine never needs to reach this host.
    jobs_enabled: bool = True
    jobs_wait_sec: int = Field(default=20, ge=1, le=60)
    # Engine-host operations (gaps program, zero-downtime) — off unless this supervisor runs on the engine host.
    ops: Optional[OpsConfig] = None
    # Where sessions, watchdog history and the engine's desired state are kept across restarts.
    # Default: <config dir>/state.
    state_dir: Optional[str] = None
    # The supervisor's own rotating log (supervisor.log).  Default: <install root>/logs, the
    # directory launchd's stdout/stderr files live in.
    log_dir: Optional[str] = None
    log_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024 * 1024)
    log_backup_count: int = Field(default=5, ge=1, le=50)


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
        install_dir = Path(executable_path)
        if install_dir.suffix.lower() == ".exe":
            install_dir = install_dir.parent
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
    if not cfg.credential_path:
        cfg.credential_path = str(path.parent / "machine_credential.json")
    if not cfg.state_dir:
        cfg.state_dir = str(path.parent / "state")
    if not cfg.log_dir:
        # install.sh layout: <root>/config/supervisor.yaml + <root>/logs/
        cfg.log_dir = str(path.parent.parent / "logs")

    # Backfill broker_name / account_login from each install's
    # common.ini when the operator left them blank.  Runs on every
    # daemon boot so a freshly-launched MT5 (post-installer-run) gets
    # its identity picked up on the next restart without forcing a
    # re-run of install.sh.
    for install in cfg.installs:
        if install.broker_name and install.account_login:
            continue
        # The data folder holds config/common.ini (next to terminal64.exe, or the Windows
        # %APPDATA% folder mql5_dir points into).
        data = Path(os.path.expanduser(install.mql5_dir)).parent if install.mql5_dir else Path(install.executable).parent
        server, login = _read_mt5_account_from_common_ini(str(data))
        if not install.broker_name and server:
            install.broker_name = server
        if not install.account_login and login:
            install.account_login = login

    return cfg
