#!/usr/bin/env bash
# Build the double-click macOS installer from the installer script the engine serves (so it is the
# same script, with the same public address, as the one-line install) and upload it to the engine
# as platform "macos-pkg".  The console offers it as "Download the installer".
#
#   LASCODIA_ADMIN_TOKEN=<jwt> tools/publish_pkg.sh
#   LASCODIA_API_BASE=https://app.example.com/api/v1/lascodia-trading-engine LASCODIA_ADMIN_TOKEN=... tools/publish_pkg.sh
#   DRY_RUN=1 tools/publish_pkg.sh
#
# Change bootstrap/install.sh or bootstrap/macos-pkg/* -> bump bootstrap/macos-pkg/VERSION, then
# publish again (the engine refuses other bytes under a version it already has).
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
API="${LASCODIA_API_BASE:-http://localhost:5081/api/v1/lascodia-trading-engine}"
API="${API%/}"
VERSION="$(tr -d ' \r\n' < "$REPO/bootstrap/macos-pkg/VERSION")"

TOKEN="${LASCODIA_ADMIN_TOKEN:-}"
if [[ -z "$TOKEN" && -n "${LASCODIA_ADMIN_TOKEN_FILE:-}" ]]; then TOKEN="$(tr -d '\r\n' < "$LASCODIA_ADMIN_TOKEN_FILE")"; fi
if [[ -z "$TOKEN" && "${DRY_RUN:-0}" != 1 ]]; then
  echo "Set LASCODIA_ADMIN_TOKEN (or LASCODIA_ADMIN_TOKEN_FILE) to an admin JWT." >&2; exit 2
fi
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CODE=$(curl -sS -o "$WORK/install.sh" -w '%{http_code}' "$API/onboarding/supervisor/install.sh")
[[ "$CODE" == 200 ]] || { echo "could not fetch $API/onboarding/supervisor/install.sh (HTTP $CODE)" >&2; exit 3; }
BAKED="$(sed -n "s/^  API_BASE='\(.*\)'$/\1/p" "$WORK/install.sh" | head -n 1)"
[[ "$BAKED" == https://* ]] || { echo "the engine's installer points at '$BAKED', not a public https address (set Onboarding:PublicApiBaseUrl)" >&2; exit 3; }
OUT="$WORK/out" "$REPO/tools/build_pkg.sh" --install-sh "$WORK/install.sh"
FILE="$WORK/out/lascodia-supervisor-setup-$VERSION.pkg"
mkdir -p "$REPO/dist/installers"; cp "$FILE" "$FILE.sha256" "$REPO/dist/installers/"
SHA="$(cat "$FILE.sha256")"
if [[ "${DRY_RUN:-0}" == 1 ]]; then
  echo "would POST $API/platform/supervisor/releases  version=$VERSION platform=macos-pkg sha256=$SHA (engine $BAKED)"; exit 0
fi
HDR="$WORK/hdr"; ( umask 077; printf 'Authorization: Bearer %s\n' "$TOKEN" > "$HDR" )
CODE=$(curl -sS -o "$WORK/resp" -w '%{http_code}' -H @"$HDR" -F "file=@$FILE;filename=lascodia-supervisor-setup-$VERSION.pkg" \
  -F "version=$VERSION" -F "platform=macos-pkg" -F "sha256=$SHA" "$API/platform/supervisor/releases")
if [[ "$CODE" == 409 ]]; then
  echo "the engine already has installer $VERSION with different bytes: bump bootstrap/macos-pkg/VERSION and publish again" >&2; exit 4
fi
[[ "$CODE" == 2* ]] || { echo "engine answered HTTP $CODE: $(head -c 400 "$WORK/resp")" >&2; exit 4; }
echo "published installer $VERSION ($SHA) for engine $BAKED"
