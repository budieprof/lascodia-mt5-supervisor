#!/usr/bin/env bash
# Build the double-click macOS installer: one payload-free .pkg (any Mac, Apple silicon or Intel)
# whose postinstall runs the one-line installer (bootstrap/install.sh) for the person signed in.
#
#   tools/build_pkg.sh --api-base https://app.example.com/api/v1/lascodia-trading-engine
#   tools/build_pkg.sh --install-sh served-install.sh     # a copy the engine already filled in
#
# Output: dist/installers/lascodia-supervisor-setup-<VERSION>.pkg (VERSION = bootstrap/macos-pkg/VERSION).
# The package carries no secret: the install code reaches it through its file name, which the
# console sets when it saves the download ("Lascodia Setup <code>.pkg").  Unsigned on purpose (no
# Apple Developer ID): macOS asks the person to confirm it once (Privacy & Security -> Open Anyway).
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SRC="$REPO/bootstrap/macos-pkg"
OUT_DIR="${OUT:-$REPO/dist/installers}"
VERSION="$(tr -d ' \r\n' < "$SRC/VERSION")"
API_BASE="" ; INSTALL_SH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base) API_BASE="${2%/}"; shift 2 ;;
    --install-sh) INSTALL_SH="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
command -v pkgbuild >/dev/null && command -v productbuild >/dev/null || { echo "pkgbuild/productbuild missing (macOS only)" >&2; exit 3; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/scripts" "$WORK/resources" "$OUT_DIR"
if [[ -n "$INSTALL_SH" ]]; then
  cp "$INSTALL_SH" "$WORK/scripts/install.sh"
elif [[ -n "$API_BASE" ]]; then
  # Same guard as the engine's renderer: nothing that could break out of the script's quoting.
  [[ "$API_BASE" =~ ^https?://[A-Za-z0-9._~:/?#@!\&()*+,\;=%-]+$ ]] || { echo "unsafe --api-base" >&2; exit 2; }
  sed "s|__LASCODIA_API_BASE__|$API_BASE|" "$REPO/bootstrap/install.sh" > "$WORK/scripts/install.sh"
else
  echo "give --api-base URL or --install-sh FILE" >&2; exit 2
fi
BAKED="$(sed -n "s/^  API_BASE='\(.*\)'$/\1/p" "$WORK/scripts/install.sh" | head -n 1)"
[[ "$BAKED" == http*://* ]] || { echo "install.sh does not carry an engine address (got '$BAKED')" >&2; exit 2; }
cp "$SRC/postinstall" "$WORK/scripts/postinstall"
chmod 755 "$WORK/scripts/postinstall" "$WORK/scripts/install.sh"
cp "$SRC/resources/"*.html "$WORK/resources/"
sed "s/__PKG_VERSION__/$VERSION/" "$SRC/distribution.xml" > "$WORK/distribution.xml"

# No Finder metadata in the package (it would ship as ._ files).
xattr -cr "$WORK" 2>/dev/null || true
export COPYFILE_DISABLE=1
pkgbuild --quiet --nopayload --scripts "$WORK/scripts" --identifier com.lascodia.supervisor.setup \
  --version "$VERSION" "$WORK/setup.pkg"
OUT_FILE="$OUT_DIR/lascodia-supervisor-setup-$VERSION.pkg"
productbuild --quiet --distribution "$WORK/distribution.xml" --resources "$WORK/resources" \
  --package-path "$WORK" "$OUT_FILE"
shasum -a 256 "$OUT_FILE" | awk '{print $1}' > "$OUT_FILE.sha256"
echo "built $OUT_FILE ($(du -h "$OUT_FILE" | awk '{print $1}'), engine $BAKED)"
