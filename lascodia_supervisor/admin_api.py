"""Daemon admin endpoints — install/operational management.

These endpoints replace the install.sh CLI flags so an operator can drive
the daemon entirely from the admin UI.  Routes are all under
``/admin/...`` and protected by the same ``X-Daemon-Auth`` shared secret
the runtime endpoints use.

Wave 1 surface (this file):
  * ``POST /admin/installs/clone-mt5``  — clone /Applications/MetaTrader 5.app
    to a per-broker bundle with isolated Wine prefix.
  * ``POST /admin/installs/register``    — register a freshly-set-up
    install (after the operator's manual MT5 login dance).
  * ``GET  /admin/installs``             — list installs with their
    full schema (includes wine_binary + extra_args, unlike /installs
    which is the engine-facing summary).
  * ``POST /admin/daemon/reload-config`` — re-read supervisor.yaml without
    restarting the daemon (used internally after YAML mutations; exposed
    so a power user can edit yaml by hand and reload).

Each endpoint mutates supervisor.yaml under a process-wide lock and
calls ``state.reload_config()`` so the change takes effect without a
launchctl kickstart.  YAML mutation goes through PyYAML's safe_dump
which drops comments — operators who hand-curate comments in
supervisor.yaml should not also drive it via the admin UI.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .config import MT5Install, SupervisorConfig, _read_mt5_account_from_common_ini

logger = logging.getLogger("lascodia.supervisor.admin")

# Single global lock guards every supervisor.yaml read-modify-write.
# Multiple concurrent admin requests would otherwise interleave reads
# and produce lost-update races.  The lock is process-wide because the
# daemon is single-process by design (uvicorn workers = 1).
_yaml_lock = threading.Lock()


# ── State binding ────────────────────────────────────────────────────────
#
# The router needs access to the daemon's in-memory state (`STATE` in
# main.py).  We avoid a direct circular import by accepting a "binder"
# object that exposes the minimal surface we need: the auth dependency,
# the current config, and a method to reload config from disk.  main.py
# constructs this and passes it to `build_router()` at startup.


class AdminApiState:
    """Daemon state the admin router needs.  Created in main.py."""

    def __init__(self, get_cfg, reload_config, config_path, get_pm=None):
        self._get_cfg = get_cfg
        self._reload  = reload_config
        self._path    = Path(config_path)
        # ProcessManager getter — lazy because main.py builds the
        # ProcessManager AFTER it builds AdminApiState during a brief
        # startup window.  Optional for the unit-test fixture which
        # doesn't need orphan-scan / kill routes.
        self._get_pm  = get_pm

    @property
    def cfg(self) -> SupervisorConfig:
        return self._get_cfg()

    @property
    def config_path(self) -> Path:
        return self._path

    @property
    def pm(self):
        """ProcessManager — raises if the route was wired without one."""
        if self._get_pm is None:
            raise RuntimeError("AdminApiState was built without a ProcessManager binder; "
                               "/admin/orphans routes are unavailable in this configuration")
        return self._get_pm()

    def reload(self) -> None:
        self._reload()


# ── Request / response models ────────────────────────────────────────────


class CloneMt5Request(BaseModel):
    """Body for POST /admin/installs/clone-mt5."""

    brokerSlug: str = Field(..., min_length=1, max_length=64,
                            description="Lower-case alphanumeric slug used in CFBundleIdentifier + bundle name.")
    sourceAppPath: Optional[str] = Field(
        default=None,
        description="Override the source bundle (default: /Applications/MetaTrader 5.app).")


class CloneMt5Response(BaseModel):
    brokerSlug: str
    bundlePath: str
    expectedExecutable: str
    winePrefix: str
    bundleId: str
    nextSteps: list[str]


class PatchInstallRequest(BaseModel):
    """Body for PATCH /admin/installs/{installId}.

    Every field is optional — only the fields actually present in the
    body get mutated.  Operator picks the property they want to change
    and submits the diff; install_id itself is immutable (rename =
    delete + add).
    """

    name: Optional[str] = None
    extraArgs: Optional[list[str]] = None
    useWine: Optional[bool] = None
    wineBinary: Optional[str] = None
    winePrefix: Optional[str] = None
    brokerName: Optional[str] = None
    accountLogin: Optional[str] = None


class RegisterInstallRequest(BaseModel):
    """Body for POST /admin/installs/register."""

    installId: str = Field(..., min_length=1, max_length=64)
    executable: str = Field(..., min_length=1,
                            description="Absolute path to terminal64.exe of the install to register.")
    name: Optional[str] = Field(default=None, description="Human-readable label; defaults to '<broker> (<slug>)'.")
    useWine: bool = Field(default=True)
    wineBinary: Optional[str] = Field(default=None)
    winePrefix: Optional[str] = Field(
        default=None,
        description="Wine prefix root directory.  When unset, the daemon auto-derives "
                    "from the executable path (parent of `drive_c`).  Critical for "
                    "multi-broker setups: each clone-mt5 broker needs its own prefix "
                    "to avoid colliding on MT5's single-instance lock.")
    extraArgs: list[str] = Field(default_factory=list)


class InstallDto(BaseModel):
    installId: str
    name: str
    executable: str
    useWine: bool
    extraArgs: list[str]
    brokerName: str
    accountLogin: str
    wineBinary: Optional[str]
    winePrefix: Optional[str] = None
    isDefault: bool = Field(
        default=False,
        description="True when this is the canonical source bundle that clone-mt5 "
                    "depends on (wine-mt5-default slug, or any install whose "
                    "executable lives under ~/Library/Application Support/"
                    "net.metaquotes.wine.metatrader5/).  Admin UI hides the "
                    "Remove button for these; DELETE refuses unless ?force=true.")


class OrphanProcessDto(BaseModel):
    """One MT5 terminal64.exe process the daemon is not tracking."""

    pid: int
    name: Optional[str]
    exePath: Optional[str]
    cmdline: str
    startedAt: str
    installHint: Optional[str] = Field(
        default=None,
        description="install_id whose executable path matches this process — null when no match.")


class KillOrphanRequest(BaseModel):
    graceSeconds: int = Field(default=10, ge=0, le=60)
    force: bool = Field(default=False,
                        description="Skip SIGTERM, send SIGKILL immediately.  Use when the terminal is unresponsive.")


class KillOrphanResponse(BaseModel):
    ok: bool
    pid: int
    method: str
    exitedWithinGrace: bool


# ── Helpers ──────────────────────────────────────────────────────────────


def _normalise_slug(raw: str) -> str:
    """Lower-case, strip non-alphanum-or-hyphen, collapse hyphen runs.

    Matches the install.sh ``--clone-mt5`` and ``--add-install`` slug
    derivation so an operator can pass the same value across both
    surfaces without surprise transforms.
    """
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise HTTPException(status_code=500,
                            detail=f"supervisor.yaml missing at {path}; daemon misconfigured")
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


_DEFAULT_SOURCE_PREFIX_REL = "Library/Application Support/net.metaquotes.wine.metatrader5"


def _seed_dest_prefix_cert_store(source_prefix: str, dest_prefix: str,
                                 wine_bin: str) -> int:
    """Transplant the trusted-CA store from ``source_prefix`` into
    ``dest_prefix`` by binary-copying ``system.reg`` straight from
    source to destination.

    **Why a direct file copy?**  Two earlier approaches failed:

    1. **Append cert sections to dest system.reg**: wineserver loads the
       registry on startup, mutates in memory, then rewrites the file
       from its in-memory snapshot on shutdown — silently clobbering
       any out-of-band file append.  Verified live.

    2. **Import via ``wine regedit /S <seed.reg>``**: routes through
       wineserver so the changes are recorded properly, BUT the imported
       cert sections get pruned by wineserver's housekeeping on its
       next launch — apparently because Wine treats imports that arrive
       under HKLM\\Software\\Microsoft\\SystemCertificates as transient
       or duplicates relative to its built-in trust store, and drops
       them.  Verified live: the 170 sections imported via regedit were
       gone after a single wineserver restart cycle.

    Wine's parser DOES accept its own ``system.reg`` format verbatim
    when read from disk on startup, and those entries persist through
    subsequent wineserver cycles.  So the simplest, most reliable
    transplant is just ``cp source/system.reg dest/system.reg`` —
    proven by live test: 159 Root\\Certificates blobs survived a full
    wineserver restart with no loss.

    The downside: we also bring across the source prefix's
    HKEY_LOCAL_MACHINE state — installed-app records, Wine bookkeeping,
    file associations.  For a fresh broker prefix that's actually
    desirable (operator gets a fully-configured machine reg) and
    cosmetic at worst (no broker-specific identity lives in HKLM —
    that's all in user.reg + drive_c/users/*).

    Returns the number of cert blob entries present in the destination
    after seeding.  Idempotent: when the destination already carries
    ≥ 50 Root cert blobs we treat it as already seeded and skip.
    The ``wine_bin`` argument is accepted but unused — kept on the
    signature so future variants (e.g. wineboot-warmup) can use it
    without changing call sites.
    """
    _ = wine_bin  # reserved for future variants
    src_reg = os.path.join(source_prefix, "system.reg")
    if not os.path.isfile(src_reg):
        logger.info("cert seed: source prefix %s has no system.reg — skipping", source_prefix)
        return 0

    with open(src_reg, "r", encoding="utf-8", errors="ignore") as f:
        src_blob_count = sum(1 for line in f if line.startswith('"Blob"=hex:'))
    if src_blob_count == 0:
        logger.info("cert seed: source prefix %s has no cert blobs — skipping", source_prefix)
        return 0

    # Idempotency check.
    dest_reg = os.path.join(dest_prefix, "system.reg")
    if os.path.isfile(dest_reg):
        with open(dest_reg, "r", encoding="utf-8", errors="ignore") as f:
            dest_blob_count = sum(1 for line in f if line.startswith('"Blob"=hex:'))
        if dest_blob_count >= max(50, src_blob_count // 2):
            logger.info("cert seed: dest prefix %s already has %d cert blobs "
                        "(threshold=%d) — already seeded, skipping",
                        dest_prefix, dest_blob_count, max(50, src_blob_count // 2))
            return dest_blob_count

    os.makedirs(dest_prefix, exist_ok=True)

    # Refuse to clobber an existing dest if wineserver is currently
    # holding it open — the write would race with wineserver's flush
    # on shutdown.  Best-effort detection via the dest prefix's
    # `.update-timestamp` file's mtime (wineserver touches it).
    # In practice the operator hasn't launched the cloned bundle yet
    # at clone-mt5 time, so this is academic — but worth being defensive.
    lockfile = os.path.join(dest_prefix, ".update-timestamp")
    if os.path.isfile(lockfile):
        try:
            age = time.time() - os.path.getmtime(lockfile)
            if age < 5:
                logger.warning("cert seed: dest prefix %s has a fresh .update-timestamp "
                               "(wineserver may be running) — skipping to avoid race",
                               dest_prefix)
                return 0
        except OSError:
            pass

    shutil.copyfile(src_reg, dest_reg)
    # Mark mtime so audits can spot daemon-seeded prefixes.
    os.utime(dest_reg, None)
    logger.info("cert seed: copied %d cert blob(s) from %s/system.reg → %s/system.reg",
                src_blob_count, source_prefix, dest_prefix)
    return src_blob_count


def _patch_launcher_bundle_id(launcher_path: str, old_id: bytes, new_id: bytes) -> int:
    """In-place byte-patch every occurrence of ``old_id`` in the
    MetaTrader 5 Mach-O launcher with ``new_id`` (null-padded to the
    same length so subsequent __cstring offsets stay intact).

    Returns the number of occurrences replaced.  Raises ``ValueError``
    when ``new_id`` is longer than ``old_id`` (would corrupt the
    binary) or when zero occurrences are found (binary layout changed
    upstream — the caller should fail loudly).

    Idempotent on re-clone: when the launcher was already patched the
    function finds zero occurrences of ``old_id`` and one or more of
    ``new_id``; we treat that as success and return 0 without
    rewriting the file.
    """
    if len(new_id) > len(old_id):
        raise ValueError(
            f"new bundle id ({new_id!r}, {len(new_id)}B) longer than launcher slot "
            f"({len(old_id)}B); shorten the slug")
    with open(launcher_path, "rb") as f:
        data = f.read()
    occurrences = data.count(old_id)
    if occurrences == 0:
        # Already patched (idempotent re-clone), OR the launcher's
        # binary layout changed upstream.  Distinguish: if the new id
        # is already present we're good; otherwise raise so the
        # operator notices instead of silently shipping a broken
        # bundle.
        if data.count(new_id) > 0:
            logger.info("launcher %s already patched with bundle id %r — skipping rewrite",
                        launcher_path, new_id.decode("ascii"))
            return 0
        raise ValueError(
            f"launcher binary at {launcher_path} does not contain expected hardcoded "
            f"string {old_id!r} — MetaQuotes may have shipped a new build that uses a "
            f"different identifier; daemon's clone-mt5 needs updating to match")
    padded = new_id + b"\x00" * (len(old_id) - len(new_id))
    patched = data.replace(old_id, padded)
    with open(launcher_path, "wb") as f:
        f.write(patched)
    logger.info("byte-patched launcher %s: %d occurrence(s) of %r → %r",
                launcher_path, occurrences, old_id.decode("ascii"),
                new_id.decode("ascii"))
    return occurrences


_DEFAULT_INSTALL_SLUG = "wine-mt5-default"
_DEFAULT_PREFIX_NAME  = "net.metaquotes.wine.metatrader5"


def _is_protected_install(install: dict) -> bool:
    """An install is "protected" — i.e. NOT removable through the admin
    UI's normal delete path — when it's the canonical source bundle
    that ``clone-mt5`` depends on.  Two recognisers:

    1. ``install_id == 'wine-mt5-default'`` — the sentinel slug
       ``install.sh`` assigns to the auto-detected MT5 install at
       initial daemon setup.
    2. The executable lives under the ORIGINAL Wine prefix at
       ``~/Library/Application Support/net.metaquotes.wine.metatrader5/`` —
       catches operator-renamed entries whose slug was changed but
       which still point at the source bundle.

    Removing this install would orphan the daemon and break every
    future ``+ Add broker terminal`` clone (the daemon ``cp -R``'s the
    original ``MetaTrader 5.app`` to seed each broker's clone).  The
    daemon refuses unless ``?force=true`` is passed AND the operator
    accepted the explicit consequences — same escape-hatch pattern
    used elsewhere in the codebase.
    """
    if install.get("install_id") == _DEFAULT_INSTALL_SLUG:
        return True
    exe = install.get("executable") or ""
    expanded_prefix = os.path.expanduser(
        f"~/Library/Application Support/{_DEFAULT_PREFIX_NAME}/")
    return exe.startswith(expanded_prefix)


def _derive_prefix_from_exe(exe_path: str) -> Optional[str]:
    """Walk up from terminal64.exe until a `drive_c` directory is
    found.  The parent of `drive_c` is the Wine prefix root.  Mirrors
    process_manager._derive_wine_prefix; kept duplicated here to avoid
    a back-import that would tangle admin_api ↔ process_manager."""
    p = Path(exe_path).resolve()
    for parent in p.parents:
        if parent.name == "drive_c":
            return str(parent.parent)
    return None


def _save_yaml_atomic(path: Path, data: dict) -> None:
    """Write supervisor.yaml atomically via temp-file + rename.

    PyYAML's safe_dump drops comments — documented in admin_api.py
    module docstring.  ``sort_keys=False`` preserves field order so
    diffs against the example config stay readable.
    """
    tmp = path.with_suffix(path.suffix + ".new")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    # chmod before rename so the new file's permissions match the
    # original (mode 600 — file holds daemon_api_key).
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ── Router builder ───────────────────────────────────────────────────────


def build_router(state: AdminApiState, auth_dep) -> APIRouter:
    """Construct the admin router with the daemon state injected.

    ``auth_dep`` is the same ``require_daemon_auth`` dependency main.py
    uses for the runtime endpoints — passed in so we don't duplicate
    the X-Daemon-Auth check in two places.
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    # ── installs ──────────────────────────────────────────────────────

    @router.get("/installs", response_model=list[InstallDto])
    def list_installs(_: None = Depends(auth_dep)) -> list[InstallDto]:
        """Full install list including wine_binary + extra_args.

        The engine-facing /installs route omits these fields because
        the engine doesn't need them for routing decisions; the admin
        UI does, for the per-row edit form.
        """
        cfg = state.cfg
        return [
            InstallDto(
                installId=i.install_id,
                name=i.name,
                executable=i.executable,
                useWine=i.use_wine,
                extraArgs=list(i.extra_args),
                brokerName=i.broker_name or "",
                accountLogin=i.account_login or "",
                wineBinary=i.wine_binary,
                winePrefix=i.wine_prefix,
                isDefault=_is_protected_install({"install_id": i.install_id,
                                                  "executable": i.executable}),
            )
            for i in cfg.installs
        ]

    @router.post("/installs/clone-mt5", response_model=CloneMt5Response)
    def clone_mt5(req: CloneMt5Request, _: None = Depends(auth_dep)) -> CloneMt5Response:
        """Clone the official MT5 app bundle for a new broker.

        Mirrors install.sh's --clone-mt5 logic in Python:
          1. cp -R the source bundle to /Applications/MetaTrader 5 - <slug>.app
          2. rewrite CFBundleIdentifier to namespace the Wine prefix
          3. strip com.apple.quarantine xattr (inherited from source)
          4. remove signature + ad-hoc re-sign (Gatekeeper requirement
             after modifying Info.plist)

        Does NOT write to supervisor.yaml — that happens in
        /admin/installs/register after the operator's manual MT5
        first-launch + broker login.  The two-step shape mirrors the
        CLI and is necessary because the Wine prefix doesn't exist
        until first launch and broker credentials are interactive.
        """
        slug = _normalise_slug(req.brokerSlug)
        if not slug:
            raise HTTPException(status_code=400,
                                detail="brokerSlug normalises to empty — supply at least one alphanumeric character")

        # MetaQuotes' Mach-O launcher inside the bundle hardcodes the
        # bundle ID (verified live: two occurrences of the string
        # `net.metaquotes.wine.metatrader5` in
        # Contents/MacOS/MetaTrader 5) and uses it — NOT the Info.plist's
        # CFBundleIdentifier — to derive WINEPREFIX.  To get genuine
        # per-broker prefix isolation we MUST byte-patch the launcher.
        #
        # Patch budget: the hardcoded slot is 31 ASCII chars + a null
        # terminator (32 bytes).  Replacing with a LONGER string would
        # shift subsequent __cstring offsets and corrupt the binary,
        # so we cap the new ID at the same length.  Padding with nulls
        # is fine — Wine reads up to the first null.
        #
        # Naming scheme: `net.metaquotes.wine.<slug>` keeps the prefix
        # directory operator-readable (matches what they see in
        # ~/Library/Application Support/) and stays within budget for
        # any slug ≤ 11 chars.  Previous naming
        # `net.metaquotes.wine.metatrader5-<slug>` exceeded the budget
        # AND wasn't being honoured by the launcher anyway, so all
        # historical clones were silently sharing the original prefix.
        _BUNDLE_ID_BASE = "net.metaquotes.wine."
        _OLD_LAUNCHER_ID = b"net.metaquotes.wine.metatrader5"
        _MAX_SLUG_LEN = len(_OLD_LAUNCHER_ID) - len(_BUNDLE_ID_BASE)  # 11
        if len(slug) > _MAX_SLUG_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"brokerSlug '{slug}' is {len(slug)} chars; must be ≤ {_MAX_SLUG_LEN} "
                       f"to fit the Mach-O launcher's hardcoded {len(_OLD_LAUNCHER_ID)}-byte "
                       f"slot.  Pick a shorter slug.")

        source_app = req.sourceAppPath or "/Applications/MetaTrader 5.app"
        target_app = f"/Applications/MetaTrader 5 - {slug}.app"
        new_bundle_id = f"{_BUNDLE_ID_BASE}{slug}"
        new_prefix = os.path.expanduser(f"~/Library/Application Support/{new_bundle_id}")
        expected_exe = f"{new_prefix}/drive_c/Program Files/MetaTrader 5/terminal64.exe"

        if not Path(source_app).is_dir():
            raise HTTPException(status_code=404,
                                detail=f"source bundle not found: {source_app}")

        if Path(target_app).exists():
            # Idempotent re-clone: when the existing bundle's
            # CFBundleIdentifier matches what we would have written,
            # treat this as a successful no-op so the wizard can move
            # past stage 1 without forcing the operator to delete a
            # perfectly good bundle.  This is the common case after a
            # prior clone succeeded but the wizard was closed before
            # stage 3 ran — re-opening the wizard would otherwise
            # 409 indefinitely.  When the identifier mismatches
            # (someone manually copied a different app to that path)
            # keep the hard 409 so we don't silently mis-launch into
            # the wrong prefix.
            try:
                existing_id = subprocess.run(
                    ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier",
                     f"{target_app}/Contents/Info.plist"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
            except subprocess.CalledProcessError as ex:
                stderr = ex.stderr.strip() if ex.stderr else ""
                raise HTTPException(
                    status_code=409,
                    detail=f"target bundle already exists at {target_app} but its Info.plist "
                           f"couldn't be read ({stderr}); inspect it manually or remove and "
                           f"re-clone") from ex

            if existing_id != new_bundle_id:
                # Common case during the v0.x → v0.y migration: pre-fix
                # clones recorded CFBundleIdentifier as
                # `net.metaquotes.wine.metatrader5-<slug>`, but that
                # naming exceeded the Mach-O launcher's 31-byte slot
                # so the launcher hardcode (`net.metaquotes.wine.metatrader5`)
                # was never overridden — meaning the bundle silently
                # ran in the ORIGINAL Wine prefix, defeating per-broker
                # isolation entirely.  Recovery requires deletion + re-
                # clone (next call will use the new naming scheme +
                # byte-patch the launcher).
                legacy_id = f"net.metaquotes.wine.metatrader5-{slug}"
                if existing_id == legacy_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"target bundle at {target_app} is a PRE-FIX clone — "
                               f"its CFBundleIdentifier ({existing_id!r}) used the "
                               f"old naming scheme that the Mach-O launcher silently "
                               f"ignored, so this bundle has been running in the "
                               f"ORIGINAL Wine prefix all along (no actual broker "
                               f"isolation).  Remove it (`rm -rf '{target_app}'`) and "
                               f"call clone-mt5 again; the new path will byte-patch "
                               f"the launcher to use {new_bundle_id!r} for genuine "
                               f"per-broker isolation.")
                raise HTTPException(
                    status_code=409,
                    detail=f"target bundle already exists at {target_app} but its "
                           f"CFBundleIdentifier ({existing_id!r}) doesn't match the "
                           f"expected {new_bundle_id!r} — that's a bundle from somewhere "
                           f"else, not a prior daemon clone.  Remove it first or pick "
                           f"a different slug.")

            # Ensure the launcher is patched too — handles the case
            # where someone restored the original Mach-O binary out-of-
            # band, or where a partial prior clone wrote the plist but
            # failed before patching.  No-op when already patched
            # (_patch_launcher_bundle_id detects existing patches).
            launcher_path = f"{target_app}/Contents/MacOS/MetaTrader 5"
            try:
                _patch_launcher_bundle_id(launcher_path, _OLD_LAUNCHER_ID,
                                          new_bundle_id.encode("ascii"))
            except (FileNotFoundError, ValueError) as ex:
                raise HTTPException(
                    status_code=500,
                    detail=f"existing bundle has plist OK but launcher byte-patch "
                           f"failed: {ex}.  Remove and re-clone.") from ex

            # Idempotent re-clone path also seeds CA certs — covers the
            # case where the operator re-ran clone-mt5 after wiping
            # only the prefix (not the bundle).
            source_prefix_path = os.path.expanduser(f"~/{_DEFAULT_SOURCE_PREFIX_REL}")
            wine_bin = f"{target_app}/Contents/SharedSupport/wine/bin/wine64"
            try:
                _seed_dest_prefix_cert_store(source_prefix_path, new_prefix, wine_bin)
            except (OSError, subprocess.SubprocessError) as ex:
                logger.warning("cert seed failed for idempotent re-clone (slug=%s): %s — "
                               "continuing; operator can manually transplant", slug, ex)

            logger.info("clone-mt5 for slug '%s' is a no-op — bundle at %s already has the "
                        "expected CFBundleIdentifier '%s'.  Returning existing-clone metadata "
                        "so the wizard can proceed.",
                        slug, target_app, existing_id)
            next_steps = [
                f"open '{target_app}'  (or double-click in Finder) — provisions the Wine prefix "
                "(skip if you've already launched it once)",
                "in the new MT5: File → Login to Trade Account, enter broker credentials, tick 'Save account information'",
                "drag LascodiaEA onto a chart; Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL', add http://127.0.0.1:5081",
                "close MT5",
                f"call POST /admin/installs/register with installId='{slug}' and executable='{expected_exe}'",
            ]
            return CloneMt5Response(
                brokerSlug=slug,
                bundlePath=target_app,
                expectedExecutable=expected_exe,
                winePrefix=new_prefix,
                bundleId=new_bundle_id,
                nextSteps=next_steps,
            )

        try:
            # copytree with dirs_exist_ok=False matches `cp -R` to a
            # not-yet-existing destination.  This is the slow step
            # (~10-30 s for a 2-3 GB bundle).  We accept the synchronous
            # blocking — operator clicked a button and is watching the
            # spinner.  Async background would complicate state
            # management for marginal UX gain.
            shutil.copytree(source_app, target_app, symlinks=True)
        except OSError as ex:
            raise HTTPException(status_code=500, detail=f"cp -R failed: {ex}") from ex

        try:
            plist = f"{target_app}/Contents/Info.plist"
            subprocess.run(
                ["/usr/libexec/PlistBuddy", "-c",
                 f"Set :CFBundleIdentifier {new_bundle_id}", plist],
                check=True, capture_output=True)
            try:
                subprocess.run(
                    ["/usr/libexec/PlistBuddy", "-c",
                     f"Set :CFBundleName MetaTrader 5 - {slug}", plist],
                    check=True, capture_output=True)
            except subprocess.CalledProcessError:
                # CFBundleName may not exist; add it.
                subprocess.run(
                    ["/usr/libexec/PlistBuddy", "-c",
                     f"Add :CFBundleName string MetaTrader 5 - {slug}", plist],
                    check=True, capture_output=True)
        except subprocess.CalledProcessError as ex:
            stderr = ex.stderr.decode("utf-8", errors="ignore") if ex.stderr else ""
            raise HTTPException(status_code=500,
                                detail=f"PlistBuddy failed: {stderr}") from ex

        # Byte-patch the Mach-O launcher so it picks up our broker-
        # specific WINEPREFIX.  Without this, the launcher reads its
        # hardcoded `net.metaquotes.wine.metatrader5` and Wine spawns
        # into the ORIGINAL prefix — silently sharing state with the
        # default broker's MT5 (MT5's single-instance lock then kills
        # whichever terminal started first).  See docstring above.
        launcher_path = f"{target_app}/Contents/MacOS/MetaTrader 5"
        try:
            _patch_launcher_bundle_id(launcher_path, _OLD_LAUNCHER_ID,
                                      new_bundle_id.encode("ascii"))
        except FileNotFoundError as ex:
            raise HTTPException(status_code=500,
                                detail=f"launcher binary missing: {launcher_path}") from ex
        except ValueError as ex:
            raise HTTPException(status_code=500,
                                detail=f"launcher byte-patch failed: {ex}") from ex

        # Best-effort xattr strip + ad-hoc resign.  Failures are warnings,
        # not blockers — the bundle can still launch but Gatekeeper may
        # show a one-time prompt.
        subprocess.run(["xattr", "-rd", "com.apple.quarantine", target_app],
                       capture_output=True)
        subprocess.run(["codesign", "--remove-signature", target_app],
                       capture_output=True)
        sign_result = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", target_app],
            capture_output=True)
        if sign_result.returncode != 0:
            logger.warning(
                "codesign --sign - failed for %s: %s",
                target_app, sign_result.stderr.decode("utf-8", errors="ignore"))

        # Pre-seed the destination Wine prefix with the source prefix's
        # CA cert store BEFORE the operator opens the cloned bundle.
        # Without this, MT5's first-time login fails with "Invalid
        # server certificate" because the fresh prefix only ships the
        # default ~12 root CAs and modern brokers (Exness, FXTM, etc.)
        # chain up to roots only present in the accumulated source
        # store.  Routed through `wine regedit` so wineserver persists
        # the import properly — see _seed_dest_prefix_cert_store
        # docstring for why a direct file append doesn't survive.
        # Best-effort: failure is a warning, not a hard error.
        source_prefix_path = os.path.expanduser(f"~/{_DEFAULT_SOURCE_PREFIX_REL}")
        wine_bin = f"{target_app}/Contents/SharedSupport/wine/bin/wine64"
        seeded = 0
        try:
            seeded = _seed_dest_prefix_cert_store(source_prefix_path, new_prefix, wine_bin)
        except (OSError, subprocess.SubprocessError) as ex:
            logger.warning("cert seed failed for slug=%s: %s — operator must manually "
                           "transplant from %s after first launch", slug, ex, source_prefix_path)

        next_steps = [
            f"open '{target_app}'  (or double-click in Finder) — provisions the Wine prefix",
            "in the new MT5: File → Login to Trade Account, enter broker credentials, tick 'Save account information'",
            "drag LascodiaEA onto a chart; Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL', add http://127.0.0.1:5081",
            "close MT5",
            f"call POST /admin/installs/register with installId='{slug}' and executable='{expected_exe}'",
        ]
        logger.info("Cloned MT5 bundle for broker '%s' at %s (cert sections seeded: %d)",
                    slug, target_app, seeded)
        return CloneMt5Response(
            brokerSlug=slug,
            bundlePath=target_app,
            expectedExecutable=expected_exe,
            winePrefix=new_prefix,
            bundleId=new_bundle_id,
            nextSteps=next_steps,
        )

    @router.post("/installs/register", response_model=InstallDto, status_code=status.HTTP_201_CREATED)
    def register_install(req: RegisterInstallRequest,
                         _: None = Depends(auth_dep)) -> InstallDto:
        """Append a new install to supervisor.yaml + reload the daemon.

        Mirrors install.sh's --add-install: reads broker_name +
        account_login from <executable dir>/config/common.ini,
        appends the entry, atomically writes the yaml, calls
        state.reload() so subsequent /installs / launch calls see the
        new entry without a launchctl kickstart.

        Refuses duplicate installId (operator picks a different slug)
        and refuses missing executable (catches typos before the
        engine tries to launch).
        """
        slug = _normalise_slug(req.installId)
        if not slug:
            raise HTTPException(status_code=400, detail="installId normalises to empty")

        exe_path = os.path.abspath(req.executable)
        if not Path(exe_path).is_file():
            raise HTTPException(status_code=404,
                                detail=f"executable not found: {exe_path}")

        # Autofill from common.ini.  Falls back to empty strings —
        # operator can edit the install later via PATCH (wave 2) or
        # re-register after the broker login finishes.
        server, login = _read_mt5_account_from_common_ini(exe_path)
        broker_name = server
        account_login = login
        display_name = req.name or (f"{server} ({slug})" if server else slug)

        # Resolve wine_prefix at register time so it's recorded once
        # and every subsequent launch reads it from yaml.  Order:
        # explicit body field → derive from executable path (parent
        # of `drive_c`).  Multi-broker setups REQUIRE this — see
        # MT5Install.wine_prefix docstring + the matching launch path.
        wine_prefix = req.winePrefix or _derive_prefix_from_exe(exe_path)

        with _yaml_lock:
            data = _load_yaml(state.config_path)
            installs = data.setdefault("installs", [])
            existing = next((i for i in installs if i.get("install_id") == slug), None)
            if existing is not None:
                # Idempotent re-register: when the existing yaml entry
                # already points at the SAME executable path, treat
                # this call as a no-op success — same pattern as
                # clone-mt5's idempotent re-clone.  The wizard's
                # "Retry register" button (and any operator double-
                # click) hits this path; raising 409 would surface a
                # scary error for an operation that already succeeded.
                #
                # When the existing entry points at a DIFFERENT
                # executable, we still 409 — silently rebinding the
                # slug to a different binary would mask an operator
                # mistake.  Recovery is to DELETE the install first
                # then re-register, OR pick a different slug.
                if existing.get("executable") == exe_path:
                    logger.info("register install '%s' is a no-op — already registered with same executable %s",
                                slug, exe_path)
                    return InstallDto(
                        installId=slug,
                        name=existing.get("name", display_name),
                        executable=exe_path,
                        useWine=existing.get("use_wine", req.useWine),
                        extraArgs=list(existing.get("extra_args", req.extraArgs)),
                        brokerName=existing.get("broker_name") or "",
                        accountLogin=existing.get("account_login") or "",
                        wineBinary=existing.get("wine_binary") or req.wineBinary,
                        winePrefix=existing.get("wine_prefix") or wine_prefix,
                        isDefault=_is_protected_install(existing),
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"install_id '{slug}' already exists with a different executable "
                           f"({existing.get('executable')!r}).  Either DELETE the existing install "
                           f"or pick a different installId.")

            new_entry = {
                "install_id":    slug,
                "name":          display_name,
                "executable":    exe_path,
                "use_wine":      req.useWine,
                "extra_args":    list(req.extraArgs),
                "broker_name":   broker_name,
                "account_login": account_login,
            }
            if req.wineBinary:
                new_entry["wine_binary"] = req.wineBinary
            if wine_prefix:
                new_entry["wine_prefix"] = wine_prefix
            installs.append(new_entry)

            _save_yaml_atomic(state.config_path, data)
            state.reload()

        logger.info("Registered install '%s' (executable=%s, broker=%s, login=%s, wine_prefix=%s)",
                    slug, exe_path, broker_name or "<unknown>", account_login or "<unknown>",
                    wine_prefix or "<auto>")
        return InstallDto(
            installId=slug,
            name=display_name,
            executable=exe_path,
            useWine=req.useWine,
            extraArgs=list(req.extraArgs),
            brokerName=broker_name or "",
            accountLogin=account_login or "",
            wineBinary=req.wineBinary,
            winePrefix=wine_prefix,
            isDefault=_is_protected_install({"install_id": slug, "executable": exe_path}),
        )

    @router.delete("/installs/{install_id}", status_code=status.HTTP_200_OK)
    def delete_install(install_id: str,
                       force: bool = False,
                       _: None = Depends(auth_dep)) -> dict:
        """Remove an install entry from supervisor.yaml + reload.

        Refuses if a running terminal session points at this install
        (would orphan the process record).  Operator should close the
        session first via the existing /admin/terminals/sessions/{id}/close.

        Also refuses the canonical source bundle (``wine-mt5-default``
        / any install under the original Wine prefix) unless
        ``?force=true`` is passed — removing it would orphan the
        daemon and break future ``+ Add broker terminal`` clones (the
        clone-mt5 endpoint ``cp -R``'s the original ``MetaTrader 5.app``
        as the seed for each broker).
        """
        with _yaml_lock:
            data = _load_yaml(state.config_path)
            installs = data.get("installs", [])
            idx = next((i for i, e in enumerate(installs)
                        if e.get("install_id") == install_id), -1)
            if idx < 0:
                raise HTTPException(status_code=404,
                                    detail=f"install_id '{install_id}' not found")
            entry = installs[idx]
            if _is_protected_install(entry) and not force:
                raise HTTPException(
                    status_code=409,
                    detail=f"install_id '{install_id}' is the canonical source bundle "
                           f"({entry.get('executable', '?')}).  Removing it would "
                           f"orphan the daemon and break the clone-mt5 path used by "
                           f"'+ Add broker terminal'.  If you really mean to remove "
                           f"it (host migration, manual re-provision), call "
                           f"DELETE /admin/installs/{install_id}?force=true.")
            del installs[idx]
            _save_yaml_atomic(state.config_path, data)
            state.reload()

        if force and _is_protected_install(entry):
            logger.warning("Force-deleted PROTECTED install '%s' (executable=%s) — "
                           "operator must re-provision before clone-mt5 will work again",
                           install_id, entry.get("executable"))
        else:
            logger.info("Deleted install '%s'", install_id)
        return {"ok": True, "installId": install_id, "forced": force and _is_protected_install(entry)}

    @router.patch("/installs/{install_id}", response_model=InstallDto)
    def patch_install(install_id: str,
                      req: PatchInstallRequest,
                      _: None = Depends(auth_dep)) -> InstallDto:
        """Mutate one install entry in supervisor.yaml.

        Only the fields present in the request body get changed.
        Useful when the operator wants to flip use_wine, add an
        extra_args flag (e.g. ``["/portable"]``), or correct a
        manually-typed broker label without re-registering.
        """
        with _yaml_lock:
            data = _load_yaml(state.config_path)
            installs = data.get("installs", [])
            entry = next((e for e in installs
                          if e.get("install_id") == install_id), None)
            if entry is None:
                raise HTTPException(status_code=404,
                                    detail=f"install_id '{install_id}' not found")

            # Pydantic v2: model_dump(exclude_unset=True) returns only
            # the fields the operator actually sent in the body.
            patch = req.model_dump(exclude_unset=True)
            field_map = {
                "name":         "name",
                "extraArgs":    "extra_args",
                "useWine":      "use_wine",
                "wineBinary":   "wine_binary",
                "winePrefix":   "wine_prefix",
                "brokerName":   "broker_name",
                "accountLogin": "account_login",
            }
            for camel, snake in field_map.items():
                if camel in patch:
                    entry[snake] = patch[camel]

            _save_yaml_atomic(state.config_path, data)
            state.reload()

        # Return the post-mutation view.
        cfg = state.cfg
        updated = next(i for i in cfg.installs if i.install_id == install_id)
        logger.info("Patched install '%s': %s", install_id, list(patch.keys()))
        return InstallDto(
            installId=updated.install_id,
            name=updated.name,
            executable=updated.executable,
            useWine=updated.use_wine,
            extraArgs=list(updated.extra_args),
            brokerName=updated.broker_name or "",
            accountLogin=updated.account_login or "",
            wineBinary=updated.wine_binary,
            winePrefix=updated.wine_prefix,
            isDefault=_is_protected_install({"install_id": updated.install_id,
                                              "executable": updated.executable}),
        )

    # ── orphan processes ──────────────────────────────────────────────

    @router.get("/orphans", response_model=list[OrphanProcessDto])
    def list_orphans(_: None = Depends(auth_dep)) -> list[OrphanProcessDto]:
        """List MT5 terminal64.exe processes not tracked by the daemon.

        Includes pre-existing MT5 instances the operator started
        manually before installing the daemon, and any MT5 that crashed
        but left a wine64-preloader→terminal64.exe orphan behind.
        The ``installHint`` tells you which configured install (by path
        match) it belongs to — useful for picking the right "this is
        the stale one to kill" choice when multiple terminals run.
        """
        rows = state.pm.scan_orphans(installs=list(state.cfg.installs))
        return [OrphanProcessDto(**r) for r in rows]

    @router.post("/orphans/{pid}/kill", response_model=KillOrphanResponse)
    def kill_orphan(pid: int,
                    req: KillOrphanRequest = Body(default_factory=KillOrphanRequest),
                    _: None = Depends(auth_dep)) -> KillOrphanResponse:
        """Terminate one orphan MT5 process by PID.

        Refuses tracked-session PIDs (operator must close via the
        regular session endpoint so the state machine stays consistent)
        and refuses non-terminal64 PIDs (guard against the endpoint
        being abused to signal arbitrary host processes).  When
        ``force=true`` the daemon skips SIGTERM and goes straight to
        SIGKILL — appropriate when MT5 is unresponsive.
        """
        try:
            result = state.pm.kill_pid(pid, grace_seconds=req.graceSeconds, force=req.force)
        except LookupError as ex:
            raise HTTPException(status_code=404, detail=str(ex)) from ex
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex
        except PermissionError as ex:
            raise HTTPException(status_code=403, detail=str(ex)) from ex
        return KillOrphanResponse(pid=pid, **result)

    # ── daemon ops ────────────────────────────────────────────────────

    @router.post("/daemon/reload-config", status_code=status.HTTP_200_OK)
    def reload_config(_: None = Depends(auth_dep)) -> dict:
        """Re-read supervisor.yaml without restarting the daemon.

        Useful when an operator edits supervisor.yaml by hand (rare
        post-UI) or after a /admin/installs/register that bypassed the
        in-process state path.  The mutation endpoints above call this
        internally, so manual invocation is only needed for out-of-band
        edits.
        """
        state.reload()
        cfg = state.cfg
        return {
            "ok": True,
            "daemonId": cfg.daemon_id,
            "installCount": len(cfg.installs),
        }

    @router.post("/daemon/restart", status_code=status.HTTP_202_ACCEPTED)
    def restart_daemon(_: None = Depends(auth_dep)) -> dict:
        """Self-terminate so launchd's ``KeepAlive=true`` respawns us.

        Returns 202 IMMEDIATELY then schedules ``os._exit(0)`` for the
        next event-loop tick so the HTTP response actually flushes
        before the process dies.  Without the defer the client sees a
        connection-reset.

        Why exit instead of in-process reload?  The Python interpreter
        can't reload pyc files for already-imported modules; an
        operator who updated the daemon's code on disk (rare in prod,
        common in dev) needs a fresh interpreter.  Exiting → launchd
        respawn → fresh interpreter + fresh code.
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        loop.call_later(0.2, lambda: os._exit(0))
        logger.warning("Daemon restart requested — exiting in 200ms; launchd will respawn")
        return {"ok": True, "scheduledExitInMs": 200}

    @router.post("/daemon/rotate-api-key", status_code=status.HTTP_200_OK)
    def rotate_api_key(_: None = Depends(auth_dep)) -> dict:
        """Generate a fresh daemon_api_key + write it to supervisor.yaml.

        The engine's TOFU rule means the engine's stored apiKey for
        this DaemonId no longer matches → the daemon is locked out of
        future ``/auth`` calls until the operator clears the engine's
        TerminalDaemon row.  The response includes the new key so the
        operator can paste it into the engine's record (or DELETE the
        row and let the next auth re-TOFU).

        Does NOT auto-restart the daemon — the operator might want to
        edit the engine side first before the next heartbeat fails
        with 401.  Combine with /admin/daemon/restart manually when
        ready.
        """
        import secrets
        new_key = secrets.token_urlsafe(32)
        with _yaml_lock:
            data = _load_yaml(state.config_path)
            data["daemon_api_key"] = new_key
            _save_yaml_atomic(state.config_path, data)
            state.reload()
        logger.warning(
            "daemon_api_key rotated — engine TOFU row must be cleared before next /auth succeeds")
        return {
            "ok": True,
            "newApiKey": new_key,
            "remediation": (
                "Delete the engine-side TerminalDaemon row with this DaemonId "
                "(SQL: DELETE FROM \"TerminalDaemon\" WHERE \"DaemonId\" = ...) "
                "or update its DaemonApiKey column to the new value, then "
                "POST /admin/daemon/restart so the daemon re-auths."
            ),
        }

    # ── observability (wave 3 surface) ────────────────────────────────

    @router.get("/logs/tail")
    def tail_logs(lines: int = 200, _: None = Depends(auth_dep)) -> dict:
        """Return the last ``lines`` lines of the daemon's stderr log.

        Stderr (not stdout) because the Python logger writes there by
        default and Uvicorn's request log + our app log both end up
        there under the LaunchAgent.  Bounded read so a huge log
        file doesn't OOM the daemon.
        """
        lines = max(1, min(2000, lines))
        # Same path the LaunchAgent's StandardErrorPath points at.
        # Derived from the config-dir convention rather than read from
        # the plist to keep this resilient to operator-side renames.
        log_path = state.config_path.parent.parent / "logs" / "stderr.log"
        if not log_path.exists():
            return {"path": str(log_path), "lines": [], "available": False}
        # Read the tail by seeking ~ lines * 200 bytes back from EOF —
        # cheap and good enough for the UI's "follow logs" panel.
        with log_path.open("rb") as f:
            try:
                f.seek(0, 2)  # to EOF
                size = f.tell()
                start = max(0, size - lines * 200)
                f.seek(start)
                tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                tail = ""
        log_lines = tail.splitlines()[-lines:]
        return {"path": str(log_path), "lines": log_lines, "available": True}

    @router.get("/config")
    def get_config(_: None = Depends(auth_dep)) -> dict:
        """Return supervisor.yaml as a JSON object with the apiKey redacted.

        Useful for the admin UI's "View config" panel.  The apiKey
        redaction is non-negotiable: the response goes over engine
        proxy + browser, and a leaked key + the network path to the
        daemon = full impersonation.
        """
        data = _load_yaml(state.config_path)
        if "daemon_api_key" in data:
            v = data["daemon_api_key"] or ""
            data["daemon_api_key"] = f"<redacted; length={len(v)}>"
        if data.get("engine_token"):
            v = data["engine_token"]
            data["engine_token"] = f"<redacted; length={len(v)}>"
        return data

    return router
