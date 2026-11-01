#!/bin/sh
# Lascodia supervisor launcher (macOS).  launchd runs this (KeepAlive), it runs current/'s Python.
#
# Before starting it rolls back a self-update that never came up: if update.pending is past its
# deadline (the new version did not register with the engine in time, or cannot even start), it
# points current back at the previous version and records the bad one in update.bad.
# Installed to <root>/bin/run.sh by the installer; the copy in each bundle is the source.
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P) || exit 1
P="$ROOT/update.pending"
if [ -f "$P" ]; then
  from=$(sed -n 's/^from=//p' "$P")
  to=$(sed -n 's/^to=//p' "$P")
  deadline=$(sed -n 's/^deadline=//p' "$P")
  now=$(date +%s)
  if [ -n "$deadline" ] && [ "$now" -ge "$deadline" ] && [ -n "$from" ] && [ -d "$ROOT/versions/$from" ]; then
    ln -sfn "versions/$from" "$ROOT/current"
    echo "$to" >> "$ROOT/update.bad"
    mv -f "$P" "$ROOT/update.rolledback"
    mkdir -p "$ROOT/logs"
    echo "$(date '+%Y-%m-%d %H:%M:%S') launcher: version $to did not come up; rolled back to $from" >> "$ROOT/logs/launcher.log"
  fi
fi
export LASCODIA_SUPERVISOR_HOME="$ROOT"
export LASCODIA_SUPERVISOR_CONFIG="$ROOT/config/supervisor.yaml"
cd "$ROOT" || exit 1
exec "$ROOT/current/python/bin/python3" -I -m lascodia_supervisor run
