"""Self-update (contract C9).

The daemon heartbeat carries ``supervisorVersion`` + ``platform``; the engine answers with
``supervisorUpdate: {version, platform, releaseId, sha256}`` when a newer release exists for that
platform and ``Supervisor:AutoUpdate`` is on.  Then, and only when no job or terminal action is in
flight:

  1. download ``GET platform/supervisor/releases/{releaseId}/binary`` with the daemon token;
  2. verify the archive's sha256, unpack it into ``versions/.staging-<v>``, check every file against
     its ``MANIFEST.sha256`` and that ``VERSION`` says ``<v>``;
  3. smoke-test it: ``<staging>/python … -m lascodia_supervisor version`` must print ``<v>``;
  4. move it to ``versions/<v>``, write ``update.pending`` (from, to, deadline = now + 120 s + restart
     slack), flip ``current``;
  5. exit — launchd (KeepAlive) / the Scheduled Task's launcher loop starts ``current`` again.

The new version removes ``update.pending`` once it has registered with the engine.  If it has not
by the deadline it rolls back itself (flip ``current`` to ``from``, record ``<v>`` in ``update.bad``,
exit); if it cannot even start, the launcher (bin/run.sh / run.ps1) does the same on its next
start after the deadline.  A version in ``update.bad`` is never installed again automatically.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__, layout

logger = logging.getLogger("lascodia.supervisor.update")

# Overridable for the end-to-end test only (it cannot wait 2.5 minutes for a rollback).
CONFIRM_WINDOW_SEC = int(os.environ.get("LASCODIA_UPDATE_CONFIRM_SEC", "120"))
RESTART_SLACK_SEC = int(os.environ.get("LASCODIA_UPDATE_SLACK_SEC", "30"))   # launchd ThrottleInterval (10 s) + start-up
KEEP_VERSIONS = 3


def parse_version(v: str) -> tuple:
    parts = []
    for piece in str(v or "").strip().lstrip("v").split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts) or (0,)


def extract_offer(body: dict) -> Optional[dict]:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    for src in (data if isinstance(data, dict) else {}, body):
        offer = src.get("supervisorUpdate")
        if isinstance(offer, dict) and offer.get("version") and offer.get("releaseId") is not None:
            return offer
    return None


def decide(offer: Optional[dict], current: str, platform: str, bad: set[str], busy: bool,
           enabled: bool = True) -> tuple[bool, str]:
    """Pure: should this offer be installed now?  (yes/no, why)."""
    if not enabled:
        return False, "self-update is off for this install (not a bundle install)"
    if not offer:
        return False, "no update offered"
    v = str(offer.get("version"))
    if str(offer.get("platform") or platform) != platform:
        return False, f"offer is for {offer.get('platform')}, this computer is {platform}"
    if len(str(offer.get("sha256") or "")) != 64:
        return False, "offer has no sha256"
    if parse_version(v) <= parse_version(current):
        return False, f"{v} is not newer than {current}"
    if v in bad:
        return False, f"{v} was rolled back before; not retrying it"
    if busy:
        return False, "a job or terminal action is in progress; will try after it finishes"
    return True, f"updating {current} → {v}"


def verify_manifest(version_dir: Path) -> tuple[bool, str]:
    manifest = version_dir / "MANIFEST.sha256"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False, "MANIFEST.sha256 is missing"
    n = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition("  ")
        p = version_dir / rel
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return False, f"{rel} is missing"
        if h != digest:
            return False, f"{rel} is damaged (checksum mismatch)"
        n += 1
    return (n > 0), f"{n} files verified" if n else "MANIFEST.sha256 is empty"


def unpack(archive: bytes, dest: Path) -> None:
    dest.mkdir(parents=True)
    if archive[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            z.extractall(dest)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as t:
            t.extractall(dest, filter="data")


def smoke_test(version_dir: Path, expect: str) -> tuple[bool, str]:
    py = layout.bundle_python(version_dir)
    try:
        res = subprocess.run([str(py), "-I", "-m", "lascodia_supervisor", "version"], capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        return False, f"new version does not start: {ex}"
    got = (res.stdout or "").strip()
    if res.returncode != 0 or got != expect:
        return False, f"new version failed its start check (exit {res.returncode}, said {got!r})"
    return True, "start check passed"


@dataclass
class UpdateResult:
    ok: bool
    message: str
    restart: bool = False


class Updater:
    def __init__(self, root: Optional[Path], version: str = __version__, platform: Optional[str] = None,
                 download: Optional[Callable[[object], tuple[Optional[bytes], str]]] = None,
                 smoke: Callable[[Path, str], tuple[bool, str]] = smoke_test,
                 clock: Callable[[], float] = time.time, style: Optional[str] = None) -> None:
        self.root = Path(root) if root else None
        self.version = version
        self.platform = platform or layout.platform_tag()
        self._download = download
        self._smoke = smoke
        self._clock = clock
        self._style = style
        self.in_progress = False

    @property
    def enabled(self) -> bool:
        return self.root is not None

    # ── deciding + applying ───────────────────────────────────────────────
    def consider(self, offer: Optional[dict], busy: Callable[[], bool]) -> tuple[bool, str]:
        bad = layout.bad_versions(self.root) if self.root else set()
        return decide(offer, self.version, self.platform, bad, busy() or self.in_progress, self.enabled)

    def apply(self, offer: dict, busy: Callable[[], bool]) -> UpdateResult:
        assert self.root is not None and self._download is not None
        v = str(offer["version"])
        self.in_progress = True
        staging = self.root / "versions" / f".staging-{v}"
        try:
            payload, err = self._download(offer["releaseId"])
            if payload is None:
                return UpdateResult(False, f"could not download supervisor {v}: {err}")
            got = hashlib.sha256(payload).hexdigest()
            if got.lower() != str(offer["sha256"]).lower():
                return UpdateResult(False, f"supervisor {v} download is damaged (sha256 {got} ≠ {offer['sha256']})")
            shutil.rmtree(staging, ignore_errors=True)
            unpack(payload, staging)
            ok, why = verify_manifest(staging)
            if not ok:
                return UpdateResult(False, f"supervisor {v} bundle check failed: {why}")
            bundle_v = (staging / "VERSION").read_text(encoding="utf-8").strip() if (staging / "VERSION").exists() else ""
            if bundle_v != v:
                return UpdateResult(False, f"bundle says version {bundle_v!r}, the engine offered {v}")
            ok, why = self._smoke(staging, v)
            if not ok:
                return UpdateResult(False, why)
            if busy():
                return UpdateResult(False, "a job or terminal action started meanwhile; will try again later")
            final = self.root / "versions" / v
            if final.exists():
                shutil.rmtree(final)
            os.replace(staging, final)
            self.flip(v)
            self.prune()
            return UpdateResult(True, f"supervisor {v} installed; restarting into it", restart=True)
        except Exception as ex:  # an update must never take the running supervisor down
            logger.exception("self-update to %s failed", v)
            return UpdateResult(False, f"self-update to {v} failed: {type(ex).__name__}: {ex}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self.in_progress = False

    def flip(self, to: str) -> None:
        now = int(self._clock())
        frm = layout.read_current(self.root) or self.version
        layout.write_kv(self.root / layout.PENDING, {
            "from": frm, "to": to, "flipped": now,
            "deadline": now + CONFIRM_WINDOW_SEC + RESTART_SLACK_SEC,
        })
        layout.set_current(self.root, to, self._style)
        logger.warning("self-update: current → %s (rolls back to %s unless it registers within %d s)",
                       to, frm, CONFIRM_WINDOW_SEC)

    # ── after the restart ─────────────────────────────────────────────────
    def pending(self) -> dict:
        return layout.read_kv(self.root / layout.PENDING) if self.root else {}

    def confirm(self) -> bool:
        """Called once this version has registered with the engine."""
        p = self.pending()
        if not p or p.get("to") != self.version:
            return False
        layout.write_kv(self.root / layout.LAST, {"version": self.version, "from": p.get("from", ""),
                                                  "confirmed": int(self._clock())})
        try:
            (self.root / layout.PENDING).unlink()
        except OSError:
            pass
        logger.info("self-update to %s confirmed (registered with the engine)", self.version)
        return True

    def check_deadline(self) -> Optional[UpdateResult]:
        """This version is pending and past its deadline without registering → roll back."""
        p = self.pending()
        if not p or p.get("to") != self.version:
            return None
        try:
            deadline = int(p.get("deadline") or 0)
        except ValueError:
            deadline = 0
        if self._clock() < deadline:
            return None
        return self.rollback(p, f"version {self.version} did not register with the engine in time")

    def rollback(self, p: dict, reason: str) -> UpdateResult:
        frm = p.get("from") or ""
        if not frm or not (self.root / "versions" / frm).is_dir():
            # Nothing to go back to: keep running, stop waiting.
            (self.root / layout.PENDING).unlink(missing_ok=True)
            return UpdateResult(False, f"{reason}; previous version {frm!r} is gone, staying on {self.version}")
        layout.set_current(self.root, frm, self._style)
        with open(self.root / layout.BAD, "a", encoding="utf-8") as f:
            f.write(p.get("to", self.version) + "\n")
        os.replace(self.root / layout.PENDING, self.root / layout.ROLLED_BACK)
        logger.error("self-update rolled back to %s: %s", frm, reason)
        return UpdateResult(True, f"rolled back to {frm}: {reason}", restart=True)

    def prune(self, keep: int = KEEP_VERSIONS) -> list[str]:
        vdir = self.root / "versions"
        protect = {layout.read_current(self.root), self.version, self.pending().get("from")}
        dirs = [d for d in vdir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        dirs.sort(key=lambda d: parse_version(d.name), reverse=True)
        removed = []
        for d in dirs[keep:]:
            if d.name in protect:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        return removed
