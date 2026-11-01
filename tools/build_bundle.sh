#!/usr/bin/env bash
# Build the relocatable supervisor bundles (contract C9).
#
#   tools/build_bundle.sh                     # macos-arm64 macos-x86_64 windows-x64
#   tools/build_bundle.sh macos-arm64         # just one
#
# Each bundle = python-build-standalone CPython 3.12 ("install_only_stripped") + the pinned
# dependencies in tools/bundle-requirements*.txt (installed for the TARGET platform with
# pip --platform --only-binary=:all: --no-deps, so it cross-builds from this Mac) + this package +
# launcher/ + bootstrap/ + VERSION + MANIFEST.sha256.  Archive contents sit at the archive root, so
# unpacking into versions/<v>/ gives versions/<v>/python/... directly.
#
# Output: dist/bundles/lascodia-supervisor-<version>-<platform>.tar.gz (macOS) / .zip (Windows),
# each with a .sha256 next to it.  Downloads are cached in ~/.cache/lascodia-supervisor-bundle.
#
# Needs: python3.12 with pip on PATH (or HOST_PYTHON=...), curl, tar.
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PBS_TAG="${PBS_TAG:-20261026}"
PY_VERSION="${PY_VERSION:-3.12.16}"
HOST_PYTHON="${HOST_PYTHON:-$(command -v python3.12 || command -v python3)}"
OUT="${OUT:-$REPO/dist/bundles}"
CACHE="${CACHE:-$HOME/.cache/lascodia-supervisor-bundle}"
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG"
VERSION="$("$HOST_PYTHON" -c "import re,sys; print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open(sys.argv[1]).read()).group(1))" "$REPO/lascodia_supervisor/__init__.py")"

PLATFORMS=("$@")
[[ ${#PLATFORMS[@]} -eq 0 ]] && PLATFORMS=(macos-arm64 macos-x86_64 windows-x64)

log() { printf '[bundle] %s\n' "$*"; }

pbs_triple() {
  case "$1" in
    macos-arm64)  echo aarch64-apple-darwin ;;
    macos-x86_64) echo x86_64-apple-darwin ;;
    windows-x64)  echo x86_64-pc-windows-msvc ;;
    *) echo "unknown platform $1" >&2; return 1 ;;
  esac
}

pip_platforms() {
  # pip does not expand these for us when --platform is given: list every tag a wheel may carry.
  case "$1" in
    macos-arm64)  echo "macosx_11_0_arm64 macosx_12_0_arm64 macosx_13_0_arm64 macosx_14_0_arm64 macosx_10_9_universal2 macosx_10_13_universal2 macosx_11_0_universal2" ;;
    macos-x86_64) echo "macosx_10_9_x86_64 macosx_10_12_x86_64 macosx_10_13_x86_64 macosx_11_0_x86_64 macosx_12_0_x86_64 macosx_10_9_universal2 macosx_10_13_universal2 macosx_11_0_universal2" ;;
    windows-x64)  echo "win_amd64" ;;
  esac
}

mkdir -p "$OUT" "$CACHE"
SUMS="$CACHE/SHA256SUMS-$PBS_TAG"
[[ -s "$SUMS" ]] || curl -fsSL "$PBS_BASE/SHA256SUMS" -o "$SUMS"

WHEELS="$(mktemp -d)"
trap 'rm -rf "$WHEELS"' EXIT
log "building the lascodia-mt5-supervisor $VERSION wheel"
"$HOST_PYTHON" -m pip wheel --quiet --no-deps --no-build-isolation -w "$WHEELS" "$REPO" 2>/dev/null \
  || "$HOST_PYTHON" -m pip wheel --quiet --no-deps -w "$WHEELS" "$REPO"
APP_WHEEL="$(ls "$WHEELS"/lascodia_mt5_supervisor-*.whl)"
# Never let a stale build/ or egg-info from the wheel build leak into the repo.
rm -rf "$REPO/build"

for PLAT in "${PLATFORMS[@]}"; do
  TRIPLE="$(pbs_triple "$PLAT")"
  NAME="cpython-$PY_VERSION+$PBS_TAG-$TRIPLE-install_only_stripped.tar.gz"
  TARBALL="$CACHE/$NAME"
  if [[ ! -s "$TARBALL" ]]; then
    log "downloading $NAME"
    curl -fsSL "$PBS_BASE/${NAME//+/%2B}" -o "$TARBALL.part"
    mv "$TARBALL.part" "$TARBALL"
  fi
  WANT="$(awk -v n="$NAME" '$2==n {print $1}' "$SUMS")"
  GOT="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
  if [[ -z "$WANT" || "$WANT" != "$GOT" ]]; then
    echo "checksum mismatch for $NAME (want ${WANT:-<not listed>}, got $GOT)" >&2
    rm -f "$TARBALL"; exit 2
  fi

  WORK="$(mktemp -d)"
  log "$PLAT: unpacking CPython $PY_VERSION"
  tar -xzf "$TARBALL" -C "$WORK"
  if [[ "$PLAT" == windows-* ]]; then
    SITE="$WORK/python/Lib/site-packages"
    STDLIB="$WORK/python/Lib"
  else
    SITE="$WORK/python/lib/python3.12/site-packages"
    STDLIB="$WORK/python/lib/python3.12"
  fi
  # Drop what a headless daemon never needs (and pip: the bundle is immutable; updates replace it).
  rm -rf "$STDLIB/test" "$STDLIB/idlelib" "$STDLIB/tkinter" "$STDLIB/turtledemo" "$STDLIB/ensurepip" \
         "$STDLIB/lib2to3" "$WORK/python/tcl" "$WORK/python/share" \
         "$SITE"/pip "$SITE"/pip-* "$WORK/python/bin/pip"* "$WORK/python/Scripts/pip"* \
         "$WORK/python/bin/idle"* "$WORK/python/bin/pydoc"* "$WORK/python/bin/2to3"*
  # Tcl/Tk libraries sit in python/lib/ only in the POSIX layout.  NEVER glob python/lib/* for a
  # Windows bundle: this Mac's file system ignores case, so python/lib/thread* is python/Lib/threading.py
  # there (0.4.0 shipped a Windows Python without threading and could not start at all).
  if [[ "$PLAT" != windows-* ]]; then
    rm -rf "$WORK/python/lib/tcl"* "$WORK/python/lib/tk"* "$WORK/python/lib/itcl"* "$WORK/python/lib/thread"*
  fi
  for m in threading.py logging/__init__.py json/__init__.py ssl.py asyncio/__init__.py concurrent/futures/__init__.py \
           subprocess.py zipfile/__init__.py tomllib/__init__.py; do
    [[ -f "$STDLIB/$m" ]] || { echo "$PLAT: the bundled Python lost $m while pruning" >&2; exit 2; }
  done
  REQS=(-r "$REPO/tools/bundle-requirements.txt")
  [[ "$PLAT" == windows-* ]] && REQS+=(-r "$REPO/tools/bundle-requirements-windows.txt")
  PLAT_ARGS=()
  for t in $(pip_platforms "$PLAT"); do PLAT_ARGS+=(--platform "$t"); done
  log "$PLAT: installing dependencies"
  "$HOST_PYTHON" -m pip install --quiet --disable-pip-version-check --no-compile --no-deps \
    --only-binary=:all: --implementation cp --python-version 3.12 "${PLAT_ARGS[@]}" \
    --target "$SITE" "${REQS[@]}" "$APP_WHEEL"
  rm -rf "$SITE/bin"   # console-script shims hard-code the build path; the launcher uses -m instead
  find "$WORK" -name '__pycache__' -type d -prune -exec rm -rf {} +

  cp -R "$REPO/launcher" "$WORK/launcher"
  mkdir -p "$WORK/bootstrap"
  cp "$REPO/bootstrap/install.sh" "$REPO/bootstrap/install.ps1" "$WORK/bootstrap/"
  printf '%s\n' "$VERSION" > "$WORK/VERSION"
  printf 'platform=%s\npython=%s\npython_build_standalone=%s\nbuilt=%s\n' \
    "$PLAT" "$PY_VERSION" "$PBS_TAG" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$WORK/BUILD_INFO"
  "$HOST_PYTHON" - "$WORK" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
rows = []
for d, _, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if rel == "MANIFEST.sha256":
            continue
        with open(p, "rb") as fh:
            rows.append((rel, hashlib.sha256(fh.read()).hexdigest()))
with open(os.path.join(root, "MANIFEST.sha256"), "w") as fh:
    for rel, h in sorted(rows):
        fh.write(f"{h}  {rel}\n")
PY

  if [[ "$PLAT" == windows-* ]]; then
    ARCHIVE="$OUT/lascodia-supervisor-$VERSION-$PLAT.zip"
    rm -f "$ARCHIVE"
    "$HOST_PYTHON" - "$WORK" "$ARCHIVE" <<'PY'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            z.write(p, os.path.relpath(p, root).replace(os.sep, "/"))
PY
  else
    ARCHIVE="$OUT/lascodia-supervisor-$VERSION-$PLAT.tar.gz"
    rm -f "$ARCHIVE"
    COPYFILE_DISABLE=1 tar --no-mac-metadata -czf "$ARCHIVE" -C "$WORK" . 2>/dev/null \
      || COPYFILE_DISABLE=1 tar -czf "$ARCHIVE" -C "$WORK" .
  fi
  shasum -a 256 "$ARCHIVE" | awk '{print $1}' > "$ARCHIVE.sha256"
  log "$PLAT: $(basename "$ARCHIVE") $(du -h "$ARCHIVE" | awk '{print $1}') (unpacked $(du -sh "$WORK" | awk '{print $1}')) sha256 $(cat "$ARCHIVE.sha256")"
  rm -rf "$WORK"
done
