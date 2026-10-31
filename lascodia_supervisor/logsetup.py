"""Logging that cannot fill the disk.

The supervisor logs to ``<log_dir>/supervisor.log`` (RotatingFileHandler, 20 MB × 5 by default)
and not to stderr, because launchd's ``StandardErrorPath`` file never rotates (it had reached
70 MB).  Whatever still lands in launchd's stdout/stderr files (a crash before logging is set
up, a native library printing) is trimmed at start-up when it is over the size limit: the last
1 MB is kept in ``<name>.1`` and the file is truncated in place (launchd opens it O_APPEND, so
truncating is safe while it is open).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"
_KEEP_TAIL = 1024 * 1024
_configured_for: Optional[str] = None


def supervisor_log_path(cfg) -> Optional[Path]:
    return Path(cfg.log_dir) / "supervisor.log" if getattr(cfg, "log_dir", None) else None


def trim_launchd_log(path: Path, max_bytes: int) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - _KEEP_TAIL))
            tail = f.read()
        Path(str(path) + ".1").write_bytes(tail)
        os.truncate(path, 0)
        return True
    except OSError:
        return False


def setup_logging(cfg) -> None:
    global _configured_for
    path = supervisor_log_path(cfg)
    key = str(path)
    if _configured_for == key:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler: logging.Handler
    try:
        if path is None:
            raise OSError("no log_dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backup_count,
                                      encoding="utf-8")
        for name in ("stderr.log", "stdout.log"):
            trim_launchd_log(path.parent / name, cfg.log_max_bytes)
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT))
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    # uvicorn's loggers propagate to root once log_config=None; make sure they do not also
    # carry their own stderr handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured_for = key
