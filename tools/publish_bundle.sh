#!/usr/bin/env bash
# Upload the built bundles to the engine (C9: POST platform/supervisor/releases, multipart
# file + version + platform; needs the release-management permission).
#
#   LASCODIA_ADMIN_TOKEN=<jwt> tools/publish_bundle.sh [platform...]
#   LASCODIA_API_BASE=https://app.codiapay.com/api/v1/lascodia-trading-engine LASCODIA_ADMIN_TOKEN=... tools/publish_bundle.sh
#   DRY_RUN=1 tools/publish_bundle.sh          # show what would be uploaded
#
# The token is read from the environment (or LASCODIA_ADMIN_TOKEN_FILE) and passed to curl through a
# header file, never on the command line.
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
API="${LASCODIA_API_BASE:-http://localhost:5081/api/v1/lascodia-trading-engine}"
API="${API%/}"
DIST="${OUT:-$REPO/dist/bundles}"
VERSION="$(python3 -c "import re,sys; print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open(sys.argv[1]).read()).group(1))" "$REPO/lascodia_supervisor/__init__.py")"
PLATFORMS=("$@")
[[ ${#PLATFORMS[@]} -eq 0 ]] && PLATFORMS=(macos-arm64 macos-x86_64 windows-x64)

TOKEN="${LASCODIA_ADMIN_TOKEN:-}"
if [[ -z "$TOKEN" && -n "${LASCODIA_ADMIN_TOKEN_FILE:-}" ]]; then TOKEN="$(tr -d '\r\n' < "$LASCODIA_ADMIN_TOKEN_FILE")"; fi
if [[ -z "$TOKEN" && "${DRY_RUN:-0}" != 1 ]]; then
  echo "Set LASCODIA_ADMIN_TOKEN (or LASCODIA_ADMIN_TOKEN_FILE) to an admin JWT." >&2; exit 2
fi
HDR="$(mktemp)"; trap 'rm -f "$HDR"' EXIT; chmod 600 "$HDR"
printf 'Authorization: Bearer %s\n' "$TOKEN" > "$HDR"

for PLAT in "${PLATFORMS[@]}"; do
  EXT=tar.gz; [[ "$PLAT" == windows-* ]] && EXT=zip
  FILE="$DIST/lascodia-supervisor-$VERSION-$PLAT.$EXT"
  [[ -f "$FILE" ]] || { echo "missing $FILE — run make bundle first" >&2; exit 3; }
  SHA="$(shasum -a 256 "$FILE" | awk '{print $1}')"
  if [[ "${DRY_RUN:-0}" == 1 ]]; then
    echo "would POST $API/platform/supervisor/releases  version=$VERSION platform=$PLAT sha256=$SHA ($(du -h "$FILE" | awk '{print $1}'))"
    continue
  fi
  echo "publishing $PLAT $VERSION ($SHA)"
  CODE=$(curl -sS -o /tmp/lascodia-publish.$$ -w '%{http_code}' -H @"$HDR" \
    -F "file=@$FILE" -F "version=$VERSION" -F "platform=$PLAT" "$API/platform/supervisor/releases")
  if [[ "$CODE" != 2* ]]; then
    echo "engine answered HTTP $CODE: $(head -c 400 /tmp/lascodia-publish.$$)" >&2; rm -f /tmp/lascodia-publish.$$; exit 4
  fi
  rm -f /tmp/lascodia-publish.$$
done
echo "published. Check: GET $API/platform/supervisor/releases"
