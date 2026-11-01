"""PruneFiles (contract C6) — keep a terminal's disk from filling, from outside the EA.

Selected for deletion:
  1. terminal journal ``<terminal>/logs/YYYYMMDD.log`` and EA print log ``MQL5/Logs/YYYYMMDD.log``
     older than ``log_days`` (14);
  2. EA dated logs in ``MQL5/Files/Lascodia`` (``bridge_YYYYMMDD.log``,
     ``lascodia_<instance>_YYYYMMDD.log``) older than ``log_days`` — older EA builds never rotate them;
  3. every artifact of an EA instance whose files (and beacon) have not changed for
     ``stale_instance_days`` (30): ``ledger/``, ``slo/``, ``workflows/``, ``incidents/``,
     ``Journal/``, ``beacon/`` and the top-level per-instance files.

Never touched: the newest file of any kind; ``config_override_*``, ``apikey_*``, ``machine_*``,
``enrollment_token*``, ``*.seq``, ``*_state.json``, ``*.tpl`` (templates), anything under
``profiles/``, ``config/``, ``outbox/``, ``retry_queue/``, ``recovery/``, ``deadletter/``; and only
the directories listed above are ever looked at.  ``dry_run`` (the default) lists what would go.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lascodia.supervisor.prune")

DAY = 86400
_DATE_LOG = re.compile(r"^(\d{8})\.log$")
_EA_DATED = re.compile(r"^(bridge|lascodia_.+)_(\d{8})\.log$")
_INSTANCE = re.compile(r"(LASC-[A-Za-z0-9-]+)")
INSTANCE_DIRS = ("ledger", "slo", "workflows", "incidents", "Journal", "beacon")
PROTECTED_GLOBS = ("config_override_*", "apikey_*", "machine_*", "enrollment_token*", "*.seq",
                   "*_state.json", "*.tpl", "*.set", "*.ini")
MAX_LISTED = 500


@dataclass
class Candidate:
    path: Path
    bytes: int
    reason: str


def _protected(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in PROTECTED_GLOBS)


def _kind(path: Path) -> str:
    """Files of one kind differ only by instance id / date: ``ledger/*_events.jsonl``."""
    name = _INSTANCE.sub("*", path.name)
    name = re.sub(r"\d{8}", "D", name)
    return f"{path.parent.name}/{name}"


def _files(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()]
    except OSError:
        return []


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def select(terminal_dir: Path, mql5: Path, now: Optional[float] = None, log_days: int = 14,
           stale_instance_days: int = 30) -> list[Candidate]:
    now = now or time.time()
    picked: dict[Path, Candidate] = {}
    log_cut = now - log_days * DAY

    # 1. journals / print logs
    for folder, why in ((terminal_dir / "logs", "terminal journal"), (mql5 / "Logs", "EA print log")):
        dated = [p for p in _files(folder) if _DATE_LOG.match(p.name)]
        if not dated:
            continue
        newest = max(dated, key=lambda p: p.name)
        for p in dated:
            if p != newest and _mtime(p) < log_cut:
                picked[p] = Candidate(p, _size(p), f"{why} older than {log_days} days")

    lasc = mql5 / "Files" / "Lascodia"
    # 2. EA dated logs
    top = _files(lasc)
    groups: dict[str, list[Path]] = {}
    for p in top:
        m = _EA_DATED.match(p.name)
        if m:
            groups.setdefault(m.group(1), []).append(p)
    for files in groups.values():
        newest = max(files, key=lambda p: p.name)
        for p in files:
            if p != newest and _mtime(p) < log_cut and not _protected(p.name):
                picked[p] = Candidate(p, _size(p), f"EA log older than {log_days} days")

    # 3. stale instances
    by_instance: dict[str, list[Path]] = {}
    candidates = list(top)
    for sub in INSTANCE_DIRS:
        candidates += _files(lasc / sub)
    for p in candidates:
        m = _INSTANCE.search(p.name)
        if m:
            by_instance.setdefault(m.group(1), []).append(p)
    newest_of_kind: dict[str, Path] = {}
    for p in candidates:
        k = _kind(p)
        if k not in newest_of_kind or _mtime(p) > _mtime(newest_of_kind[k]):
            newest_of_kind[k] = p
    keep = set(newest_of_kind.values())
    stale_cut = now - stale_instance_days * DAY
    for inst, files in by_instance.items():
        last = max(_mtime(p) for p in files)
        if last >= stale_cut:
            continue
        for p in files:
            if p in keep or _protected(p.name) or p in picked:
                continue
            picked[p] = Candidate(p, _size(p), f"instance {inst} silent for {stale_instance_days}+ days")
    return sorted(picked.values(), key=lambda c: str(c.path))


def _key(path) -> str:
    import os
    return os.path.normcase(os.path.normpath(str(path)))


def run(terminal_dir: Path, mql5: Path, dry_run: bool = True, now: Optional[float] = None,
        paths: Optional[list] = None) -> dict:
    """Select (and, unless ``dry_run``, delete) what the rules allow.

    ``paths`` = exactly the list an operator reviewed (absolute paths as a dry run returned them
    in ``files[].path``): only those are touched, and each only if it is still in a fresh
    selection right now — inside an allowed folder, not a protected kind, not the newest of its
    kind, still old enough.  The rest are reported in ``skipped`` and left alone."""
    cands = select(terminal_dir, mql5, now)
    skipped: list[dict] = []
    if paths is not None:
        current = {_key(c.path): c for c in cands}
        chosen: dict[str, Candidate] = {}
        for raw in paths:
            text = str(raw or "")
            if not text or not Path(text).is_absolute():
                skipped.append({"path": text, "reason": "not an absolute path"})
                continue
            k = _key(text)
            if k in chosen:
                continue
            c = current.get(k)
            if c is not None:
                chosen[k] = c
            elif not Path(text).exists():
                skipped.append({"path": text, "reason": "already gone"})
            else:
                skipped.append({"path": text, "reason": "no longer allowed to be deleted (protected, the newest "
                                                        "of its kind, recently changed, or outside the prune folders)"})
        cands = sorted(chosen.values(), key=lambda c: str(c.path))
    deleted, failed = 0, []
    freed = 0
    if not dry_run:
        for c in cands:
            try:
                c.path.unlink()
                deleted += 1
                freed += c.bytes
            except FileNotFoundError:
                skipped.append({"path": str(c.path), "reason": "already gone"})
            except OSError as ex:
                failed.append(f"{c.path.name}: {type(ex).__name__}")
        logger.info("prune %s: deleted %d file(s), %d bytes", terminal_dir, deleted, freed)
    total = sum(c.bytes for c in cands)
    out = {
        "dryRun": dry_run,
        "fileCount": len(cands),
        "totalBytes": total,
        "deletedCount": deleted,
        "freedBytes": freed,
        "failures": failed[:50],
        "files": [{"path": str(c.path), "bytes": c.bytes, "reason": c.reason} for c in cands[:MAX_LISTED]],
        "truncated": len(cands) > MAX_LISTED,
    }
    if paths is not None:
        out["requestedCount"] = len(paths)
        out["skipped"] = skipped[:MAX_LISTED]
        out["skippedCount"] = len(skipped)
    return out
