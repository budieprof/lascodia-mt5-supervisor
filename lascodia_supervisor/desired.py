"""Per-install desired state the engine sends back on every heartbeat (contract C4).

``{"installs":[{"installId":"exness","desiredState":"Running|Stopped","autoHeal":true}]}``.
An install missing from a response keeps its last known value (persisted, so a supervisor
restart does not forget an operator's Stop).  Never known ⇒ ``Running`` + auto-heal for installs
marked ``managed: true`` in supervisor.yaml, else ``Stopped`` + auto-heal off — and with auto-heal
off the watchdog does nothing at all, so an unmanaged install is simply left alone.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lascodia.supervisor.desired")


def extract_desired(payload) -> Optional[list]:
    """The installs[] list from a heartbeat response, enveloped (``data.installs``) or bare."""
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("data"), payload):
        if isinstance(candidate, dict) and isinstance(candidate.get("installs"), list):
            return candidate["installs"]
    return None


class DesiredStateStore:
    def __init__(self, state_dir: Optional[str]) -> None:
        self._path = Path(state_dir) / "desired_state.json" if state_dir else None
        self._lock = threading.Lock()
        self._known: dict[str, dict] = {}
        if self._path and self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._known = {k: v for k, v in raw.items() if isinstance(v, dict)}
            except (OSError, ValueError) as ex:
                logger.warning("desired state %s unreadable (%s) — using defaults", self._path, type(ex).__name__)

    def get(self, install) -> tuple[str, bool, bool]:
        """(desiredState, autoHeal, explicit)."""
        with self._lock:
            row = self._known.get(install.install_id)
        if row is not None:
            return row.get("desiredState", "Running"), bool(row.get("autoHeal", False)), True
        if getattr(install, "managed", False):
            return "Running", True, False
        return "Stopped", False, False

    def apply(self, rows: Optional[list]) -> list[str]:
        """Merge a heartbeat response; returns the install ids whose desired state changed."""
        if not rows:
            return []
        changed: list[str] = []
        with self._lock:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                iid = r.get("installId")
                state = r.get("desiredState")
                if not iid or state not in ("Running", "Stopped"):
                    continue
                new = {"desiredState": state, "autoHeal": bool(r.get("autoHeal", False)), "updatedAt": time.time()}
                old = self._known.get(iid)
                if old is None or old.get("desiredState") != state or old.get("autoHeal") != new["autoHeal"]:
                    changed.append(iid)
                    logger.info("install %s: engine desired state %s, auto-heal %s", iid, state,
                                "on" if new["autoHeal"] else "off")
                self._known[iid] = new
            if changed:
                self._save_locked()
        return changed

    def _save_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._known, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as ex:
            logger.warning("could not persist desired state: %s", ex)
