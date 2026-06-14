#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Lascodia MT5 Supervisor — macOS installer
#
# Bootstraps the daemon on macOS:
#   * picks Python ≥ 3.11 (prefers Homebrew over /usr/bin/python3)
#   * creates an isolated venv under ~/Library/Application Support/Lascodia/supervisor/.venv
#   * pip-installs the daemon package (editable from this repo)
#   * writes config.yaml from config.example.yaml, prompting interactively
#     for engine URL, engine JWT, and auto-generating the daemon_api_key
#   * installs a launchd LaunchAgent at
#     ~/Library/LaunchAgents/com.lascodia.supervisor.plist with KeepAlive
#     + RunAtLoad so the daemon survives logouts and crashes
#   * bootstraps the agent and health-checks /healthz
#   * prints the curl command to register the daemon with the engine
#
# Idempotent: re-running preserves an existing config.yaml unless you
# pass --reconfigure.  Uninstall with `./install.sh --uninstall`.
#
# Why bash + launchd, not PowerShell / Windows Service?  The deployment
# host here is macOS (Wine MT5 for development; multi-broker prod could
# move to a Windows VPS later, at which point a separate install.ps1
# adds itself alongside).  launchd's bootstrap + bootout verbs replaced
# the older `launchctl load -w` pattern in macOS 10.10+; we use the
# modern form.
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configurable paths ──────────────────────────────────────────────────
INSTALL_ROOT="${HOME}/Library/Application Support/Lascodia/supervisor"
VENV_DIR="${INSTALL_ROOT}/.venv"
CONFIG_DIR="${INSTALL_ROOT}/config"
CONFIG_FILE="${CONFIG_DIR}/supervisor.yaml"
LOG_DIR="${INSTALL_ROOT}/logs"
PLIST_LABEL="com.lascodia.supervisor"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"

# ── Defaults a fresh install will prompt to override ────────────────────
DEFAULT_BIND_HOST="127.0.0.1"
DEFAULT_BIND_PORT="7770"
DEFAULT_ENGINE_URL="http://localhost:5081/api/v1/lascodia-trading-engine"
DEFAULT_WINE_MT5_PATH="${HOME}/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/terminal64.exe"

# ── Console helpers (ANSI; degrades cleanly without colour TTYs) ────────
if [[ -t 1 ]]; then
  BOLD="\033[1m" ; DIM="\033[2m" ; GREEN="\033[32m" ; YELLOW="\033[33m" ; RED="\033[31m" ; RESET="\033[0m"
else
  BOLD="" ; DIM="" ; GREEN="" ; YELLOW="" ; RED="" ; RESET=""
fi

say()  { printf "%b%s%b\n"  "${BOLD}"   "$*" "${RESET}"; }
note() { printf "  %b%s%b\n" "${DIM}"   "$*" "${RESET}"; }
ok()   { printf "  %b✓%b %s\n" "${GREEN}" "${RESET}" "$*"; }
warn() { printf "  %b!%b %s\n" "${YELLOW}" "${RESET}" "$*"; }
err()  { printf "  %b✗%b %s\n" "${RED}"   "${RESET}" "$*" >&2; }

# ── parse_mt5_account ───────────────────────────────────────────────────
# Parse an MT5 install's saved-account credentials out of its
# config/common.ini.  Emits two lines on stdout:
#   LOGIN=<account_number>
#   SERVER=<broker_server>
# Missing values are emitted as empty strings.  Silently empty when
# the install hasn't been launched yet (common.ini doesn't exist).
#
# Defined at the top of the script so the --add-install short-circuit
# can call it without depending on the install flow setup.
#
# Why parse common.ini and not the binary accounts.dat?
# common.ini stores the last-used Login + Server in plaintext key=value
# pairs every MT5 release respects; accounts.dat is an undocumented
# binary format that changes between builds.
parse_mt5_account() {
  local executable="$1"
  local install_dir
  install_dir="$(dirname -- "${executable}")"
  local ini="${install_dir}/config/common.ini"
  if [[ ! -f "${ini}" ]]; then
    echo "LOGIN="
    echo "SERVER="
    return
  fi
  # MT5 writes common.ini as UTF-16-LE on every shutdown.  iconv-decode
  # to UTF-8 first so plain awk can match the lines.  `|| cat` keeps us
  # working on a UTF-8 ini (some MT5 builds write 8-bit when locale is
  # ASCII).  `|| true` swallows iconv exit codes so set -e doesn't fire.
  local content
  content="$(iconv -f UTF-16LE -t UTF-8 "${ini}" 2>/dev/null || cat "${ini}" 2>/dev/null || true)"
  # awk pattern matches the first occurrence and exits — avoids the
  # SIGPIPE-with-pipefail trap that bites `grep | head -1` under
  # `set -euo pipefail`.
  local login server
  login="$(echo "${content}"  | awk '/^Login=/  { sub(/^Login=/,  ""); print; exit }' || true)"
  server="$(echo "${content}" | awk '/^Server=/ { sub(/^Server=/, ""); print; exit }' || true)"
  login="$(echo  "${login}"  | tr -d '\r' | xargs)"
  server="$(echo "${server}" | tr -d '\r' | xargs)"
  echo "LOGIN=${login}"
  echo "SERVER=${server}"
}

# ── clone_mt5_for_broker ────────────────────────────────────────────────
# Clone /Applications/MetaTrader 5.app to a sibling bundle named for the
# broker (".../MetaTrader 5 - <slug>.app"), with its CFBundleIdentifier
# rewritten so the MetaQuotes wine wrapper provisions an isolated Wine
# prefix at ~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>/.
#
# Why bundle-clone vs MT5's /portable flag?
#   /portable only isolates MT5's own data dir (MQL5/, config/, etc.) but
#   keeps the Wine prefix shared.  Wine state — including the registry
#   account-database — is what binds an MT5 process to a broker login on
#   macOS.  Two processes in one prefix step on each other's saved logins
#   in common.ini (we hit this live: account-switching wiped the
#   WebRequest allowlist).  A second .app bundle with a unique
#   CFBundleIdentifier triggers the wrapper's per-bundle Wine-prefix
#   provisioning — full isolation.
#
# Why ad-hoc re-sign (codesign --sign -)?
#   Gatekeeper refuses to launch a notarized bundle whose Info.plist has
#   been modified.  Removing the original signature + ad-hoc signing
#   keeps the bundle launchable for the operator who built it.  Doesn't
#   work for distribution to other Macs (those need full notarization),
#   which is fine — the clone is a per-host setup step.
clone_mt5_for_broker() {
  local slug="$1"
  if [[ -z "${slug}" ]]; then
    err "--clone-mt5 requires a broker slug (e.g. --clone-mt5 icmarkets)"
    exit 8
  fi
  # Sanitize: lower-case, alphanum + hyphens only.  Same shape as
  # --add-install's auto-derived install_id so the operator can pass
  # the same value here and at --install-id later.
  slug="$(printf '%s' "${slug}" \
    | tr 'A-Z' 'a-z' \
    | tr -c 'a-z0-9' '-' \
    | sed -e 's/-\{2,\}/-/g' -e 's/^-//' -e 's/-$//')"
  if [[ -z "${slug}" ]]; then
    err "broker slug normalises to empty — supply at least one alphanumeric character"
    exit 8
  fi

  local SOURCE_APP="/Applications/MetaTrader 5.app"
  local TARGET_APP="/Applications/MetaTrader 5 - ${slug}.app"
  local NEW_BUNDLE_ID="net.metaquotes.wine.metatrader5-${slug}"
  local NEW_PREFIX="${HOME}/Library/Application Support/${NEW_BUNDLE_ID}"
  local EXPECTED_EXE="${NEW_PREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"

  if [[ ! -d "${SOURCE_APP}" ]]; then
    err "source bundle not found: ${SOURCE_APP}"
    err "install the official MetaTrader 5 macOS app first, then re-run this command"
    exit 8
  fi
  if [[ -e "${TARGET_APP}" ]]; then
    err "target bundle already exists: ${TARGET_APP}"
    err "remove it first (rm -rf '${TARGET_APP}') or pick a different --clone-mt5 slug"
    exit 8
  fi

  say "Cloning MT5 bundle for broker '${slug}'"
  note "Source:  ${SOURCE_APP}"
  note "Target:  ${TARGET_APP}"
  note "Wine prefix will be at: ${NEW_PREFIX}/"

  # cp -R preserves resource forks + metadata that Wine wrappers care
  # about.  On the M-series Mac this takes ~10-30s for a 2-3GB bundle.
  cp -R "${SOURCE_APP}" "${TARGET_APP}"
  ok "Bundle copied"

  # Rewrite CFBundleIdentifier so the wine wrapper sees a new app and
  # spins up an isolated prefix.  CFBundleName drives the macOS display
  # name (window title + Cmd-Tab + Dock).
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier ${NEW_BUNDLE_ID}" \
    "${TARGET_APP}/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName MetaTrader 5 - ${slug}" \
    "${TARGET_APP}/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string MetaTrader 5 - ${slug}" \
    "${TARGET_APP}/Contents/Info.plist"
  ok "Info.plist rewritten (CFBundleIdentifier=${NEW_BUNDLE_ID})"

  # Quarantine flags inherited from the source bundle would trigger a
  # "downloaded from internet" warning on first launch.  The clone is
  # operator-built locally, not downloaded — strip the xattr.
  xattr -rd com.apple.quarantine "${TARGET_APP}" 2>/dev/null || true

  # Gatekeeper rejects a notarized bundle whose contents changed.
  # Strip + ad-hoc re-sign so launchctl will accept it.  --deep covers
  # the embedded wine64 / wineserver / dylibs.  --force overrides the
  # existing signature.
  codesign --remove-signature "${TARGET_APP}" 2>/dev/null || true
  if codesign --force --deep --sign - "${TARGET_APP}" 2>/dev/null; then
    ok "Ad-hoc re-signed"
  else
    warn "codesign --sign - failed; bundle still launchable from /Applications/ but Gatekeeper may prompt on first launch"
  fi

  cat <<EOF

  Bundle clone complete.  Next steps (manual — the Wine prefix is
  initialised lazily by MT5 on first launch):

  1. Launch the new MT5:
       open '${TARGET_APP}'
     The wine wrapper will provision the prefix at:
       ${NEW_PREFIX}/
     This takes 30-90 seconds the first time.

  2. In the new MT5:
       File -> Login to Trade Account
     Enter the second broker's credentials.  Tick "Save account
     information" so the EA can launch without prompting later.

  3. Open the Lascodia EA on a chart (any symbol; the EA will
     re-claim its configured set on first OnInit).  Tools -> Options
     -> Expert Advisors -> tick "Allow WebRequest for listed URL",
     add http://127.0.0.1:5081, OK.

  4. Close MT5.

  5. Register the new install with the daemon:
       ./install.sh --add-install '${EXPECTED_EXE}' --install-id '${slug}'
     This autofills broker_name + account_login from the new prefix's
     config/common.ini and kickstarts the daemon.

  6. Refresh the admin UI Terminals page.  The new install appears in
     the daemon's "pick install" dropdown.  Click Launch terminal.

EOF
  say "Done"
}

# ── Pre-flight: macOS, modern launchd, sufficient Python ────────────────
require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    err "This installer targets macOS.  For Windows VPS deployments, see install.ps1 (TBD)."
    exit 2
  fi
}

resolve_python() {
  # Prefer Homebrew Python (newer, has its own pip) over the macOS system
  # Python (which warns + nags about externally-managed environments).
  #
  # `brew install python@3.12` writes ONLY `python3.12` to its bin dir
  # (no generic `python3` symlink unless the formula was promoted to
  # default).  So we must enumerate the versioned names explicitly —
  # newest first so the resolution preference matches Homebrew's own
  # `python` formula behaviour.
  local candidates=(
    "/opt/homebrew/bin/python3.13"
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3.13"
    "/usr/local/bin/python3.12"
    "/usr/local/bin/python3.11"
    "/usr/local/bin/python3"
    "$(command -v python3.13 || true)"
    "$(command -v python3.12 || true)"
    "$(command -v python3.11 || true)"
    "$(command -v python3 || true)"
    "/usr/bin/python3"
  )
  for candidate in "${candidates[@]}"; do
    [[ -z "${candidate}" || ! -x "${candidate}" ]] && continue
    local ver
    ver="$(${candidate} -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [[ -z "${ver}" ]] && continue
    # Compare as zero-padded ints: 3.11 → 311
    local n
    n=$(awk -F. '{printf "%d%02d", $1, $2}' <<< "${ver}")
    if (( n >= 311 )); then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

# ── Uninstall path ──────────────────────────────────────────────────────
do_uninstall() {
  say "Uninstalling Lascodia MT5 Supervisor"

  if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
    ok "LaunchAgent unloaded"
  else
    note "LaunchAgent not loaded — skipping bootout"
  fi

  [[ -f "${PLIST_PATH}" ]] && rm -f "${PLIST_PATH}" && ok "Removed ${PLIST_PATH}"

  if [[ -d "${INSTALL_ROOT}" ]]; then
    if [[ "${KEEP_CONFIG:-0}" == "1" ]]; then
      # Keep config + logs; nuke only the venv so a future re-install
      # is fast.  This is what most operators want when "removing".
      rm -rf "${VENV_DIR}"
      ok "Removed venv; preserved ${CONFIG_DIR} and ${LOG_DIR}"
    else
      rm -rf "${INSTALL_ROOT}"
      ok "Removed ${INSTALL_ROOT}"
    fi
  fi
  say "Uninstall complete"
  exit 0
}

# ── Argument parsing ────────────────────────────────────────────────────
RECONFIGURE=0
SKIP_HEALTH=0
NON_INTERACTIVE=0
ROTATE_API_KEY=0
ADD_INSTALL_EXEC=""
ADD_INSTALL_ID=""
ADD_INSTALL_NAME=""
CLONE_MT5_BROKER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall)        do_uninstall ;;
    --reconfigure)      RECONFIGURE=1 ;;
    --rotate-api-key)   ROTATE_API_KEY=1 ;;
    --skip-health)      SKIP_HEALTH=1 ;;
    --non-interactive)  NON_INTERACTIVE=1 ;;
    --add-install)      shift; ADD_INSTALL_EXEC="${1:-}" ;;
    --install-id)       shift; ADD_INSTALL_ID="${1:-}" ;;
    --install-name)     shift; ADD_INSTALL_NAME="${1:-}" ;;
    --clone-mt5)        shift; CLONE_MT5_BROKER="${1:-}" ;;
    -h|--help)
      cat <<EOF
Usage: ./install.sh [OPTIONS]

  --reconfigure      Overwrite an existing supervisor.yaml (otherwise preserved).
                     Preserves daemon_api_key by default — see --rotate-api-key.
  --rotate-api-key   On --reconfigure, generate a fresh daemon_api_key.  The
                     engine's TOFU rule means you must ALSO delete the engine
                     row first (admin UI Terminals page, or DELETE the DB row).
  --add-install PATH Append an additional MT5 install entry to supervisor.yaml
                     (PATH = absolute path to terminal64.exe of the install to
                     add).  broker_name + account_login are autofilled from the
                     install's config/common.ini.  Pair with --install-id and
                     --install-name to override the auto-generated slug / label.
                     The LaunchAgent is kickstarted after the edit so the daemon
                     re-registers with the new advertised installs immediately.
  --install-id ID    With --add-install, override the slug (default: derived from
                     broker server, e.g. "icmarkets-live").
  --install-name N   With --add-install, override the human-readable label.
  --clone-mt5 SLUG   Clone /Applications/MetaTrader 5.app -> ".../MetaTrader 5 -
                     <slug>.app" with its CFBundleIdentifier rewritten so macOS
                     gives it its own Wine prefix at
                     ~/Library/Application Support/net.metaquotes.wine.metatrader5-<slug>/.
                     Eliminates the shared-prefix bugs (chart-id collisions,
                     stomped WebRequest allowlist, shared retry queue) when
                     running MT5 against multiple brokers on macOS.  After the
                     clone, you launch the new app manually once to initialise
                     the Wine prefix and log into the broker, then point
                     --add-install at the new terminal64.exe.
  --uninstall        Stop the LaunchAgent and remove the install directory.
                     Set KEEP_CONFIG=1 in env to keep config + logs.
  --skip-health      Don't curl /healthz after starting the daemon.
  --non-interactive  Don't prompt for engine URL / JWT — install with placeholders
                     for an unattended bootstrap, expecting --reconfigure later.
  -h, --help         Show this help.
EOF
      exit 0
      ;;
    *)
      err "Unknown flag: $1 (see --help)"
      exit 2
      ;;
  esac
  shift
done

# ── Begin install ───────────────────────────────────────────────────────
require_macos

# ── --clone-mt5 short-circuit (bundle clone, no venv/plist touch) ──────
# Per-broker Wine prefix isolation: clone /Applications/MetaTrader 5.app
# under a unique CFBundleIdentifier so the wine wrapper provisions a
# separate prefix.  No daemon-side state changes happen here — the
# operator runs the manual MT5 first-launch + login dance, then comes
# back to --add-install which actually registers the new terminal64.exe
# with the daemon.
if [[ -n "${CLONE_MT5_BROKER}" ]]; then
  clone_mt5_for_broker "${CLONE_MT5_BROKER}"
  exit 0
fi

# ── --add-install short-circuit (no venv/plist rebuild) ────────────────
# Append a new entry to the existing supervisor.yaml's installs list,
# autofilling broker_name + account_login from the new install's
# common.ini.  Uses Python for YAML manipulation because preserving
# the existing file's structure + comments through a bash sed-rewrite
# is fragile.  PyYAML is already in the venv.
if [[ -n "${ADD_INSTALL_EXEC}" ]]; then
  say "Appending install entry to supervisor.yaml"

  if [[ ! -f "${CONFIG_FILE}" ]]; then
    err "No existing config at ${CONFIG_FILE} — run ./install.sh first to bootstrap, then --add-install"
    exit 6
  fi
  if [[ ! -f "${ADD_INSTALL_EXEC}" ]]; then
    err "executable not found: ${ADD_INSTALL_EXEC}"
    exit 6
  fi

  # Resolve to absolute path so the daemon doesn't depend on its own
  # working directory at launch time.
  ABS_EXEC="$(cd -- "$(dirname -- "${ADD_INSTALL_EXEC}")" && pwd -P)/$(basename -- "${ADD_INSTALL_EXEC}")"

  # Parse broker + login from this install's common.ini.  Default to
  # empty before eval so `set -u` doesn't trip when the file is absent
  # (parse_mt5_account emits "LOGIN=" / "SERVER=" in that case).
  LOGIN=""
  SERVER=""
  eval "$(parse_mt5_account "${ABS_EXEC}")"
  if [[ -z "${SERVER}" && -z "${LOGIN}" ]]; then
    warn "common.ini absent for this install — broker/account will be empty until MT5 has been launched once"
  else
    note "Autofilled broker=${SERVER:-?} login=${LOGIN:-?} from common.ini"
  fi

  # Derive an install_id slug from broker server if the operator didn't
  # supply one.  e.g. "MetaQuotes-Demo" → "metaquotes-demo".  Falls back
  # to a generic "mt5-<NN>" when no broker info is available, which the
  # operator can rename later.
  if [[ -z "${ADD_INSTALL_ID}" ]]; then
    if [[ -n "${SERVER}" ]]; then
      # Lower-case, replace non-alphanum chars with hyphens, collapse
      # runs of hyphens, strip leading/trailing hyphens.  Input
      # "ICMarketsSC-Live" → "icmarketssc-live" (no trailing dash from
      # the echo newline that tr -c would otherwise convert).
      ADD_INSTALL_ID="$(printf '%s' "${SERVER}" \
        | tr 'A-Z' 'a-z' \
        | tr -c 'a-z0-9' '-' \
        | sed -e 's/-\{2,\}/-/g' -e 's/^-//' -e 's/-$//')"
    else
      # Pick a unique-ish suffix from epoch seconds so concurrent adds
      # don't collide.  PyYAML dedup-checks the slug anyway.
      ADD_INSTALL_ID="mt5-$(date +%s | tail -c 6)"
    fi
  fi
  [[ -z "${ADD_INSTALL_NAME}" ]] && ADD_INSTALL_NAME="${SERVER:-MT5} (${ADD_INSTALL_ID})"

  # Detect whether to set use_wine.  An MT5 install inside a Wine
  # prefix has "net.metaquotes.wine" or "wine" in its path; native
  # Windows installs (e.g. operator running this on a Mac that also
  # mounts a Windows drive) don't.  Falling back to true on macOS is
  # the safe default — Wine handles native .exe paths transparently
  # via /proc/sys/fs/binfmt_misc, but only when configured.
  # Python boolean literal — capital-T True so the embedded YAML-mutator
  # heredoc parses cleanly.  Both branches default to True on macOS
  # since native MT5 doesn't run here; operator can flip to False in
  # supervisor.yaml when running on a Windows VPS variant of the daemon.
  if [[ "${ABS_EXEC}" == *"net.metaquotes.wine"* || "${ABS_EXEC}" == *"/wine/"* ]]; then
    USE_WINE="True"
  else
    USE_WINE="True"
  fi

  # YAML mutation via embedded Python.  ROUND-TRIPS through PyYAML so
  # existing comments are preserved as best-effort (PyYAML's default
  # SafeLoader drops comments; using ruamel.yaml would preserve them
  # perfectly but adds a dependency.  Trading comment loss for
  # zero-deps simplicity here — the file is regenerable from
  # --reconfigure anyway).
  VENV_DIR_FOR_PY="${VENV_DIR}"
  if [[ ! -x "${VENV_DIR_FOR_PY}/bin/python" ]]; then
    err "venv missing at ${VENV_DIR_FOR_PY} — run ./install.sh first"
    exit 6
  fi

  "${VENV_DIR_FOR_PY}/bin/python" - <<PYEOF
import sys, yaml
from pathlib import Path

cfg_path = Path("${CONFIG_FILE}")
with cfg_path.open("r") as f:
    data = yaml.safe_load(f) or {}

installs = data.setdefault("installs", [])
new_id = "${ADD_INSTALL_ID}"

# Refuse to duplicate an existing install_id — guides operator to
# either pick a different id or run --reconfigure (full rewrite).
for existing in installs:
    if existing.get("install_id") == new_id:
        print(f"  ✗ install_id '{new_id}' already exists in supervisor.yaml — pick a different --install-id", file=sys.stderr)
        sys.exit(7)

installs.append({
    "install_id":    new_id,
    "name":          "${ADD_INSTALL_NAME}",
    "executable":    "${ABS_EXEC}",
    "use_wine":      ${USE_WINE},
    "extra_args":    [],
    "broker_name":   "${SERVER}",
    "account_login": "${LOGIN}",
})

with cfg_path.open("w") as f:
    yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)

print(f"  ✓ Appended install '${ADD_INSTALL_ID}' ({len(installs)} install(s) total)")
PYEOF
  PY_EXIT=$?
  if [[ "${PY_EXIT}" -ne 0 ]]; then
    exit "${PY_EXIT}"
  fi

  # Kickstart the daemon so it re-reads supervisor.yaml and re-registers
  # with the engine.  The daemon's heartbeat loop won't pick up new
  # installs without a restart — installs are only sent on register.
  launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null \
    && ok "Daemon kickstarted; new install will appear on Terminals page after next register"
  say "Done"
  exit 0
fi

say "Lascodia MT5 Supervisor — macOS install"
note "Install root: ${INSTALL_ROOT}"
note "Repo root:    ${REPO_ROOT}"

# 1. Python
PYTHON_BIN="$(resolve_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  err "Couldn't find Python >= 3.11."
  err "Install via Homebrew:  brew install python@3.12"
  err "Or download:  https://www.python.org/downloads/macos/"
  exit 3
fi
PY_VERSION="$(${PYTHON_BIN} -c 'import sys; print(".".join(map(str,sys.version_info[:3])))')"
ok "Python ${PY_VERSION} at ${PYTHON_BIN}"

# 2. Directories
mkdir -p "${INSTALL_ROOT}" "${CONFIG_DIR}" "${LOG_DIR}"
ok "Created install + config + log directories"

# 3. Virtualenv
if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  ok "Created venv"
else
  note "Reusing existing venv"
fi
# Activate via direct path — no `source` needed in a non-login shell.
VENV_PIP="${VENV_DIR}/bin/pip"
VENV_BIN="${VENV_DIR}/bin/lascodia-supervisor"

"${VENV_PIP}" install --quiet --upgrade pip
"${VENV_PIP}" install --quiet -e "${REPO_ROOT}"
ok "Installed lascodia-mt5-supervisor (editable from ${REPO_ROOT})"

if [[ ! -x "${VENV_BIN}" ]]; then
  err "Entry point ${VENV_BIN} missing after pip install — check pyproject.toml [project.scripts]"
  exit 4
fi

# 4. Config
generate_secret() {
  "${PYTHON_BIN}" -c 'import secrets; print(secrets.token_urlsafe(32))'
}

prompt() {
  # prompt VAR_NAME "Question" "default"
  local var="$1"
  local question="$2"
  local default="${3:-}"
  if (( NON_INTERACTIVE )); then
    eval "${var}=\"\${default}\""
    return
  fi
  local answer
  if [[ -n "${default}" ]]; then
    read -r -p "  ${question} [${default}]: " answer || true
    answer="${answer:-${default}}"
  else
    read -r -p "  ${question}: " answer || true
  fi
  eval "${var}=\"\${answer}\""
}

if [[ -f "${CONFIG_FILE}" && "${RECONFIGURE}" == 0 ]]; then
  note "Existing config at ${CONFIG_FILE} preserved (re-run with --reconfigure to overwrite)"
else
  say "Configuring supervisor"
  DAEMON_ID_DEFAULT="$(hostname -s | tr 'A-Z' 'a-z')-mac"
  prompt DAEMON_ID         "Daemon ID (unique across the fleet)"  "${DAEMON_ID_DEFAULT}"
  prompt DAEMON_NAME       "Human-readable name"                  "Mac Wine (${USER})"
  # Detect the Docker-on-same-host case: the engine runs in a Docker
  # container on this Mac and the operator entered a localhost engine
  # URL.  In that topology a 127.0.0.1 bind + 127.0.0.1 callback is
  # broken because `127.0.0.1` inside the container is the container
  # itself, not the host — the engine can't reach the daemon back.
  #
  # When detected, default to:
  #   bind_host         = 0.0.0.0                         (accept from Docker bridge)
  #   callback_base_url = http://host.docker.internal:<port>  (Docker DNS for host)
  # Without it, `0 install(s) advertised` or `Failed to reach daemon`
  # errors surface on the admin UI Terminals page.
  prompt ENGINE_URL        "Engine API URL"                       "${DEFAULT_ENGINE_URL}"
  if [[ "${ENGINE_URL}" == *"localhost"* || "${ENGINE_URL}" == *"127.0.0.1"* ]] \
     && docker info >/dev/null 2>&1; then
    DEFAULT_BIND_HOST_NOW="0.0.0.0"
    DEFAULT_CALLBACK_HOST="host.docker.internal"
    note "Detected Docker-on-same-host engine — defaulting bind_host=0.0.0.0 + callback=${DEFAULT_CALLBACK_HOST}"
  else
    DEFAULT_BIND_HOST_NOW="${DEFAULT_BIND_HOST}"
    DEFAULT_CALLBACK_HOST=""   # empty → callback uses bind_host (loopback path)
  fi
  prompt BIND_HOST         "Bind host"                            "${DEFAULT_BIND_HOST_NOW}"
  prompt BIND_PORT         "Bind port"                            "${DEFAULT_BIND_PORT}"
  # Phase v0.2: daemon auto-bootstraps its JWT via POST
  # /admin/terminals/daemons/auth using daemon_api_key as the proof-of-
  # identity.  No operator JWT minting required.  Left blank in the
  # generated config so EngineClient takes the bootstrap path.
  ENGINE_TOKEN=""
  # Preserve the existing daemon_api_key if there's one in the prior
  # config — TOFU on the engine side means rotating it locally without
  # also wiping the engine-side TerminalDaemon row locks the daemon
  # out of /auth.  Only generate a fresh key on a clean install.
  # Pass --rotate-api-key to opt into a rotation (which still needs an
  # operator to clear the engine row first; documented in --help).
  if [[ "${ROTATE_API_KEY:-0}" == "1" ]] || [[ ! -f "${CONFIG_FILE}" ]]; then
    DAEMON_API_KEY_DEFAULT="$(generate_secret)"
  else
    # Pull the existing key out of the prior yaml.  Falls back to a
    # fresh secret only when the line is missing or malformed — an
    # operator-friendly safety net rather than a silent regeneration.
    DAEMON_API_KEY_DEFAULT="$(awk -F'"' '/^daemon_api_key:/{print $2; exit}' "${CONFIG_FILE}" 2>/dev/null || true)"
    if [[ -z "${DAEMON_API_KEY_DEFAULT}" ]]; then
      DAEMON_API_KEY_DEFAULT="$(generate_secret)"
      warn "Couldn't read prior daemon_api_key — generating a fresh one (engine-side row needs deletion)"
    else
      note "Preserving existing daemon_api_key from prior config (pass --rotate-api-key to force rotation)"
    fi
  fi
  prompt DAEMON_API_KEY    "X-Daemon-Auth shared secret (engine→daemon)" "${DAEMON_API_KEY_DEFAULT}"

  # Detect Wine MT5 install for the default install entry.  If present
  # we seed it; otherwise leave installs: [] and let the operator add
  # entries later via the admin UI's Terminals page.
  #
  # Test is `-f`, not `-x`: terminal64.exe inside the Wine prefix is
  # always mode 644 (no Unix executable bit — .exe runs through `wine`,
  # not directly).  An `-x` test misses this and falls through to the
  # empty-installs branch silently.
  WINE_BLOCK=""
  if [[ -f "${DEFAULT_WINE_MT5_PATH}" ]]; then
    note "Detected Wine MT5 at ${DEFAULT_WINE_MT5_PATH} — seeding a default install entry"
    # Pull broker_name + account_login from the install's common.ini
    # if it's been launched at least once.  Empty values are written
    # verbatim so the daemon's runtime autofill (config.py) can still
    # populate them later — the installer doesn't fail just because
    # MT5 hasn't been opened yet.
    LOGIN=""
    SERVER=""
    eval "$(parse_mt5_account "${DEFAULT_WINE_MT5_PATH}")"
    if [[ -n "${LOGIN}" || -n "${SERVER}" ]]; then
      note "Autofilled broker=${SERVER:-?} login=${LOGIN:-?} from common.ini"
    else
      note "No saved account in common.ini yet — daemon will autofill on first launch"
    fi
    WINE_BLOCK=$(cat <<EOF
installs:
  - install_id: "wine-mt5-default"
    name: "Wine MT5 (autodetected)"
    executable: "${DEFAULT_WINE_MT5_PATH}"
    use_wine: true
    extra_args: []
    broker_name: "${SERVER}"
    account_login: "${LOGIN}"
EOF
)
  else
    WINE_BLOCK="installs: []"
  fi

  cat > "${CONFIG_FILE}" <<EOF
# Generated by lascodia-mt5-supervisor/install.sh on $(date '+%Y-%m-%d %H:%M:%S %Z')
# Re-run with --reconfigure to regenerate, or edit in place.

daemon_id: "${DAEMON_ID}"
name: "${DAEMON_NAME}"

bind_host: "${BIND_HOST}"
bind_port: ${BIND_PORT}

# Callback URL the engine uses to reach this daemon.  For loopback dev
# this matches bind_host:bind_port; for cross-host (engine on a server,
# daemon on a Mac) point at the Mac's reachable address.  For
# Docker-on-same-host (engine in a container on this Mac) we use
# host.docker.internal so the engine container can route back to the
# host.
callback_base_url: "http://${DEFAULT_CALLBACK_HOST:-${BIND_HOST}}:${BIND_PORT}"

engine_url: "${ENGINE_URL}"

# Auto-bootstrap path: leave engine_token empty and the daemon hits
# POST /admin/terminals/daemons/auth on startup, exchanging
# daemon_api_key for a short-lived JWT.  Set this only if you want to
# pin a longer-lived operator-minted token.
engine_token: "${ENGINE_TOKEN}"

# The proof-of-identity for the auto-bootstrap exchange.  Engine pins
# this on TOFU; future auths must present the matching value.  Rotate
# by deleting the engine-side TerminalDaemon row before changing.
daemon_api_key: "${DAEMON_API_KEY}"

heartbeat_interval_sec: 30

${WINE_BLOCK}
EOF
  chmod 600 "${CONFIG_FILE}"
  ok "Wrote ${CONFIG_FILE} (mode 600)"
fi

# 5. LaunchAgent plist
say "Installing LaunchAgent"
mkdir -p "$(dirname -- "${PLIST_PATH}")"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <!-- The daemon entry script is in the venv; passing the absolute path
       avoids any reliance on PATH at LaunchAgent invocation time. -->
  <key>ProgramArguments</key>
  <array>
    <string>${VENV_BIN}</string>
  </array>

  <!-- Config path is read from this env var by config.py.  Setting it
       here keeps the supervisor.yaml location out of the daemon's
       hardcoded defaults. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>LASCODIA_SUPERVISOR_CONFIG</key>
    <string>${CONFIG_FILE}</string>
    <!-- Ensure Wine + Homebrew binaries resolve when the daemon spawns
         terminal64.exe via the Wine wrapper.  launchd's default PATH
         is /usr/bin:/bin:/usr/sbin:/sbin — no Homebrew. -->
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <!-- Start at login + restart on crash.  ThrottleInterval prevents a
       crash-loop from melting the CPU; 10s matches systemd's default. -->
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/stderr.log</string>

  <key>WorkingDirectory</key>
  <string>${INSTALL_ROOT}</string>
</dict>
</plist>
EOF
ok "Wrote ${PLIST_PATH}"

# Bootout any previous instance to pick up plist + venv changes, then
# bootstrap fresh.  `bootstrap`/`bootout` replaced load/unload in
# macOS 10.10+; the latter still works but emits a deprecation.
#
# Race: launchd's tear-down is asynchronous — `bootout` returns
# instantly but the agent may not actually be gone for ~1s, after
# which `bootstrap` fails with "5: Input/output error".  Poll
# `launchctl print` until the agent disappears (max 5 s) before
# re-bootstrapping.
if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1 || break
    sleep 1
  done
fi
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"
launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}"
ok "LaunchAgent bootstrapped + kickstarted"

# 6. Health check
if (( SKIP_HEALTH == 0 )); then
  say "Health-checking daemon"
  BIND_HOST_FOR_CHECK="$(grep -E '^bind_host:' "${CONFIG_FILE}" | awk -F'"' '{print $2}')"
  BIND_PORT_FOR_CHECK="$(grep -E '^bind_port:' "${CONFIG_FILE}" | awk '{print $2}')"
  HEALTH_URL="http://${BIND_HOST_FOR_CHECK}:${BIND_PORT_FOR_CHECK}/healthz"

  # Up to ~6s for uvicorn to bind.  The daemon's lifespan() runs first
  # heartbeat at startup which can take ~500ms over a healthy network.
  for attempt in 1 2 3 4 5 6; do
    if curl -fsS -m 2 "${HEALTH_URL}" >/dev/null 2>&1; then
      ok "Daemon healthy at ${HEALTH_URL}"
      break
    fi
    if (( attempt == 6 )); then
      err "Daemon did not become healthy in 6s."
      warn "Check logs:  tail -F '${LOG_DIR}/stderr.log'"
      exit 5
    fi
    sleep 1
  done
fi

# 7. Closing notes — auto-bootstrap path needs no manual registration.
say "Daemon auto-registers via /admin/terminals/daemons/auth on startup"
DAEMON_ID_FROM_CFG="$(grep -E '^daemon_id:' "${CONFIG_FILE}" | awk -F'"' '{print $2}')"
ENGINE_URL_FROM_CFG="$(grep -E '^engine_url:' "${CONFIG_FILE}" | awk -F'"' '{print $2}')"

cat <<EOF

  The daemon's first heartbeat exchanges daemon_api_key for a JWT and
  registers itself on the engine.  No manual curl required.  Watch
  it land:

    tail -F '${LOG_DIR}/stderr.log' | grep -E 'authenticated|register'
    curl -fsS '${ENGINE_URL_FROM_CFG}/admin/terminals/daemons'        # operator JWT required

  After the first successful auth the daemon appears on the admin UI's
  Terminals page and the engine can route CMD_LAUNCH_INSTANCE through
  it.

  Useful local commands:
    launchctl print 'gui/$(id -u)/${PLIST_LABEL}'    # status
    launchctl kickstart -k 'gui/$(id -u)/${PLIST_LABEL}'  # restart
    tail -F '${LOG_DIR}/stderr.log'                  # follow logs
    ${0} --reconfigure                               # change config
    ${0} --uninstall                                 # remove

  Daemon id:  ${DAEMON_ID_FROM_CFG}

EOF
say "Install complete"
