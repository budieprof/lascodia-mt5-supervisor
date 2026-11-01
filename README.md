# Lascodia MT5 Supervisor

Sidecar daemon for the Lascodia Trading Engine. Manages MT5 terminal
lifecycles per broker account so the engine's admin UI can launch,
monitor, and close MT5 instances remotely — across multiple broker
logins, without VNCing into the host that runs MT5.

This complements Phase-11 (`CMD_LAUNCH_INSTANCE`), which spawns sibling
EA instances **within one MT5 terminal** (same broker account). The
supervisor is the path for **cross-terminal** spawning — i.e. one EA
per broker account, which MT5 binds at the terminal-process level.

---

## Install on a computer (one line)

This is how an organisation connects a computer that runs MetaTrader 5. No git, no Python, no
flags, no config files.

1. In the console, go to **Terminals → Add a computer** and pick **macOS** or **Windows**.
2. Copy the one line it shows and paste it into **Terminal** (macOS) or **PowerShell** (Windows):

   ```sh
   # macOS
   curl -fsSL https://app.codiapay.com/api/v1/lascodia-trading-engine/onboarding/supervisor/install.sh | sh -s -- lsc_enr_xxxxxxxx_...
   ```
   ```powershell
   # Windows (Windows PowerShell 5.1 or PowerShell 7; no administrator needed)
   & ([scriptblock]::Create((irm https://app.codiapay.com/api/v1/lascodia-trading-engine/onboarding/supervisor/install.ps1))) lsc_enr_xxxxxxxx_...
   ```
3. It prints `Connected: 3 terminals found. Go back to the console to finish.` The console page
   flips from "Waiting for your computer…" to "Connected" and walks the last manual step per
   terminal (adding the engine address to MetaTrader's WebRequest list; MetaTrader offers no
   supported way to set it from outside).

The `lsc_enr_…` code in the line is a single-use Daemon enrollment code, bound to your
organisation. It downloads the supervisor and enrolls this computer once. After that, the computer
signs in with its own machine credential.

**What the line does**
- **Checks the platform and downloads the bundle.** The bundle is `macos-arm64`,
  `macos-x86_64` or `windows-x64`, fetched from
  `onboarding/supervisor/bundle?platform=…`. The install code goes in the
  `X-Enrollment-Token` header, so it never appears in the process list. The script then
  checks the download's sha256 against the engine's `X-Bundle-Sha256`.
- **Installs the bundle.** A bundle contains its own CPython 3.12
  (python-build-standalone), the pinned dependencies, the supervisor itself, `VERSION`
  and `MANIFEST.sha256`. Nothing is installed system-wide. Before switching to a new bundle,
  the script runs the bundled Python and checks the manifest.
  - macOS installs into `~/Library/Application Support/Lascodia/supervisor`, for this user
    only.
  - Windows installs into `%ProgramData%\Lascodia\Supervisor`. The `config` folder there is
    locked down so only this user, SYSTEM and Administrators can read it.
- **Finds every MetaTrader 5 install.**
  - macOS: `/Applications/MetaTrader 5*.app` together with its Wine prefix. These are
    `launch_mode: app`, `managed: true`. A Wine prefix with no app is added as
    `managed: false`.
  - Windows: `terminal64.exe` under `Program Files\*MetaTrader*`, mapped to its data folder
    via `%APPDATA%\MetaQuotes\Terminal\<hash>\origin.txt`. These are
    `launch_mode: native`, `managed: true`.
  - Preview what would be found, without changing anything:
    `<root>/current/python/bin/python3 -m lascodia_supervisor detect-installs`.
- **Writes `config/supervisor.yaml`.** It contains:
  - `daemon_id` = the hostname plus 4 random characters;
  - `engine_resolver: auto`;
  - `bind_host: 127.0.0.1` (the job channel is pull, so no inbound access is needed);
  - a free port starting at 7770;
  - every detected terminal.
- **Starts the supervisor.**
  - macOS: the LaunchAgent `com.lascodia.supervisor` (KeepAlive, `AbandonProcessGroup`, so
    terminals survive a supervisor restart) runs `bin/run.sh`.
  - Windows: the Scheduled Task **Lascodia Supervisor**, at logon of the installing user, runs
    `bin\run.ps1`. MetaTrader is a desktop app and must run in that user's session. If the
    computer forbids a normal user from adding a logon task, it falls back to the user's Run key.
- **Waits up to 60 s for registration.** If the supervisor has not registered with the engine
  by then, the script prints exactly what failed (code refused, engine unreachable, …).

**Pasting it again** upgrades in place. It keeps:
- the computer's identity: `daemon_id`, the daemon key and the machine credential;
- the engine address;
- every configured terminal, including `managed: false` set by hand.

It only adds newly found terminals. If nothing changed, the config file is not rewritten.

**Uninstall:** run the same line with `--uninstall` in place of the code. This removes the
supervisor, its settings and its credential. It never touches the terminals. To connect the
computer again, use a fresh line from the console.

### Updates

The supervisor keeps itself up to date:

1. Every heartbeat sends `supervisorVersion` and `platform`.
2. When a newer release for the platform exists and `Supervisor:AutoUpdate` is on, the engine
   answers with `supervisorUpdate`.
3. The supervisor installs the update, but never during a job or a terminal start/stop. It:
   - downloads the release with its own token and verifies the sha256 and the manifest;
   - smoke-tests the new Python;
   - unpacks it into `versions/<v>/`;
   - points `current` at it (a symlink on macOS, `current.txt` on Windows);
   - exits, so launchd or the launcher loop starts the new version.
4. The new version must register within 120 s. If it does not:
   - if it is running, it switches back itself;
   - if it cannot even start, `bin/run.sh` / `run.ps1` switches back on the next start.

   The rolled-back version is written to `update.bad` and never tried again. The three newest
   versions are kept.

A developer checkout (`pip install -e`, like the engine laptop's own supervisor) is never
self-updated.

### Building and publishing bundles (maintainers)

```bash
make bundle               # dist/bundles/lascodia-supervisor-<v>-{macos-arm64,macos-x86_64}.tar.gz + -windows-x64.zip
make e2e                  # install.sh + the built bundle against a stub engine, temp HOME, fake launchctl
LASCODIA_ADMIN_TOKEN=… make publish-bundle     # POST platform/supervisor/releases (LASCODIA_API_BASE optional)
make publish-bundle-dry   # show what would be uploaded
```

Bump `__version__` (`lascodia_supervisor/__init__.py`) and `pyproject.toml` together. Running
supervisors pick up the new release on their next heartbeat after it is published.

The bootstrap scripts are `bootstrap/install.sh` (POSIX sh) and `bootstrap/install.ps1` (ASCII,
PowerShell 5.1 compatible). The engine serves them publicly, with its own API base substituted
for `__LASCODIA_API_BASE__`.

### Runtime layout (bundle installs)

```
<root>/versions/<v>/      unpacked bundles (python/, launcher/, bootstrap/, VERSION, MANIFEST.sha256)
<root>/current            -> versions/<v>   (Windows: current.txt)
<root>/bin/run.sh|run.ps1 the launcher (rollback check, then current's python -I -m lascodia_supervisor run)
<root>/config/            supervisor.yaml, machine_credential.json, state/ (status.json, sessions, watchdog)
<root>/logs/              supervisor.log (rotating), launcher.log, stdout/stderr
<root>/update.pending|.bad|.last|.rolledback
```

`python -m lascodia_supervisor` subcommands: `run`, `version`, `platform`, `detect-installs`,
`setup`, `wait-registered`, `verify-bundle`.

### `engine_resolver`

The default is `auto`: the system resolver, which honours `/etc/hosts`. It switches to `public`
for the rest of the process in one case only: a connection to a private or link-local address
fails with `EHOSTUNREACH`, `ENETUNREACH` or `EPERM`. That is macOS 15 *Local Network* privacy
blocking a launchd agent's connection to a LAN pin. The switch is logged once. Failures against
a public address never switch. `system` and `public` force one behaviour.

---

## Table of contents

- [Install on a computer (one line)](#install-on-a-computer-one-line)
- [Architecture](#architecture)
- [Self-healing (watchdog, job channel, beacons)](#self-healing-watchdog-job-channel-beacons)
- [Advanced: install from a checkout (mac2 recipe)](#advanced-install-from-a-checkout-mac2-recipe)
- [Advanced: developer quickstart (macOS)](#advanced-developer-quickstart-macos)
- [`install.sh` reference](#installsh-reference)
- [Configuration reference](#configuration-reference)
- [HTTP API](#http-api)
- [Operational flows](#operational-flows)
  - [Multi-broker isolation (macOS)](#multi-broker-isolation-macos)
  - [Orphan-process cleanup](#orphan-process-cleanup)
- [File layout & runtime state](#file-layout--runtime-state)
- [Security model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Smoke test commands](#smoke-test-commands)

---

## Architecture

```
┌─────────────────┐                                ┌──────────────────────┐
│  Lascodia       │                                │  lascodia-mt5-       │
│  engine         │  ─── (anonymous + apiKey) ──►  │  supervisor          │
│  (.NET API)     │      POST /daemons/auth        │  (FastAPI, this repo)│
│                 │  ◄── { token, expiresAt } ───  │                      │
│                 │                                │                      │
│                 │  ◄── register, heartbeat ────  │                      │
│                 │      (Bearer JWT)              │                      │
│                 │                                │                      │
│                 │  ── launch / close ──────────► │                      │
│                 │      (X-Daemon-Auth)           │                      │
└─────────────────┘                                └──────────┬───────────┘
                                                              │ spawns
                                                              ▼ (Wine on macOS,
                                                                native on Windows)
                                                  ┌──────────────────────┐
                                                  │  MT5 terminal #1     │
                                                  │  (broker account A)  │
                                                  │  + Lascodia EA       │
                                                  ├──────────────────────┤
                                                  │  MT5 terminal #2     │
                                                  │  (broker account B)  │
                                                  │  + Lascodia EA       │
                                                  └──────────────────────┘
```

**Components:**

- **Daemon** — Python FastAPI process, one per host. Bound to
  `127.0.0.1:7770` by default (loopback) or to a routable address when
  the engine and daemon live on different machines.
- **`ProcessManager`** — spawns `terminal64.exe` (through `wine` on
  macOS / Linux), tracks PIDs, reconciles state via `psutil` on each
  heartbeat.
- **`EngineClient`** — handles auth + register + heartbeat to the
  engine. Auto-bootstraps its JWT on first use; retries on 401.
- **LaunchAgent** (`com.lascodia.supervisor.plist`) — launchd plist on
  macOS that runs the daemon at user login and restarts it on crash.

**Authority model:**

- The **EA** is authoritative for its own runtime symbol ownership.
- The **engine** is authoritative for which daemon owns which
  `DaemonId` (pinned on first auth, which must come from a trusted
  network or carry the bootstrap token — see [Security model](#security-model)).
- The **daemon** is authoritative for which MT5 process is running
  on its host — the engine never spawns terminals directly.

The daemon does **not** manage broker credentials. Each MT5 install
saves its own login state via *Tools → Options → Server → Save account
information*. The supervisor's job is "start / stop / report on MT5
processes."

---

## Self-healing (watchdog, job channel, beacons)

The supervisor is the hands of the self-healing system: the only component on the host that can
relaunch a dead or wedged terminal, deploy a new EA binary and read local logs.  Contracts C2,
C4, C6 and C8 of the 2026-10-31 self-heal program.

**Adoption.**  Every heartbeat the supervisor lists the host's processes, finds every
`terminal64.exe` (psutil names it `wine`; its command line is the Windows path), and maps it to an
install by Wine prefix — `WINEPREFIX` when readable, else the files it holds open (`lsof`), else
the wrapper app it was started from.  A terminal the supervisor did not start (the hand-launched
live ones) is **adopted**: tracked as a session (`adopted: true`), restartable, reported.  Sessions
are persisted in `state/sessions.json`, so ids survive a supervisor restart.

**Start / stop.**  `app` mode starts with `open -a <app_path>`.  Stop = SIGTERM `terminal64` →
wait → SIGKILL; SIGTERM the wrapper (`<app>/Contents/MacOS/MetaTrader 5`) → wait → SIGKILL; then the
prefix's `wineserver` if it lingers.  Killing only terminal64 leaves the wrapper behind, and
`open -a` then launches nothing — so a start also clears a terminal-less wrapper first.

**Watchdog** (`runtime.watchdog` in the heartbeat).  Obeys the engine's per-install desired state
and auto-heal flag (heartbeat response; persisted in `state/desired_state.json`; never known ⇒
`Running` + auto-heal for `managed: true`, else hands off):

| Situation | Action |
|---|---|
| desired Running, auto-heal on, terminal absent | start (backoff 30 s / 2 m / 5 m / 15 m) |
| terminal alive, every EA beacon older than `wedge_after_sec` | restart — not within 5 min of a launch or a wake from sleep; never on "no beacons" |
| `init_failed` beacon after 3 launches in a row | `gave_up` (bad inputs); cleared by an operator start/restart |
| journal: `old tick` stamped before 2000, bulk `invalid book item`, `debugger has been found` | restart, at most once per hour |
| > 3 automatic restarts in an hour or > 8 in a day | `gave_up`, one retry per hour |
| desired Stopped | never start; stop once when the engine *changes* it to Stopped |
| auto-heal off | nothing at all |

A restart only ever happens when the EA is already not working; open positions keep their
broker-side SL/TP meanwhile.

**Beacons** (C2).  Each EA writes `MQL5/Files/Lascodia/beacon/<instanceId>.json` every ≤ 15 s
(works before sign-in).  The supervisor ships them verbatim in `runtime.beacons`.

**Job channel** (C4).  The supervisor long-polls `GET admin/terminals/daemons/jobs?waitSec=20`
(it also sends `daemonId=` for engines that key on it) and reports
`POST admin/terminals/daemons/jobs/{jobId}/result {status, message, data}`.  Pull, not push: the
engine never needs to reach this host, so hosts behind NAT work and `bind_host` can stay
`127.0.0.1`.  Jobs: `StartTerminal`, `StopTerminal`, `RestartTerminal`,
`DeployEaBinary {releaseId, sha256}` (downloads `platform/ea/releases/{id}/binary`, verifies the
sha256, writes `MQL5/Experts/LascodiaEA.ex5` via a temp file + rename, keeps `LascodiaEA.ex5.prev`,
returns `previousSha256` + `sha256`), `TailLog {kind: ea|journal|supervisor, instanceId?, lines ≤ 2000}`
(UTF-16LE decoded, ≤ 1 MB), `PruneFiles {dryRun}` (dry run unless `dryRun: false`).

**Prune** (C6).  Terminal `logs/` and `MQL5/Logs/` older than 14 days, EA `bridge_*.log` /
`lascodia_*_YYYYMMDD.log` older than 14 days, and every artifact of an EA instance whose files have
not changed for 30 days.  Never: the newest file of any kind, `config_override_*`, `apikey_*`,
`machine_*`, `*.seq`, `*_state.json`, templates, profiles, `outbox/`, `retry_queue/`, `recovery/`,
`deadletter/`, `config/`.

**Logs.**  `logs/supervisor.log`, rotating 20 MB × 5.  launchd's `stdout.log` / `stderr.log` now
only catch crashes before logging starts, and are trimmed at start-up when over 20 MB.

**WebRequest allowlist.**  MT5's documented `/config:` startup file has no key for the
WebRequest allowlist (`[Experts]` knows only `AllowLiveTrading`, `AllowDllImport`, `Enabled`,
`Account`, `Profile`); forum reports say `WebRequest=` / `WebRequestUrl=` there are ignored.  So the
supervisor does not try to seed it: add the engine URL once per prefix in Tools → Options →
Expert Advisors (or clone a prefix that already has it).  The EA reports error 4014 in its
heartbeat `diag.lastWebRequestError` when it is missing.

## Advanced: install from a checkout (mac2 recipe)

The [one-line install](#install-on-a-computer-one-line) replaces this for everyone else. On the second Mac, with the repo checked out and the MetaTrader apps installed and signed in once:

```bash
cd ~/lascodia-mt5-supervisor
./install.sh --non-interactive \
  --daemon-id mac2-mac \
  --daemon-name "mac2 (Wine)" \
  --engine-url "http://<laptop LAN IP>:5081/api/v1/lascodia-trading-engine" \
  --enrollment-token "lsc_enr_xxxxxxxx_<40 chars>"     # console → Terminals → Connect a supervisor
```

That auto-detects every `/Applications/MetaTrader 5*.app` and its prefix
(`~/Library/Application Support/<bundle id>`) as an install with `launch_mode: app` and
`managed: true`, keeps `bind_host: 127.0.0.1` (the pull job channel needs no inbound
connection), writes `supervisor.yaml`, installs the LaunchAgent and health-checks it.  Preview the
installs first with `./install.sh --print-detected-installs` (changes nothing).  On an existing
host add `--reconfigure` (the daemon key is preserved).  `managed: true` means the watchdog starts
an absent terminal as soon as the daemon runs, before the engine has sent any desired state — set
`managed: false` on an install that must stay down.

**If every engine call fails with `[Errno 65] No route to host`** while `curl` over SSH works, the
host's `/etc/hosts` (or local DNS) points the engine name at a LAN address, and macOS 15 *Local
Network* privacy blocks LAN connections from a launchd agent until someone clicks Allow on that
Mac's screen. The default `engine_resolver: auto` now handles this by itself; `engine_resolver: public` forces it: the supervisor then
resolves the engine through public DNS-over-HTTPS and connects over the internet (TLS still
verified against the real name), leaving the LAN pin in place for the EAs. mac2 runs this way.

## Advanced: developer quickstart (macOS)

For working on the supervisor itself (editable install, never self-updated).

```bash
cd lascodia-mt5-supervisor
./install.sh
```

That single command:

1. Picks the newest available Python ≥ 3.11 (Homebrew preferred).
2. Creates a venv at `~/Library/Application Support/Lascodia/supervisor/.venv`.
3. `pip install -e .` from this repo.
4. Prompts for engine URL, daemon name/id, and auto-generates a
   `daemon_api_key`. Auto-detects Wine MT5 at the standard
   `net.metaquotes.wine.metatrader5/.../terminal64.exe` path and seeds
   a default install entry, auto-filling `broker_name` and
   `account_login` from `config/common.ini` if MT5 has been launched
   once.
5. Writes a launchd LaunchAgent that auto-starts at login and restarts
   on crash.
6. Bootstraps the agent, polls `/healthz` until the daemon is up.
7. Prints log-tail commands.

The daemon **auto-registers** with the engine on first start. No
manual JWT minting required — the daemon exchanges its
`daemon_api_key` for a JWT via the engine's anonymous
`POST /admin/terminals/daemons/auth` endpoint. The first daemon to
claim a given `DaemonId` permanently owns it (trust-on-first-use); see
[Security model](#security-model).

After install you should see:

- `launchctl print gui/$(id -u)/com.lascodia.supervisor` → state running.
- `curl http://127.0.0.1:7770/healthz` → `{"ok":true,...}`.
- Engine admin UI **Terminals** page → your daemon listed online.

---

## `install.sh` reference

All flags are macOS-only (the daemon also runs on Linux + Windows but
the installer is launchd-specific). The script is idempotent —
re-running it is safe.

| Flag | Effect |
|------|--------|
| *(no flags)* | Fresh install. Picks Python, creates venv, prompts for config, installs LaunchAgent, starts daemon. |
| `--reconfigure` | Overwrite an existing `supervisor.yaml`. Preserves `daemon_api_key` by default (TOFU-safe). |
| `--rotate-api-key` | Used with `--reconfigure` — generate a fresh `daemon_api_key`. **Requires** you to delete the engine-side `TerminalDaemon` row first; otherwise the daemon will be locked out of `/auth`. |
| `--add-install PATH` | Append a new MT5 install entry. `PATH` is the absolute path to `terminal64.exe` of the install to add. Autofills `broker_name` + `account_login` from `<PATH dir>/config/common.ini`. Kickstarts the daemon so it re-registers with the new install advertised. |
| `--install-id ID` | With `--add-install`, override the auto-derived slug (default: lower-cased broker name). |
| `--install-name N` | With `--add-install`, override the human-readable label. |
| `--clone-mt5 SLUG` | Clone `/Applications/MetaTrader 5.app` to a sibling bundle with a unique `CFBundleIdentifier` so macOS provisions a separate Wine prefix. Eliminates the shared-prefix bugs on macOS multi-broker setups. Bundle ends up at `/Applications/MetaTrader 5 - <slug>.app`, prefix at `~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>/`. Manual MT5 first-launch + broker login follow; then `--add-install` registers the new install with the daemon. |
| `--non-interactive` | Skip prompts, use defaults. Useful for unattended bootstraps; pair with `--reconfigure` later to set real values. |
| `--skip-health` | Skip the post-install `/healthz` poll. Use when the engine isn't reachable yet. |
| `--uninstall` | `launchctl bootout` the agent and remove the install directory. Set `KEEP_CONFIG=1` in the env to keep `config/` + `logs/` and only nuke the venv (faster re-install path). |
| `-h`, `--help` | Print usage summary. |

**Examples:**

```bash
# Fresh install with interactive prompts.
./install.sh

# Add a second broker after the fact.
./install.sh --add-install "/path/to/ICMarkets/MetaTrader 5/terminal64.exe"

# Add with custom slug + name.
./install.sh --add-install /path/to/terminal64.exe \
             --install-id ic-markets-prod \
             --install-name "IC Markets Production"

# Rotate the API key (after deleting the engine row).
./install.sh --reconfigure --rotate-api-key

# Drop daemon entirely.
./install.sh --uninstall

# Drop daemon, keep config + logs.
KEEP_CONFIG=1 ./install.sh --uninstall
```

---

## Configuration reference

`supervisor.yaml` lives at
`~/Library/Application Support/Lascodia/supervisor/config/supervisor.yaml`
(mode 600) on macOS, or `~/.lascodia/supervisor.yaml` for manual
installs. Override with the `LASCODIA_SUPERVISOR_CONFIG` env var.

### Top-level fields

| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| `daemon_id` | yes | — | Globally-unique identifier. TOFU-pinned on the engine side. |
| `name` | yes | — | Human-readable label shown on the admin UI Terminals page. |
| `bind_host` | no | `127.0.0.1` | Where the daemon's FastAPI server binds. |
| `bind_port` | no | `7770` | Daemon's HTTP port. |
| `callback_base_url` | no | `http://<bind_host>:<bind_port>` | URL the engine uses to call back into the daemon for launch/close. For cross-host setups, point at the daemon's externally-reachable address. |
| `engine_url` | yes | — | Engine API root, e.g. `http://localhost:5081/api/v1/lascodia-trading-engine`. |
| `engine_token` | no | `""` | Pre-provisioned JWT. Leave blank for auto-bootstrap (recommended). Only set when you want a longer-lived operator-minted token to override the auto-issued one. |
| `daemon_api_key` | yes | — | 32+ random chars. Proof-of-identity for `/daemons/auth` (legacy sign-in) and the `X-Daemon-Auth` secret the engine sends back. TOFU-pinned on the engine. |
| `enrollment_token` | no | `""` | Single-use Daemon enrollment token (`lsc_enr_…`, 24 h) from the console. Exchanged once for the daemon's machine credential — see [Machine credentials](#machine-credentials-enrollment). |
| `credential_path` | no | `machine_credential.json` next to `supervisor.yaml` | Where the machine credential is kept (mode 0600). |
| `heartbeat_interval_sec` | no | `30` | Daemon→engine heartbeat cadence. Min 5, max 300. Also the watchdog's evaluation cadence. |
| `installs` | no | `[]` | List of MT5 installs this daemon can launch. |
| `watchdog.enabled` | no | `true` | Master switch for the local watchdog. |
| `watchdog.start_backoff_sec` | no | `[30, 120, 300, 900]` | Waits between failed start attempts (last one repeats). |
| `watchdog.max_restarts_per_hour` / `_per_day` | no | `3` / `8` | Automatic starts + restarts per install; then `gave_up`, one retry per `gave_up_retry_sec` (3600). Operator jobs never count. |
| `watchdog.launch_grace_sec` / `wake_grace_sec` | no | `300` / `300` | No wedge decision this soon after a launch / after the Mac wakes from sleep. |
| `watchdog.init_failed_launches` | no | `3` | An `init_failed` beacon after this many launches in a row = bad inputs: stop restarting. |
| `watchdog.journal_restart_min_interval_sec` | no | `3600` | Journal corruption restarts at most this often. |
| `watchdog.stop_grace_sec` | no | `20` | SIGTERM → SIGKILL grace for terminal64. |
| `jobs_enabled` / `jobs_wait_sec` | no | `true` / `20` | Pull job channel (long-poll). |
| `state_dir` | no | `<config dir>/state` | `sessions.json`, `watchdog.json`, `desired_state.json`. |
| `log_dir` / `log_max_bytes` / `log_backup_count` | no | `<install root>/logs` / 20 MB / 5 | Rotating `supervisor.log`. |

### Per-install fields (`installs[]`)

| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| `install_id` | yes | — | Operator-assigned slug, e.g. `ic-markets-prod`. Used by the engine's launch endpoint. Unique within this daemon. |
| `name` | yes | — | Human-readable label. |
| `executable` | yes | — | Absolute path to `terminal64.exe`. |
| `use_wine` | no | `false` | If `true`, daemon prepends `wine` when spawning. Auto-set to `true` for macOS Wine paths. |
| `extra_args` | no | `[]` | Extra CLI flags, e.g. `["/portable"]`. |
| `wine_binary` | no | auto | Explicit Wine binary path. Overrides auto-discovery (`PATH` → `MetaTrader 5.app`-bundled `wine64` → CrossOver → Homebrew). |
| `wine_prefix` | no | derived | Wine prefix root. Auto-derived at register time by walking up from `executable` to the parent of `drive_c`. **Critical for multi-broker isolation** — see [Multi-broker isolation](#multi-broker-isolation-macos). |
| `broker_name` | no | autofill | Empty values are backfilled from `<executable dir>/config/common.ini`'s `Server=` field at daemon startup. |
| `account_login` | no | autofill | Empty values are backfilled from `Login=` in the same file. |
| `mql5_dir` | no | `<executable dir>/MQL5` | The terminal's MQL5 data folder, where an enrolled daemon drops the EA enrollment token before a launch. Set it for a Windows install whose data lives under `%APPDATA%`. |
| `launch_mode` | no | Windows: `native`; else `app` if a wrapper app is found, else `wine` | `app` = `open -a <app_path>` (how the live terminals run; client builds ≥ 6290 need the Wine 11 wrapper app). `wine` = spawn `wine64 terminal64.exe` directly. `native` = Windows: start `terminal64.exe` directly in the user's session; stop = close request (`taskkill /PID`) → kill. |
| `app_path` | no | auto | The wrapper app, e.g. `/Applications/MetaTrader 5 - exness.app`. Auto = the `/Applications/MetaTrader 5*.app` whose `CFBundleIdentifier` is the prefix's directory name. |
| `managed` | no | `false` | `true` = keep this terminal running (watchdog) even before the engine has sent a desired state. `false` = hands off until the engine says Running + auto-heal on. `install.sh` writes `true` for every auto-detected install. |
| `wedge_after_sec` | no | `300` | A running terminal whose every EA beacon stopped ticking this long ago counts as wedged. |

### Autofill cascade

When `broker_name` or `account_login` is left blank, the daemon
populates it at **load time** by reading `<install dir>/config/common.ini`.
The installer does the same at write time for the auto-detected
Wine MT5 install. Order:

1. **Operator override** — explicit non-empty value in `supervisor.yaml` always wins.
2. **Installer autofill** — `parse_mt5_account()` runs during `--add-install` or `--reconfigure`.
3. **Daemon runtime autofill** — `_read_mt5_account_from_common_ini()` runs on every `load_config()`, backfilling blanks.

Net effect: launch MT5 once manually, sign in, close it — every
subsequent daemon restart picks up the broker + login without any
operator action.

---

## HTTP API

### Daemon endpoints (engine → daemon)

The engine calls these on the operator's behalf when an operator
clicks buttons on the admin UI Terminals page. All except `/healthz`
require an `X-Daemon-Auth` header carrying `daemon_api_key`.

**Runtime — sessions:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/healthz` | Liveness probe. Returns `{ok, daemonId, sessions}`. |
| `GET` | `/installs` | List advertised installs (engine-facing — omits `wine_binary` / `wine_prefix`). |
| `GET` | `/terminals` | List currently-tracked sessions (`installId`, `pid`, `status`, timestamps). |
| `POST` | `/terminals/launch` | Spawn an install. Body: `{installId, extraArgs?}`. Returns the new session. Daemon sets `WINEPREFIX` per `wine_prefix` (or auto-derives) so each broker's MT5 runs in its own prefix. |
| `POST` | `/terminals/{sessionId}/close` | Graceful close: SIGTERM, wait 10 s, then SIGKILL if still alive. |

**Admin — install CRUD** (full install schema, used by the admin UI per-row edit form):

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/installs` | Full install list including `wine_binary` + `wine_prefix` + `extra_args`. |
| `POST` | `/admin/installs/clone-mt5` | macOS-only. Clone `/Applications/MetaTrader 5.app` to `/Applications/MetaTrader 5 - <slug>.app` with a unique `CFBundleIdentifier`. **Idempotent** — when the target bundle already exists with the expected identifier, returns the existing-clone metadata (`200`) so the wizard can move on; only refuses with `409` when an unrelated bundle occupies the path. Body: `{brokerSlug, sourceAppPath?}`. |
| `POST` | `/admin/installs/register` | Append a new install entry to `supervisor.yaml` + reload the in-memory config. Auto-fills `broker_name` / `account_login` from `common.ini` and auto-derives `wine_prefix` from the executable path. Body: `{installId, executable, name?, useWine?, wineBinary?, winePrefix?, extraArgs?}`. |
| `PATCH` | `/admin/installs/{installId}` | Sparse update — only the fields actually in the body are mutated. Accepts every `installs[]` field including `winePrefix`. |
| `DELETE` | `/admin/installs/{installId}` | Remove the install entry. Refuses when a tracked session points at it. |

**Admin — orphan processes:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/orphans` | List `terminal64.exe` processes the daemon is NOT tracking (pre-daemon manual launches or stale post-crash leftovers). Each row carries `installHint` when the orphan's executable matches one of the configured installs. |
| `POST` | `/admin/orphans/{pid}/kill` | Terminate one orphan by PID. Refuses tracked-session PIDs (use the close-session endpoint instead) and non-`terminal64.exe` PIDs. Body: `{graceSeconds?, force?}` — `force=true` skips SIGTERM and sends SIGKILL immediately. |

**Admin — daemon ops + observability:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/admin/daemon/reload-config` | Re-read `supervisor.yaml` without restarting the daemon. |
| `POST` | `/admin/daemon/restart` | Self-exit with code 0 after a short delay so launchd's `KeepAlive=true` respawns a fresh process. Returns `202 Accepted` immediately. |
| `POST` | `/admin/daemon/rotate-api-key` | Generate a new `daemon_api_key`, write to `supervisor.yaml`. Engine still has the OLD key pinned via TOFU — response carries the new key + the remediation steps the operator must run. |
| `GET` | `/admin/logs/tail?lines=200` | Tail the daemon's `supervisor.log` (falls back to `stderr.log`). `lines` clamps to `[1, 2000]`. |
| `GET` | `/admin/config` | Return the running `supervisor.yaml` as JSON with `daemon_api_key` + `engine_token` redacted. |

### Engine endpoints (daemon → engine)

The daemon's `EngineClient` calls these. All but `/auth` require a
Bearer JWT obtained from `/auth`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/admin/terminals/daemons/auth` | anonymous + body apiKey (+ optional `X-Daemon-Bootstrap-Token` header) | Bootstrap auth. Returns `{token, expiresAt}`. A new `daemonId` is accepted only from a trusted network or with the bootstrap token (HTTP 403 otherwise); a known one must present its registered apiKey (HTTP 401 otherwise). Rate-limited. |
| `POST` | `/admin/terminals/daemons` | `Bearer` daemon token for **this** `daemonId` (or an operator) | Full register with installs payload. Idempotent — overwrites the engine row. Another daemon's token, or an EA/trading-account token, gets HTTP 403. |
| `POST` | `/admin/terminals/daemons/heartbeat` | same as register | Refresh `LastSeenAt` and reconcile sessions. |

### Operator endpoints (admin UI → engine, engine → daemon proxy)

These are operator-facing routes on the engine that proxy through to
the daemon's `/admin/*` surface. The daemon does not call these — the
engine calls them when operators click buttons in the admin UI. All
require `Bearer` JWT with the `Operator` policy.

**Sessions:**

| Method | Path | Effect |
|--------|------|--------|
| `GET` | `/admin/terminals/daemons` | List all registered daemons. |
| `GET` | `/admin/terminals/sessions?daemonId=&includeClosed=` | List sessions. |
| `POST` | `/admin/terminals/launch` | Proxy `POST /terminals/launch`. |
| `POST` | `/admin/terminals/sessions/{id}/close` | Proxy session close. |

**Install CRUD** (per-daemon — the `{daemonId}` route segment selects which daemon to proxy to):

| Method | Path | Effect |
|--------|------|--------|
| `GET` | `/admin/terminals/daemons/{daemonId}/installs` | Proxy `GET /admin/installs`. |
| `POST` | `/admin/terminals/daemons/{daemonId}/installs/clone-mt5` | Proxy `POST /admin/installs/clone-mt5`. Body: `{brokerSlug, sourceAppPath?}`. |
| `POST` | `/admin/terminals/daemons/{daemonId}/installs/register` | Proxy `POST /admin/installs/register`. |
| `PATCH` | `/admin/terminals/daemons/{daemonId}/installs/{installId}` | Proxy install patch. |
| `DELETE` | `/admin/terminals/daemons/{daemonId}/installs/{installId}` | Proxy install delete. |

**Orphan MT5 processes:**

| Method | Path | Effect |
|--------|------|--------|
| `GET` | `/admin/terminals/daemons/{daemonId}/orphans` | Proxy `GET /admin/orphans`. |
| `POST` | `/admin/terminals/daemons/{daemonId}/orphans/{pid}/kill` | Proxy orphan kill. Body: `{graceSeconds?, force?}`. |

**Daemon ops + observability:**

| Method | Path | Effect |
|--------|------|--------|
| `POST` | `/admin/terminals/daemons/{daemonId}/restart` | Proxy `POST /admin/daemon/restart`. |
| `POST` | `/admin/terminals/daemons/{daemonId}/rotate-api-key` | Proxy `POST /admin/daemon/rotate-api-key`. |
| `GET` | `/admin/terminals/daemons/{daemonId}/logs/tail?lines=` | Proxy `GET /admin/logs/tail`. |
| `GET` | `/admin/terminals/daemons/{daemonId}/config` | Proxy `GET /admin/config`. |

---

## Operational flows

### 1. First-time bootstrap (TOFU)

```
operator runs ./install.sh
  → installer prompts for engine URL, etc.
  → installer auto-generates daemon_api_key (random 32-char base64url)
  → installer writes supervisor.yaml + LaunchAgent plist
  → launchctl bootstrap → daemon starts

daemon startup
  → EngineClient.register() called from FastAPI lifespan
  → EngineClient._ensure_token() finds token cache empty
  → POST /admin/terminals/daemons/auth { daemonId, name, baseUrl, daemonApiKey }
  → engine: no TerminalDaemon row for this daemonId
      → caller on a trusted network (or valid X-Daemon-Bootstrap-Token)?
        yes → CREATE with this apiKey (stored encrypted)
        no  → HTTP 403 "Unknown daemon" — nothing stored
  → engine: GenerateToken(daemonId) → returns { token, expiresAt }
  → EngineClient caches token + expiresAt
  → register() retries with Bearer header → 200 OK
  → heartbeat loop starts, ticks every 30 s

admin UI Terminals page refreshes
  → daemon appears with ONLINE pill + advertised installs
```

### 2. Daemon restart (token still valid)

```
launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor
  → SIGTERM to daemon, then re-spawn
  → daemon startup re-runs lifespan
  → cached token from prior process is gone (in-memory only)
  → first call again hits /auth → existing TerminalDaemon row,
    apiKey matches → 200 OK with fresh token
  → register + heartbeat resume
```

### 3. Engine restart with rotated JWT secret

```
engine reboots with new JwtSettings:SecretKey
daemon heartbeat → POST /heartbeat with old token → 401
EngineClient._request_with_auth sees 401, clears cached token, re-auths
  → POST /auth with apiKey → 200 OK with newly-signed token
EngineClient retries heartbeat with new token → 200 OK
```

The daemon never logs the operator out — even a hard engine restart
with key rotation self-heals on the next heartbeat tick.

### 4. Launch a terminal end-to-end

```
operator clicks Launch on admin UI
  → admin UI POST /admin/terminals/launch { daemonId, installId }
  → engine resolves the daemon row, looks up its BaseUrl + apiKey
  → engine POST <daemon BaseUrl>/terminals/launch
       with X-Daemon-Auth: <apiKey>
       body { installId }
  → daemon ProcessManager.spawn(install) executes:
      cmd = [wine, executable, ...extra_args]  if use_wine else
            [executable, ...extra_args]
      subprocess.Popen(cmd, start_new_session=True)
  → daemon returns session record { sessionId, pid, status: "Launching" }
  → engine persists TerminalSession row, returns to admin UI
  → operator sees session on admin UI

heartbeat tick (≤30 s later):
  → daemon reconciles via psutil.Process(pid).is_running()
  → status flips Launching → Running
  → engine reflects it on next /heartbeat push
```

### 5. Close a terminal

```
operator clicks Close on admin UI
  → admin UI POST /admin/terminals/sessions/{id}/close
  → engine looks up the session's daemon
  → engine POST <daemon BaseUrl>/terminals/{sessionId}/close
       with X-Daemon-Auth
  → daemon ProcessManager: send SIGTERM
  → daemon waits up to 10 s for graceful exit
  → if still alive: send SIGKILL
  → status flips Running → Closing → Closed
```

### Multi-broker isolation (macOS)

The daemon ships two complementary mechanisms so multiple brokers'
MT5 terminals run side-by-side without colliding:

1. **Per-broker bundle clone** — `--clone-mt5` / `POST /admin/installs/clone-mt5`
   creates `/Applications/MetaTrader 5 - <slug>.app` with a unique
   `CFBundleIdentifier` (`net.metaquotes.wine.metatrader5-<slug>`).
   macOS's first-launch dance then provisions a fresh Wine prefix at
   `~/Library/Application Support/<CFBundleIdentifier>/`.

2. **Explicit `WINEPREFIX` on spawn** — at register time the daemon
   walks up from the executable to the parent of `drive_c` and
   persists that as `wine_prefix` in `supervisor.yaml`.  At launch
   time the daemon spawns `terminal64.exe` with
   `env={..., "WINEPREFIX": install.wine_prefix}` so Wine is
   *forced* into the right prefix regardless of which `wine64` binary
   resolves at the front of the command.

**Why both?**  MetaQuotes' bundled `wine64` is bundle-aware — it
picks `WINEPREFIX` from the .app it lives inside, not from the
executable it's launching.  Without (2), every install launched via
the original `MetaTrader 5.app`'s `wine64` would default into the
*original* prefix even when its `executable` lived in a different
clone's prefix.  Two terminals sharing one prefix → MT5's single-
instance lock → broker A killed when broker B starts.  Setting
`WINEPREFIX` explicitly closes that gap belt-and-suspenders.

Auto-derivation runs on both register and launch:

- `POST /admin/installs/register` — when `winePrefix` is absent from
  the body, the handler derives it from the executable path and
  persists.  Operator can override via the body field or via
  `PATCH /admin/installs/{id}`.
- `ProcessManager.launch()` — when `install.wine_prefix` is still
  `null` (existing pre-fix yaml entries), the spawner re-derives at
  spawn time.  No yaml backfill needed; existing installs keep
  working.

If the executable doesn't live under a `drive_c` tree (custom
non-bundle setups), no prefix is set on spawn and Wine falls back to
its built-in default — set `wine_prefix` explicitly in
`supervisor.yaml` for those cases.

### 6. Add a second broker

**On macOS — per-broker Wine prefix (recommended; eliminates shared-state bugs):**

**Path A — admin UI wizard** (Terminals page → daemon card → `+ Add broker terminal`):

```
1. Stage 1 — Clone bundle: type the broker slug → daemon clones the
   .app and rewrites CFBundleIdentifier.  Already-cloned slugs return
   the existing-clone metadata (the endpoint is idempotent), so re-
   opening the wizard after closing it mid-flow doesn't 409.

2. Stage 2 — Manual MT5 setup:
   a. Open the new "MetaTrader 5 - <slug>.app" from Finder.  The wine
      wrapper provisions ~/Library/Application Support/
      net.metaquotes.wine.metatrader5-<slug>/ on first launch (30-90 s).
   b. File → Login to Trade Account, enter the second broker's
      credentials, tick "Save account information".
   c. Drag LascodiaEA onto a chart.  Tools → Options → Expert
      Advisors → "Allow WebRequest for listed URL" → add
      http://127.0.0.1:5081 → close MT5.

3. Stage 3 — Register install: wizard pre-fills `executable` from
   stage 1's `expectedExecutable`.  On submit, the daemon auto-fills
   broker_name + account_login from common.ini, auto-derives
   wine_prefix from the executable, appends to supervisor.yaml, and
   reloads.  The new install appears in the Launch dropdown
   immediately.
```

**Path B — CLI** (no admin UI required):

```
1. ./install.sh --clone-mt5 <broker-slug>
   → cp -R /Applications/MetaTrader 5.app to "MetaTrader 5 - <slug>.app"
   → rewrites CFBundleIdentifier to net.metaquotes.wine.metatrader5-<slug>
   → ad-hoc re-signs (Gatekeeper accepts modified bundles only when re-signed)
   → prints next-step instructions

2. Operator (manual): open the new "MetaTrader 5 - <slug>.app" via Finder.
   The wine wrapper provisions an isolated prefix at
   ~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>/
   on first launch (30-90 s).

3. Operator: File → Login to Trade Account, enter second broker's
   credentials, tick "Save account information".

4. Operator: drag LascodiaEA onto a chart.
   Tools → Options → Expert Advisors → tick "Allow WebRequest for
   listed URL", add http://127.0.0.1:5081.  Close MT5.

5. ./install.sh --add-install \
     "~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>/drive_c/Program Files/MetaTrader 5/terminal64.exe" \
     --install-id <slug>
   → autofills broker_name + account_login from the new prefix's common.ini
   → appends entry to supervisor.yaml (including wine_prefix)
   → kickstarts daemon → re-registers with both installs

6. admin UI Terminals page refresh
   → "2 install(s) advertised", both selectable in the Launch dropdown
   → click Launch terminal → daemon spawns the cloned MT5 + EA on the
     second broker, fully isolated (per-install WINEPREFIX → no MT5
     single-instance collision)
```

**Why bundle-clone over `/portable`?**  The `/portable` flag only
isolates MT5's data dir; the Wine prefix (which holds the broker
credentials cache, the WebRequest allowlist, and the chart-id GVar
namespace) stays shared.  We observed live: account-switching wipes
the WebRequest allowlist; two terminals with the same Wine prefix
derive the same `ChartID()`; both EAs collide on the same
`MQL5/Files/Lascodia/` retry queue.  Bundle clones with unique
`CFBundleIdentifier` — paired with explicit `WINEPREFIX` on spawn —
are how MT5's official macOS wrapper provisions per-install
isolation; same mechanism, just automated.  See
[Multi-broker isolation](#multi-broker-isolation-macos) for why the
explicit `WINEPREFIX` is non-negotiable.

**On Windows VPS — separate install directories (native multi-MT5):**

```
1. Download MT5 installer a second time.  Install to a different
   directory (e.g. C:\Program Files\MetaTrader 5 - ICMarkets\).

2. Launch the new MT5 install, log into the second broker, save
   credentials, configure WebRequest allowlist, close.

3. On the daemon host:
   .\install.ps1 --add-install "C:\Program Files\MetaTrader 5 - ICMarkets\terminal64.exe"
   (install.ps1 is the Windows counterpart, parallel to install.sh.)

4. admin UI Terminals page refresh — second install appears.
```

Windows doesn't have the Wine-prefix problem — each MT5 install is
its own thing.  `--clone-mt5` is macOS-specific.

### Orphan-process cleanup

Sometimes MT5 terminals end up running on the daemon host without a
matching daemon session — pre-daemon manual launches, post-crash
zombies, or anything started outside the daemon's `/terminals/launch`
path.  These don't show up in `GET /terminals` because the daemon's
in-memory `ProcessManager` only knows about processes IT spawned.

The orphan-scan surface fills that gap:

```
# Daemon-side scan (or via the admin UI: Terminals → daemon card →
# "Orphan processes" link, expandable table with Kill / Force buttons).
curl -fsS -H "X-Daemon-Auth: $API_KEY" \
  http://127.0.0.1:7770/admin/orphans
# → [{ pid, name, exePath, cmdline, startedAt, installHint }, ...]
#   installHint is the install_id whose `executable` matches the
#   orphan's path — null when no match.

# Graceful kill (SIGTERM → SIGKILL after 10 s grace).
curl -fsS -X POST -H "X-Daemon-Auth: $API_KEY" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:7770/admin/orphans/<pid>/kill

# Force kill (SIGKILL immediately — use when MT5 is unresponsive).
curl -fsS -X POST -H "X-Daemon-Auth: $API_KEY" \
  -H "Content-Type: application/json" -d '{"force": true}' \
  http://127.0.0.1:7770/admin/orphans/<pid>/kill
```

**Safety guards:**

- Daemon refuses to kill PIDs that ARE tracked sessions — operator
  must use `/terminals/{sessionId}/close` for those so the session
  state machine stays consistent.
- Daemon refuses to kill non-`terminal64.exe` PIDs — guards against
  the endpoint being abused to signal arbitrary host processes.
- `graceSeconds` is clamped to `[0, 60]` on the daemon and mirrored
  in the engine's FluentValidator.

The admin UI's orphan panel auto-refreshes every 5 s while open, so a
freshly-crashed MT5 appears without manual reload.

### 7. Rotate the API key

```
1. operator deletes the engine-side TerminalDaemon row:
   - admin UI Terminals page → remove daemon (TBD on UI)
   - OR direct SQL: DELETE FROM "TerminalDaemon" WHERE "DaemonId" = '<id>'

2. on daemon host:
   ./install.sh --reconfigure --rotate-api-key
   → fresh daemon_api_key written to supervisor.yaml
   → daemon kickstarted

3. daemon's next /auth call:
   → no engine row → CREATE with new apiKey → 200 OK
     (only from a trusted network — see flow #1 — so run this on the
     daemon host, which reaches the engine over loopback / the LAN)
   → register + heartbeat resume
```

If you skip step 1, the daemon's `/auth` call returns HTTP 401 (`-401`) with the
message `Daemon '<id>' is registered with a different API key`. The
daemon logs the error and keeps retrying — no data loss, just stuck.

### 8. Move the daemon to a different machine

```
1. on the new machine:
   git clone <repo>
   ./install.sh
   → during prompts, USE THE SAME daemon_id as the old machine

2. enter the daemon_api_key from the old machine's supervisor.yaml
   (so the engine sees the same apiKey on /auth — TOFU passes)

3. shut down the old daemon (KEEP_CONFIG=1 ./install.sh --uninstall)

4. new daemon's /auth → engine row exists, apiKey matches → 200 OK
   The engine's BaseUrl + Name get updated by the auth handler.
```

---

## File layout & runtime state

### macOS install layout

```
~/Library/Application Support/Lascodia/supervisor/
├── .venv/                    # Python virtualenv, daemon deps live here
├── config/
│   ├── supervisor.yaml       # The config file (mode 600)
│   ├── machine_credential.json
│   └── state/                # sessions.json, watchdog.json, desired_state.json
└── logs/
    ├── supervisor.log        # All logging, rotating 20 MB x 5 (+ .1 .. .5)
    ├── stdout.log            # launchd catch-all (crashes before logging starts); trimmed at start-up
    └── stderr.log            # launchd catch-all; trimmed at start-up when over 20 MB

~/Library/LaunchAgents/
└── com.lascodia.supervisor.plist
```

### LaunchAgent plist details

Generated by `install.sh`. Key settings:

| Key | Value | Reason |
|-----|-------|--------|
| `Label` | `com.lascodia.supervisor` | Standard reverse-DNS label. |
| `ProgramArguments` | `[<venv>/bin/lascodia-supervisor]` | Absolute path — no PATH reliance. |
| `EnvironmentVariables.LASCODIA_SUPERVISOR_CONFIG` | `<config dir>/supervisor.yaml` | Tells `config.py` where to read. |
| `EnvironmentVariables.PATH` | `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:...` | launchd's default PATH lacks Homebrew; needed for `wine` resolution. |
| `RunAtLoad` | `true` | Auto-start at login. |
| `KeepAlive` | `true` | Restart on crash. |
| `ThrottleInterval` | `10` | Anti-crash-loop (matches systemd default). |
| `StandardOutPath` / `StandardErrorPath` | `<install root>/logs/*.log` | Per-install log isolation. |

---

## Security model

### First contact: trusted network or bootstrap token

The engine's `POST /admin/terminals/daemons/auth` endpoint is
anonymous — the daemon needs it to get its first JWT. What it accepts
(engine 2026-10-19+):

- **Known `DaemonId`** — the request must present the `daemon_api_key`
  the engine holds for that id. The comparison is constant-time; a
  wrong key is HTTP 401 and changes nothing (not even `BaseUrl`). This
  works from any network, so a daemon that moved hosts (flow #8) keeps
  authenticating with its key.
- **New `DaemonId`** — its first key gets pinned, so the engine only
  accepts it from a **trusted network** (loopback, RFC 1918 LANs, the
  Docker networks — engine setting `Auth:TrustedNetworks`; never the
  public tunnel), or when the request carries the header
  `X-Daemon-Bootstrap-Token` equal to the engine's
  `Terminals__DaemonBootstrapToken` secret (empty = header path off).
  Anything else is HTTP 403 `Unknown daemon ...` and nothing is stored.
  The public site does not route this endpoint at all.

This daemon does not send the bootstrap header: every supported
topology (daemon on the engine host → `http://localhost:5081`, daemon on
another LAN machine → the LAN endpoint) is a trusted network. A daemon
that must first contact the engine from elsewhere (e.g. a VPN whose
addresses are outside RFC 1918, such as Tailscale's `100.64.0.0/10`)
needs that network added to the engine's `Auth:TrustedNetworks`, or one
first authentication from a trusted network — after which its key is
known and works from anywhere.

The endpoint is rate-limited per client address.

### Machine credentials (enrollment)

Engine multi-tenancy Phase 5 (EA ADR-0017) replaces the daemon key as the daemon's proof of
identity. The sign-in ladder (`EngineClient._authenticate`):

1. **Stored machine credential** (`credential_path`, mode 0600) → `POST /machine/token` → a
   15-minute access token, renewed the same way 5 minutes before it expires. 401 = the engine
   no longer accepts it (revoked): the file is deleted and the ladder continues; 403 = refused
   for now (organisation closed): kept; 404 / 5xx = the engine cannot do machine credentials
   now: kept, and only the daemon key is tried.
2. **`enrollment_token`** → `POST /machine/enroll` (kind `Daemon`, instance id = `daemon_id`) →
   the credential is stored (0600) and used at once. A refused token (used, expired, revoked, a
   `daemon_id` of another organisation) is not sent again; ask for a new one.
3. **`daemon_api_key`** → `POST /admin/terminals/daemons/auth`, while the engine allows it. HTTP
   410 means the engine retired daemon-key sign-in: set `enrollment_token` and restart.

An enrolled daemon belongs to the organisation of its token; the engine stamps its daemon row
and sessions with it. Before it launches a terminal (`POST /terminals/launch`) whose install has
`account_login` + `broker_name` and whose `MQL5/Files/Lascodia` holds no `machine_<login>_*.txt`
credential yet, it asks the engine for an EA-terminal enrollment token bound to that account
(`POST /machine/daemon/enrollments`) and writes it to
`<mql5_dir>/Files/Lascodia/enrollment_token.txt` (0600). The EA (v8.47.253+) uses it once and
deletes it. Failing to prepare a token never blocks the launch. A legacy (daemon-key) session
cannot request terminal tokens — the engine refuses it.

Setting up: create a Daemon enrollment token in the console (people with `accounts.manage`, in the
organisation the host belongs to), put it in `enrollment_token`, restart the daemon
(`launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor`), check the log for
`daemon <id> enrolled`, then clear `enrollment_token` (it is spent).

### Who may write a daemon row

`POST /admin/terminals/daemons` (register) and `/daemons/heartbeat`
rewrite the engine's row — callback `BaseUrl`, `daemon_api_key`,
installs, session states — and the engine later sends launch/close
calls **and the key** to that `BaseUrl`. So:

- a daemon token may only write the row of its own `daemonId`;
- EA and trading-account tokens are refused (HTTP 403) even though they
  carry the same `EA` role as daemon tokens;
- operators with `access.operator` (and super admins) may write any row.

`baseUrl` must be an absolute `http://` or `https://` URL without
user credentials (`http://user:pass@host` is rejected).

### Key storage

The engine stores `daemon_api_key` encrypted at rest (its field
encryption key ring). It has to be recoverable — the engine sends it
back as `X-Daemon-Auth` — so it is encrypted, not hashed. Rows written
before 2026-10-19 held it in plaintext; the engine re-encrypts them on
its first start after the upgrade. Nothing changes on the daemon side.

### Token lifecycle

- JWT issued at `/auth` has the same TTL as trading account tokens
  (default 480 minutes = 8 hours, from `JwtSettings:ExpirationMinutes`).
- `EngineClient` pre-emptively re-auths when the cached token is
  within 5 minutes of expiry (`_REFRESH_MARGIN_SEC = 300`).
- On 401 from any request, the client clears the cache and re-auths
  once. Two consecutive 401s bubble up as a register/heartbeat failure
  and the next tick retries the whole sequence.
- Tokens are kept in process memory only — never persisted to disk (the machine
  credential they come from is, with mode 0600).
- Machine tokens (enrolled daemons) live 15 minutes and renew through `/machine/token`.

### API-key rotation

See [Operational flows §7](#7-rotate-the-api-key). The engine pins
the first key per `daemonId`, so rotation requires engine-side
cooperation (deleting the existing row) — `--rotate-api-key` alone
isn't enough — and the re-registration must come from a trusted
network.

### X-Daemon-Auth (engine → daemon)

When the engine calls daemon endpoints (`/terminals/launch` etc.),
it sends the same `daemon_api_key` as an `X-Daemon-Auth` header. The
daemon checks this in its FastAPI dependency. Symmetric secret — same
value flows both directions.

---

## Troubleshooting

### Launch fails with `Failed to reach daemon at http://127.0.0.1:7770: Connection refused`

The daemon shows ONLINE on the admin UI but the engine can't proxy
launch/close calls into it. Almost always the **Docker-on-same-host
loopback trap**: the engine runs in a Docker container, `127.0.0.1`
inside the container is the *container itself*, not your Mac, so the
container can't reach a daemon bound to `127.0.0.1` on the host.

Fix the daemon's bind + callback:

```bash
# Edit ~/Library/Application Support/Lascodia/supervisor/config/supervisor.yaml:
bind_host: 0.0.0.0                               # accept from Docker bridge
callback_base_url: http://host.docker.internal:7770   # Docker's host DNS

# Reload:
launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor
```

Within one heartbeat the engine's `TerminalDaemon.BaseUrl` updates,
and the next Launch click reaches the daemon.

`install.sh` v0.2+ auto-detects this topology (localhost engine URL
+ Docker running) and applies the right defaults. Re-running
`./install.sh --reconfigure` on an existing install picks up the new
behaviour. Manual installs need the YAML patch above.

### Daemon doesn't appear on admin UI Terminals page

1. `curl http://127.0.0.1:7770/healthz` — daemon up?
   - **No** → `launchctl print gui/$(id -u)/com.lascodia.supervisor` +
     `tail -F ~/Library/Application Support/Lascodia/supervisor/logs/stderr.log`
2. `grep -i auth ~/.../logs/stderr.log` — auth succeeding?
   - **`daemon /auth failed: ... 401`** (`... different API key`) →
     key mismatch. Either `--rotate-api-key` requires you to delete the
     engine row first (see flow #7), or someone else registered with
     this daemonId.
   - **`daemon /auth failed: ... 403`** (`Unknown daemon ...`) → the
     engine has no row for this `daemon_id` and the daemon is not on a
     trusted network. Start it on the engine host or the LAN, or add its
     network to the engine's `Auth:TrustedNetworks` (see
     [Security model](#security-model)).
   - **`daemon /auth failed: ConnectError`** → daemon can't reach
     `engine_url`. Check `curl <engine_url>/health` from the daemon
     host.
3. `docker exec lascodia-trading-engine-postgres-1 psql -U postgres -d LascodiaTradingEngineDb -c 'SELECT * FROM "TerminalDaemon"'`
   — engine row present?

### `0 install(s) advertised`

The daemon's `installs:` list in `supervisor.yaml` is empty, or the
installer's auto-detection didn't fire. Common causes:

- Wine MT5 detection in `install.sh` uses `[[ -f executable ]]`
  (file exists), not `[[ -x ]]` (executable bit). `.exe` files inside
  the Wine prefix are mode 644 — they don't need the Unix executable
  bit because Wine invokes them.
- Manually-added install in a non-standard path → daemon can't find
  `<dir>/config/common.ini` for autofill, but the install entry
  itself is still picked up. Empty `broker_name`/`account_login` is
  cosmetic, not blocking.

Fix: `./install.sh --add-install /path/to/terminal64.exe` adds the
entry explicitly.

### Empty `broker_name` / `account_login` on the admin UI

The MT5 install hasn't been launched yet (no `config/common.ini`).
Launch MT5 once manually, log into the broker account, close MT5,
then `launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor`.
The daemon's `load_config()` will autofill on its next read.

### `Bootstrap failed: 5: Input/output error` from launchctl

The previous LaunchAgent hasn't fully torn down. The installer now
polls `launchctl print` up to 5 s after `bootout` to avoid this, but
if you hit it manually:

```bash
launchctl bootout gui/$(id -u)/com.lascodia.supervisor
sleep 3
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lascodia.supervisor.plist
launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor
```

### Daemon logs show 401 indefinitely after `--rotate-api-key`

You skipped the engine-side delete. The engine still has the old key
pinned via TOFU. See [flow #7](#7-rotate-the-api-key).

### Multiple `python3.X` versions on macOS — installer picks the wrong one

The installer enumerates `/opt/homebrew/bin/python3.13`,
`python3.12`, `python3.11`, then `python3` (unversioned), then
`/usr/local/bin/...`, then PATH, then `/usr/bin/python3`. Highest
version wins.

### Launching broker B's MT5 kills broker A's terminal (macOS multi-broker)

Symptom: two installs registered (`default` + a cloned `<broker>`),
launching the second one via the admin UI shuts down the first MT5
window the same instant the second one appears.

Cause: every install's `terminal64.exe` runs through the SAME Wine
prefix because `WINEPREFIX` was never set explicitly on spawn.
MetaQuotes' bundled `wine64` picks the prefix from the *.app it
lives in (`/Applications/MetaTrader 5.app` → original prefix),
so even with separate `executable` paths the runtime collapsed onto
one prefix.  MT5's per-prefix single-instance lock then evicted the
first terminal.

Fix shipped: daemon now persists `wine_prefix` per install in
`supervisor.yaml` and passes `env={..., "WINEPREFIX": ...}` to every
`Popen`.  Pre-fix yaml entries auto-derive at spawn time.

To verify a given install is isolated:

```bash
# Look at what the daemon will pass to Wine for an install.
grep -B1 -A5 "install_id: <slug>" \
  "${HOME}/Library/Application Support/Lascodia/supervisor/config/supervisor.yaml"
# → wine_prefix should be ~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>
```

If `wine_prefix` is missing AND `executable` doesn't live under a
`drive_c` tree (custom non-bundle setup), set `wine_prefix`
explicitly via `PATCH /admin/installs/{installId}` body
`{ "winePrefix": "<absolute prefix path>" }`.  See
[Multi-broker isolation](#multi-broker-isolation-macos) for details.

### `clone-mt5` returns `409 target bundle already exists`

The endpoint is **idempotent** when the existing bundle's
`CFBundleIdentifier` matches `net.metaquotes.wine.metatrader5-<slug>`
— in that case the daemon returns `200` with the existing-clone
metadata so the admin UI wizard moves on to stage 2.  A 409 means
something other than a daemon-clone occupies that path.  Inspect:

```bash
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" \
  "/Applications/MetaTrader 5 - <slug>.app/Contents/Info.plist"
```

If the identifier is unrelated, remove the bundle (`rm -rf
"/Applications/MetaTrader 5 - <slug>.app"`) or pick a different
slug.

### Wine MT5 spawns but EA doesn't attach

Daemon-launched terminals start with whatever profile MT5 has saved.
For the EA to auto-attach, pre-configure MT5 once:

1. Launch MT5, drag the Lascodia EA onto a chart of your choice.
2. *File → Save as default profile*. Or *Charts → Template → Save*
   and set it as the default template.
3. Close MT5. Future daemon launches will load the EA.

This is documented in
[Operational flows §4](#4-launch-a-terminal-end-to-end).

---

## Smoke test commands

```bash
# Daemon liveness.
curl -s http://127.0.0.1:7770/healthz | jq

# Daemon's view of advertised installs.
curl -s http://127.0.0.1:7770/installs \
     -H "X-Daemon-Auth: $(awk -F'"' '/^daemon_api_key:/{print $2}' \
        "${HOME}/Library/Application Support/Lascodia/supervisor/config/supervisor.yaml")" | jq

# Engine's view of daemons (operator JWT required).
curl -s "$ENGINE_URL/admin/terminals/daemons" \
     -H "Authorization: Bearer $OPERATOR_JWT" | jq

# Launch a terminal via the engine (proxies to daemon).
curl -s "$ENGINE_URL/admin/terminals/launch" \
     -H "Authorization: Bearer $OPERATOR_JWT" \
     -H "Content-Type: application/json" \
     -d '{"daemonId":1,"installId":"wine-mt5-default"}' | jq

# Confirm MT5 is running.
pgrep -laf terminal64.exe

# Daemon's view of running sessions.
curl -s http://127.0.0.1:7770/terminals \
     -H "X-Daemon-Auth: <apiKey>" | jq

# Tail logs.
tail -F "${HOME}/Library/Application Support/Lascodia/supervisor/logs/stderr.log"

# Restart the daemon.
launchctl kickstart -k gui/$(id -u)/com.lascodia.supervisor

# Status of the LaunchAgent.
launchctl print gui/$(id -u)/com.lascodia.supervisor | grep -E "state|pid|last exit"
```

---

## Reference: manual install (non-macOS)

For Linux / Windows VPS deployments (no `install.sh` yet), the steps
are equivalent but you wire your own service manager:

```bash
git clone <repo> && cd lascodia-mt5-supervisor
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
mkdir -p ~/.lascodia
cp config.example.yaml ~/.lascodia/supervisor.yaml
# Edit ~/.lascodia/supervisor.yaml:
#   - daemon_id, name, engine_url, daemon_api_key (32+ chars)
#   - leave engine_token blank for auto-bootstrap
#   - add at least one installs[] entry
LASCODIA_SUPERVISOR_CONFIG=~/.lascodia/supervisor.yaml lascodia-supervisor
```

Wrap that last command in systemd (Linux) or NSSM (Windows) for
auto-restart + auto-start at boot. The daemon's behaviour from
register onwards is identical to the macOS install.
# lascodia-mt5-supervisor
