"""Machine credentials for the supervisor daemon (engine multi-tenancy Phase 5, EA ADR-0017).

The daemon enrolls once with a single-use enrollment token (``enrollment_token`` in
supervisor.yaml) and keeps the credential the engine issues — its id and an opaque blob
(the secret, encrypted by the engine) — in a file only its user can read (0600). It trades
that credential at ``POST /machine/token`` for 15-minute access tokens.

When it launches a terminal for an account, it asks the engine for an EA-terminal enrollment
token bound to that account and drops it where the EA looks for one
(``<MQL5>/Files/Lascodia/enrollment_token.txt``), so the EA enrolls itself. Nothing here talks
HTTP; the engine client does.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lascodia.supervisor.machine")

#: Name of the file the EA reads an enrollment token from (EA ENROLLMENT_TOKEN_FILE).
EA_ENROLLMENT_TOKEN_FILE = "enrollment_token.txt"

_ENROLLMENT_TOKEN_RE = re.compile(r"^lsc_enr_[a-z0-9]{8}_[A-Za-z0-9]{40}$")
_BLOB_MIN_LEN = 32


@dataclass(frozen=True)
class MachineCredential:
    credential_id: int
    credential_blob: str
    daemon_id: str
    engine_url: str


def is_enrollment_token(value: Optional[str]) -> bool:
    """Shape check only (``lsc_enr_<8>_<40>``) — the engine decides whether it is valid."""
    return bool(value) and bool(_ENROLLMENT_TOKEN_RE.match(value.strip()))


def write_private_file(path: Path, text: str) -> None:
    """Write ``text`` so that only this user can read it (0600), atomically.

    The temp file is created with mode 0600 from the start (``os.open``) — never written
    first and narrowed afterwards — flushed and fsynced, then renamed over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # A pre-existing temp file keeps its old mode through O_CREAT; make sure.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class MachineCredentialStore:
    """The daemon's credential file. One credential per (daemon id, engine URL)."""

    def __init__(self, path: Path, daemon_id: str, engine_url: str) -> None:
        self._path = Path(path)
        self._daemon_id = daemon_id
        self._engine_url = engine_url.rstrip("/")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[MachineCredential]:
        """The stored credential, or None when there is none, it is unreadable, or it belongs
        to another daemon id / engine (that file is left alone, not deleted)."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as ex:
            logger.warning("machine credential file %s unreadable (%s) — ignoring it", self._path, type(ex).__name__)
            return None

        try:
            credential = MachineCredential(
                credential_id=int(raw["credentialId"]),
                credential_blob=str(raw["credentialBlob"]),
                daemon_id=str(raw.get("daemonId", "")),
                engine_url=str(raw.get("engineUrl", "")).rstrip("/"),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("machine credential file %s is malformed — ignoring it", self._path)
            return None

        if credential.credential_id <= 0 or len(credential.credential_blob) < _BLOB_MIN_LEN:
            logger.warning("machine credential file %s holds no usable credential — ignoring it", self._path)
            return None
        if credential.daemon_id != self._daemon_id or credential.engine_url != self._engine_url:
            logger.warning(
                "machine credential file %s is for daemon %r at %s, not %r at %s — ignoring it",
                self._path, credential.daemon_id, credential.engine_url, self._daemon_id, self._engine_url)
            return None
        return credential

    def save(self, credential_id: int, credential_blob: str) -> MachineCredential:
        credential = MachineCredential(credential_id, credential_blob, self._daemon_id, self._engine_url)
        write_private_file(self._path, json.dumps({
            "credentialId": credential.credential_id,
            "credentialBlob": credential.credential_blob,
            "daemonId": credential.daemon_id,
            "engineUrl": credential.engine_url,
            "savedAtUtc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        }, indent=2) + "\n")
        logger.info("machine credential %d stored in %s (0600)", credential_id, self._path)
        return credential

    def delete(self) -> None:
        try:
            self._path.unlink()
            logger.warning("machine credential file %s deleted (the engine no longer accepts it)", self._path)
        except FileNotFoundError:
            pass


def host_fingerprint() -> str:
    """A short, non-secret description of this host for the engine's records."""
    host = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]
    return f"supervisor-host-{host}"


# ── Terminal enrollment ──────────────────────────────────────────────────────

def resolve_mql5_dir(executable: str, explicit: Optional[str]) -> Optional[Path]:
    """The terminal's MQL5 data directory: ``mql5_dir`` from the install entry, else
    ``<directory of terminal64.exe>/MQL5`` (portable installs and the macOS Wine layout).
    None when neither exists — set ``mql5_dir`` for a terminal whose data lives elsewhere."""
    if explicit:
        candidate = Path(os.path.expanduser(explicit))
        return candidate if candidate.is_dir() else None
    candidate = Path(executable).parent / "MQL5"
    return candidate if candidate.is_dir() else None


def terminal_has_machine_credential(mql5_dir: Path, account_login: str) -> bool:
    """True when an EA in this terminal already holds a machine credential for the account
    (``Files/Lascodia/machine_<login>_*.txt``): its siblings get theirs from it, so no token is needed."""
    folder = mql5_dir / "Files" / "Lascodia"
    safe_login = re.sub(r"[^A-Za-z0-9._-]", "_", account_login)
    return any(folder.glob(f"machine_{safe_login}_*.txt"))


def write_terminal_enrollment_token(mql5_dir: Path, token: str, *, daemon_id: str, account: str,
                                    expires_at_utc: Optional[str]) -> Path:
    """Drop ``token`` where the EA reads it (0600). Comment lines say where it came from."""
    if not is_enrollment_token(token):
        raise ValueError("not an enrollment token")
    path = mql5_dir / "Files" / "Lascodia" / EA_ENROLLMENT_TOKEN_FILE
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    text = (
        f"# Written by lascodia-mt5-supervisor (daemon {daemon_id}) at {now} for account {account}.\n"
        f"# Single-use EA enrollment token; the EA deletes this file once it has used it."
        f"{' Expires ' + expires_at_utc + '.' if expires_at_utc else ''}\n"
        f"{token.strip()}\n"
    )
    write_private_file(path, text)
    return path


def prepare_terminal_enrollment(engine, daemon_id: str, install) -> Optional[Path]:
    """Before launching ``install``: when no EA in that terminal holds a machine credential for
    its account yet, ask the engine for an EA-terminal enrollment token bound to the account and
    drop it where the EA reads it. Returns the file written, or None (nothing needed / possible).
    Never raises into the launch: a terminal without a token still signs in with an API key while
    the engine allows it, or through a sibling's credential."""
    login = (getattr(install, "account_login", None) or "").strip()
    server = (getattr(install, "broker_name", None) or "").strip()
    if not login or not server:
        logger.info("install %s: no account_login/broker_name — no enrollment token prepared", install.install_id)
        return None

    mql5_dir = resolve_mql5_dir(install.executable, getattr(install, "mql5_dir", None))
    if mql5_dir is None:
        logger.warning("install %s: MQL5 data directory not found — set mql5_dir to let its EA enroll itself",
                       install.install_id)
        return None
    if terminal_has_machine_credential(mql5_dir, login):
        return None

    issued = engine.request_terminal_enrollment_token(login, server, label=f"{daemon_id}/{install.install_id}")
    if not issued:
        return None
    try:
        path = write_terminal_enrollment_token(
            mql5_dir, issued["token"], daemon_id=daemon_id, account=f"{login}@{server}",
            expires_at_utc=issued.get("expiresAtUtc"))
    except (OSError, ValueError) as ex:
        logger.error("install %s: could not write the enrollment token (%s)", install.install_id, type(ex).__name__)
        return None
    logger.info("install %s: enrollment token for %s@%s written to %s (0600)", install.install_id, login, server, path)
    return path
