#!/usr/bin/env bash
# Build the double-click Windows installer: one small unsigned .exe (NSIS) that runs the one-line
# installer (bootstrap/install.ps1) for the person signed in to Windows.
#
#   tools/build_exe.sh --api-base https://app.example.com/api/v1/lascodia-trading-engine
#   tools/build_exe.sh --install-ps1 served-install.ps1    # a copy the engine already filled in
#
# Output: dist/installers/lascodia-supervisor-setup-<VERSION>.exe (VERSION = bootstrap/windows-exe/VERSION).
# Needs makensis (macOS: brew install makensis; Linux: apt install nsis).  The .exe carries no
# secret: the install code reaches it through its file name, which the console sets when it saves
# the download ("Lascodia Setup <code>.exe").  No code-signing certificate: Windows SmartScreen asks
# the person to confirm it once (More info -> Run anyway).
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SRC="$REPO/bootstrap/windows-exe"
OUT_DIR="${OUT:-$REPO/dist/installers}"
VERSION="$(tr -d ' \r\n' < "$SRC/VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bootstrap/windows-exe/VERSION must be x.y.z (got '$VERSION')" >&2; exit 2; }
API_BASE="" ; INSTALL_PS1=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base) API_BASE="${2%/}"; shift 2 ;;
    --install-ps1) INSTALL_PS1="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
MAKENSIS="${MAKENSIS:-$(command -v makensis || true)}"
[[ -n "$MAKENSIS" ]] || { echo "makensis missing (brew install makensis / apt install nsis)" >&2; exit 3; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/payload" "$OUT_DIR"
if [[ -n "$INSTALL_PS1" ]]; then
  cp "$INSTALL_PS1" "$WORK/payload/install.ps1"
elif [[ -n "$API_BASE" ]]; then
  # Same guard as the engine's renderer: nothing that could break out of the script's quoting.
  [[ "$API_BASE" =~ ^https?://[A-Za-z0-9._~:/?#@!\&()*+,\;=%-]+$ && "$API_BASE" != *"'"* ]] || { echo "unsafe --api-base" >&2; exit 2; }
  sed "s|__LASCODIA_API_BASE__|$API_BASE|" "$REPO/bootstrap/install.ps1" > "$WORK/payload/install.ps1"
else
  echo "give --api-base URL or --install-ps1 FILE" >&2; exit 2
fi
BAKED="$(sed -n "s/^    \$ApiBase = '\(.*\)'\r\{0,1\}$/\1/p" "$WORK/payload/install.ps1" | head -n 1)"
[[ "$BAKED" == http*://* ]] || { echo "install.ps1 does not carry an engine address (got '$BAKED')" >&2; exit 2; }
# Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page: both scripts must stay ASCII.
for f in "$SRC/setup.ps1" "$WORK/payload/install.ps1"; do
  if LC_ALL=C grep -q '[^[:print:][:space:]]' "$f"; then echo "$(basename "$f") is not ASCII" >&2; exit 2; fi
done
cp "$SRC/setup.ps1" "$WORK/payload/setup.ps1"

OUT_FILE="$OUT_DIR/lascodia-supervisor-setup-$VERSION.exe"
"$MAKENSIS" -V2 -WX -DVERSION="$VERSION" -DOUTFILE="$OUT_FILE" -DPAYLOAD="$WORK/payload" "$SRC/installer.nsi"
shasum -a 256 "$OUT_FILE" | awk '{print $1}' > "$OUT_FILE.sha256"
echo "built $OUT_FILE ($(du -h "$OUT_FILE" | awk '{print $1}'), engine $BAKED)"
