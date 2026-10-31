"""Engine job channel (contract C4): execute one job, return ``(status, message, data)``.

Job types: StartTerminal, StopTerminal, RestartTerminal, DeployEaBinary, TailLog, PruneFiles.
The poll/report loop lives in main.py; everything here is synchronous and testable with fakes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

from . import host, prune

logger = logging.getLogger("lascodia.supervisor.jobs")

EA_BINARY_NAME = "LascodiaEA.ex5"
MAX_TAIL_LINES = 2000
MAX_TAIL_BYTES = 1024 * 1024
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")

SUCCEEDED, FAILED = "Succeeded", "Failed"


def sha256_file(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


class Sha256Cache:
    """sha256 of a file, recomputed only when its size / mtime change (heartbeat runs every 30 s)."""

    def __init__(self) -> None:
        self._c: dict[str, tuple[int, float, Optional[str]]] = {}

    def get(self, path: Path) -> str:
        try:
            st = path.stat()
        except OSError:
            return ""
        hit = self._c.get(str(path))
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2] or ""
        digest = sha256_file(path)
        self._c[str(path)] = (st.st_size, st.st_mtime, digest)
        return digest or ""


# ── log tails ────────────────────────────────────────────────────────────────

def tail_file(path: Path, lines: int, utf16: bool) -> str:
    lines = max(1, min(MAX_TAIL_LINES, int(lines or 200)))
    size = path.stat().st_size
    want = min(MAX_TAIL_BYTES * (2 if utf16 else 1), max(4096, lines * (400 if utf16 else 200)))
    start = max(0, size - want)
    if utf16:
        start -= start % 2
    with path.open("rb") as f:
        f.seek(start)
        raw = f.read(size - start)
    if start == 0 and raw[:2] == b"\xff\xfe":
        raw = raw[2:]
    if utf16 and raw and raw.count(0) < len(raw) // 10:
        utf16 = False  # the hint was wrong: this file is 8-bit text
    text = raw[: len(raw) - len(raw) % 2].decode("utf-16-le", errors="replace") if utf16 \
        else raw.decode("utf-8", errors="replace")
    out = text.splitlines()
    if start > 0 and out:
        out = out[1:]  # first line is probably partial
    text = "\n".join(out[-lines:])
    if len(text.encode("utf-8")) > MAX_TAIL_BYTES:
        text = text.encode("utf-8")[-MAX_TAIL_BYTES:].decode("utf-8", errors="ignore")
    return text


def _newest(paths) -> Optional[Path]:
    paths = [p for p in paths if p.is_file()]
    return max(paths, key=lambda p: (p.stat().st_mtime, p.name)) if paths else None


def resolve_log(kind: str, install, instance_id: Optional[str], supervisor_log: Optional[Path]) -> tuple[Optional[Path], bool, str]:
    """(path, utf16, error)."""
    if kind == "supervisor":
        if supervisor_log and supervisor_log.exists():
            return supervisor_log, False, ""
        return None, False, "the supervisor log file does not exist yet"
    if install is None:
        return None, False, "unknown or missing installId"
    if kind == "journal":
        p = host.terminal_dir(install) / "logs"
        found = _newest(x for x in p.glob("*.log") if re.match(r"^\d{8}\.log$", x.name)) if p.is_dir() else None
        return (found, True, "") if found else (None, True, f"no terminal journal in {p}")
    if kind == "ea":
        folder = host.mql5_dir(install) / "Files" / "Lascodia"
        if instance_id:
            if not _INSTANCE_ID_RE.match(instance_id):
                return None, True, "instanceId has characters that are not allowed"
            pattern = f"lascodia_{instance_id}_*.log"
        else:
            pattern = "lascodia_*_*.log"
        found = _newest(folder.glob(pattern)) if folder.is_dir() else None
        return (found, True, "") if found else (None, True, f"no EA log matching {pattern}")
    return None, False, f"unknown log kind '{kind}' (ea, journal or supervisor)"


# ── EA binary deploy ─────────────────────────────────────────────────────────

def deploy_ea_binary(install, payload: bytes, expected_sha: str) -> tuple[bool, str, dict]:
    """Write ``MQL5/Experts/LascodiaEA.ex5`` atomically; keep the previous one as ``.prev``."""
    actual = hashlib.sha256(payload).hexdigest()
    if actual.lower() != expected_sha.lower():
        return False, f"downloaded binary sha256 {actual} does not match the release ({expected_sha}) — nothing written", \
            {"downloadedSha256": actual}
    experts = host.mql5_dir(install) / "Experts"
    if not experts.is_dir():
        return False, f"Experts folder not found: {experts}", {}
    target = experts / EA_BINARY_NAME
    previous = sha256_file(target) if target.exists() else None
    if previous and previous.lower() == actual.lower():
        return True, "this binary is already installed", {"previousSha256": previous, "sha256": actual,
                                                          "path": str(target), "changed": False}
    tmp = experts / (EA_BINARY_NAME + ".new")
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            prev_tmp = experts / (EA_BINARY_NAME + ".prev.new")
            with open(target, "rb") as src, open(prev_tmp, "wb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(prev_tmp, experts / (EA_BINARY_NAME + ".prev"))
        os.replace(tmp, target)
    except OSError as ex:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, f"could not write the EA binary: {ex}", {"previousSha256": previous or ""}
    written = sha256_file(target)
    if (written or "").lower() != actual.lower():
        return False, "the EA binary on disk does not match after writing it", \
            {"previousSha256": previous or "", "sha256": written or ""}
    logger.info("install %s: EA binary deployed %s → %s", install.install_id, previous, actual)
    return True, "EA binary installed (takes effect when the EA is reloaded)", \
        {"previousSha256": previous or "", "sha256": actual, "path": str(target), "changed": True}


# ── dispatcher ───────────────────────────────────────────────────────────────

class JobRunner:
    def __init__(self, get_installs: Callable[[], list], controller, pm, watchdog, engine,
                 supervisor_log: Callable[[], Optional[Path]]) -> None:
        self._get_installs = get_installs
        self.controller = controller
        self.pm = pm
        self.watchdog = watchdog
        self.engine = engine
        self._supervisor_log = supervisor_log

    def _install(self, install_id):
        return next((i for i in self._get_installs() if i.install_id == install_id), None)

    def execute(self, job: dict) -> tuple[str, str, dict]:
        jtype = str(job.get("type") or "")
        params = job.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        install = self._install(job.get("installId"))
        reason = str(params.get("reason") or f"engine job {job.get('jobId')}")[:300]
        try:
            if jtype in ("StartTerminal", "StopTerminal", "RestartTerminal", "DeployEaBinary", "PruneFiles") \
                    and install is None:
                return FAILED, f"this supervisor has no install '{job.get('installId')}'", {}
            if jtype == "StartTerminal":
                res = self.controller.start(install, reason=reason, wait=True)
                if res.ok and not res.data.get("alreadyRunning"):
                    self.watchdog.note_operator_launch(install.install_id)
                self.pm.refresh()
                return (SUCCEEDED if res.ok else FAILED), res.message, res.data
            if jtype == "StopTerminal":
                self.pm.note_stopping(install.install_id)
                res = self.controller.stop(install, reason=reason)
                self.pm.refresh()
                return (SUCCEEDED if res.ok else FAILED), res.message, res.data
            if jtype == "RestartTerminal":
                self.pm.note_stopping(install.install_id)
                res = self.controller.restart(install, reason=reason)
                if res.ok:
                    self.watchdog.note_operator_launch(install.install_id)
                self.pm.refresh()
                return (SUCCEEDED if res.ok else FAILED), res.message, res.data
            if jtype == "DeployEaBinary":
                release_id = params.get("releaseId")
                sha = str(params.get("sha256") or "")
                if not release_id or not _SHA_RE.match(sha):
                    return FAILED, "DeployEaBinary needs releaseId and a 64-character sha256", {}
                payload, err = self.engine.download_release(release_id)
                if payload is None:
                    return FAILED, f"could not download release {release_id}: {err}", {}
                ok, msg, data = deploy_ea_binary(install, payload, sha)
                return (SUCCEEDED if ok else FAILED), msg, data
            if jtype == "TailLog":
                kind = str(params.get("kind") or "ea")
                path, utf16, err = resolve_log(kind, install, params.get("instanceId"), self._supervisor_log())
                if path is None:
                    return FAILED, err, {}
                text = tail_file(path, int(params.get("lines") or 200), utf16)
                return SUCCEEDED, f"{len(text.splitlines())} line(s) from {path.name}", \
                    {"path": str(path), "text": text, "kind": kind}
            if jtype == "PruneFiles":
                dry = params.get("dryRun", True) is not False
                result = prune.run(host.terminal_dir(install), host.mql5_dir(install), dry_run=dry)
                verb = "would free" if dry else "freed"
                amount = result["totalBytes"] if dry else result["freedBytes"]
                return SUCCEEDED, f"{result['fileCount']} file(s), {verb} {amount / 1e6:.1f} MB", result
            return FAILED, f"unknown job type '{jtype}'", {}
        except Exception as ex:  # a job must always get a result
            logger.exception("job %s (%s) crashed", job.get("jobId"), jtype)
            return FAILED, f"supervisor error: {type(ex).__name__}: {ex}"[:500], {}


def utc_iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
