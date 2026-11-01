#!/bin/sh
# Lascodia supervisor - one-line installer for macOS.
#
#   curl -fsSL <api>/onboarding/supervisor/install.sh | sh -s -- <INSTALL-CODE>
#   curl -fsSL <api>/onboarding/supervisor/install.sh | sh -s -- --uninstall
#
# Downloads the self-contained supervisor (its own Python; nothing else is installed), checks its
# sha256, installs it for this user in ~/Library/Application Support/Lascodia/supervisor, finds the
# MetaTrader 5 terminals on this Mac, starts it as the LaunchAgent com.lascodia.supervisor and waits
# until it has connected to the engine.  Running it again upgrades in place and keeps this
# computer's identity.  It never starts, stops or changes a MetaTrader terminal itself.
#
# The engine fills in __LASCODIA_API_BASE__ when it serves this file.  No secrets live here: the
# install code is the only credential and it arrives as the argument.
#
# Test hooks (environment): LASCODIA_API_BASE (engine API base), LASCODIA_BUNDLE_FILE (use this
# archive instead of downloading; LASCODIA_BUNDLE_SHA256 optional), LASCODIA_SKIP_SERVICE=1 (write
# everything but do not touch launchd), LASCODIA_LAUNCHCTL (launchctl binary),
# LASCODIA_LAUNCHD_LABEL, LASCODIA_APPS_DIR (where MetaTrader apps are looked for),
# LASCODIA_WAIT_SEC (default 60).
#
# The whole script is one function called on the last line, so a half-downloaded copy runs nothing.

lascodia_install() {
  set -u
  API_BASE='__LASCODIA_API_BASE__'
  case "$API_BASE" in __LASCODIA*) API_BASE="" ;; esac
  [ -n "${LASCODIA_API_BASE:-}" ] && API_BASE="$LASCODIA_API_BASE"
  API_BASE=${API_BASE%/}
  ROOT="$HOME/Library/Application Support/Lascodia/supervisor"
  LABEL="${LASCODIA_LAUNCHD_LABEL:-com.lascodia.supervisor}"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  LAUNCHCTL="${LASCODIA_LAUNCHCTL:-launchctl}"
  WAIT_SEC="${LASCODIA_WAIT_SEC:-60}"
  UID_NUM=$(id -u)
  TMP=""

  say()  { printf '%s\n' "$*"; }
  fail() {
    printf '\nInstall failed: %s\n' "$1" >&2
    [ -n "${2:-}" ] && printf '%s\n' "$2" >&2
    [ -n "$TMP" ] && rm -rf "$TMP"
    return 1
  }

  TOKEN=""
  MODE=install
  for arg in "$@"; do
    case "$arg" in
      --uninstall|-uninstall|uninstall) MODE=uninstall ;;
      -*) ;;
      *) [ -z "$TOKEN" ] && TOKEN="$arg" ;;
    esac
  done

  if [ "$(uname -s)" != "Darwin" ]; then
    fail "this line is for macOS." "On Windows, copy the PowerShell line from the console instead."
    return 1
  fi

  service_stop() {
    [ "${LASCODIA_SKIP_SERVICE:-0}" = 1 ] && return 0
    if "$LAUNCHCTL" print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
      "$LAUNCHCTL" bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
      i=0
      while [ $i -lt 10 ] && "$LAUNCHCTL" print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; do
        sleep 1; i=$((i + 1))
      done
    fi
    return 0
  }

  # ---- uninstall -----------------------------------------------------------------------------
  if [ "$MODE" = uninstall ]; then
    say "Removing the Lascodia supervisor from this Mac..."
    service_stop
    rm -f "$PLIST"
    rm -rf "$ROOT"
    say "Removed. Your MetaTrader terminals were not touched."
    say "To connect this Mac again later, use Add a computer in the console (a new line is needed)."
    return 0
  fi

  # ---- checks --------------------------------------------------------------------------------
  if [ -z "$TOKEN" ]; then
    fail "this line is missing its install code." \
         "In the console open Terminals, click Add a computer and copy the whole line again."
    return 1
  fi
  if [ -z "$API_BASE" ]; then
    fail "this copy of the installer does not know where the engine is." \
         "Copy the line from the console again (do not download the script by hand)."
    return 1
  fi
  case "$(uname -m)" in
    arm64) PLATFORM=macos-arm64 ;;
    x86_64)
      # An Intel shell on Apple Silicon (Rosetta) still gets the native build.
      if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then PLATFORM=macos-arm64; else PLATFORM=macos-x86_64; fi ;;
    *) fail "this Mac's processor ($(uname -m)) is not supported."; return 1 ;;
  esac
  START=$(date +%s)
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/lascodia-install.XXXXXX") || { fail "could not create a temporary folder." "Free some disk space and paste the line again."; return 1; }
  chmod 700 "$TMP"

  say "Installing the Lascodia supervisor on this Mac ($PLATFORM)..."

  # ---- download + verify ---------------------------------------------------------------------
  ARCHIVE="$TMP/bundle.tar.gz"
  if [ -n "${LASCODIA_BUNDLE_FILE:-}" ]; then
    cp "$LASCODIA_BUNDLE_FILE" "$ARCHIVE" || { fail "could not read $LASCODIA_BUNDLE_FILE."; return 1; }
    SHA="${LASCODIA_BUNDLE_SHA256:-}"
  else
    # The install code goes in a header file, so it never shows up in the process list.
    printf 'X-Enrollment-Token: %s\n' "$TOKEN" > "$TMP/headers.txt"
    say "  Downloading..."
    CODE=$(curl -sS -L --retry 3 --retry-delay 2 --connect-timeout 20 -o "$ARCHIVE" -D "$TMP/response.txt" \
           -w '%{http_code}' -H @"$TMP/headers.txt" "$API_BASE/onboarding/supervisor/bundle?platform=$PLATFORM" 2>"$TMP/curl.err") || CODE=000
    rm -f "$TMP/headers.txt"
    case "$CODE" in
      200) ;;
      000) fail "could not reach ${API_BASE%%/api/*}." \
                "Check this Mac's internet connection, then paste the line again. ($(head -c 200 "$TMP/curl.err" 2>/dev/null))"; return 1 ;;
      400|401|403|410) fail "the install code in this line was not accepted (it may have expired or already been used)." \
                "In the console, click Add a computer again to get a fresh line."; return 1 ;;
      404) fail "no supervisor download for $PLATFORM is published yet." \
                "Ask your administrator to publish one, then paste the line again."; return 1 ;;
      *) fail "the engine answered HTTP $CODE to the download." "Wait a minute and paste the line again."; return 1 ;;
    esac
    SHA=$(tr -d '\r' < "$TMP/response.txt" | awk 'tolower($1)=="x-bundle-sha256:"{print $2}' | tail -n 1)
  fi
  GOT=$(shasum -a 256 "$ARCHIVE" 2>/dev/null | awk '{print $1}')
  [ -n "$GOT" ] || GOT=$(openssl dgst -sha256 "$ARCHIVE" | awk '{print $NF}')
  if [ -n "$SHA" ] && [ "$(printf '%s' "$SHA" | tr 'A-F' 'a-f')" != "$GOT" ]; then
    fail "the download is damaged (checksum mismatch)." "Paste the line again; if it keeps failing, tell your administrator."
    return 1
  fi
  if [ -z "$SHA" ] && [ -z "${LASCODIA_BUNDLE_FILE:-}" ]; then
    fail "the engine did not say which download to expect (no checksum)." "Ask your administrator to update the engine."
    return 1
  fi

  # ---- unpack + check ------------------------------------------------------------------------
  mkdir -p "$ROOT/versions" "$ROOT/bin" "$ROOT/logs" "$ROOT/config" || { fail "could not create $ROOT." "Check the disk has free space."; return 1; }
  chmod 700 "$ROOT/config"
  STAGE="$ROOT/versions/.staging-$$"
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  if ! tar -xzf "$ARCHIVE" -C "$STAGE" 2>"$TMP/tar.err"; then
    rm -rf "$STAGE"
    fail "could not unpack the download." "Free some disk space and paste the line again."
    return 1
  fi
  VERSION=$(tr -d ' \r\n' < "$STAGE/VERSION" 2>/dev/null)
  PY="$STAGE/python/bin/python3"
  if [ -z "$VERSION" ] || ! "$PY" -I -m lascodia_supervisor verify-bundle "$STAGE" >/dev/null 2>&1 </dev/null \
     || [ "$("$PY" -I -m lascodia_supervisor version 2>/dev/null </dev/null)" != "$VERSION" ]; then
    rm -rf "$STAGE"
    fail "the downloaded supervisor does not run on this Mac." "Tell your administrator (platform $PLATFORM)."
    return 1
  fi

  # ---- swap in (stop, move, point current, start) --------------------------------------------
  UPGRADE=0
  [ -e "$ROOT/config/supervisor.yaml" ] && UPGRADE=1
  [ $UPGRADE = 1 ] && say "  Upgrading the supervisor already on this Mac (its settings are kept)..."
  service_stop
  rm -rf "$ROOT/versions/$VERSION"
  mv "$STAGE" "$ROOT/versions/$VERSION" || { fail "could not install into $ROOT/versions."; return 1; }
  ln -sfn "versions/$VERSION" "$ROOT/current"
  rm -f "$ROOT/update.pending"
  cp "$ROOT/versions/$VERSION/launcher/run.sh" "$ROOT/bin/run.sh" && chmod 755 "$ROOT/bin/run.sh"
  # Keep the three newest versions (self-update rolls back to the previous one).
  ls -1t "$ROOT/versions" 2>/dev/null | grep -v '^\.' | tail -n +4 | while read -r old; do
    [ "$old" != "$VERSION" ] && rm -rf "$ROOT/versions/$old"
  done
  PY="$ROOT/current/python/bin/python3"

  if LASCODIA_ENROLLMENT_TOKEN="$TOKEN" "$PY" -I -m lascodia_supervisor setup --root "$ROOT" \
       --engine-url "$API_BASE" </dev/null >"$TMP/setup.out" 2>&1; then
    sed 's/^/  /' "$TMP/setup.out"
  else
    sed 's/^/  /' "$TMP/setup.out" >&2
    fail "could not write the supervisor settings." "Paste the line again; if it keeps failing, tell your administrator."
    return 1
  fi

  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$ROOT/bin/run.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- Terminals the supervisor starts must survive it restarting or updating itself. -->
  <key>AbandonProcessGroup</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$ROOT/logs/stdout.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/stderr.log</string>
  <key>WorkingDirectory</key><string>$ROOT</string>
</dict>
</plist>
EOF

  if [ "${LASCODIA_SKIP_SERVICE:-0}" = 1 ]; then
    rm -rf "$TMP"
    say "Installed supervisor $VERSION (test mode: the background service was not started)."
    return 0
  fi
  say "  Starting..."
  if ! "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$PLIST" 2>"$TMP/launchctl.err"; then
    fail "macOS did not start the background service." "Log out and back in, then paste the line again. ($(head -c 200 "$TMP/launchctl.err"))"
    return 1
  fi
  "$LAUNCHCTL" kickstart "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true

  rm -rf "$TMP"; TMP=""
  say "  Waiting for it to connect to the engine (up to $WAIT_SEC s)..."
  if "$PY" -I -m lascodia_supervisor wait-registered --root "$ROOT" --since "$START" --timeout "$WAIT_SEC" </dev/null; then
    return 0
  fi
  return 1
}

lascodia_install "$@"
