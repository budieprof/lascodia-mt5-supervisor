"""HTTP client for daemon→engine calls — signs itself in.

Sign-in ladder (engine multi-tenancy Phase 5, EA ADR-0017):

1. **Machine credential** (``credential_path``, 0600): ``POST /machine/token`` with the stored
   credential id + blob → a 15-minute access token. Refreshed the same way before it expires
   (no refresh tokens). 401 means the engine no longer accepts the credential: the file is
   deleted and the ladder continues.
2. **Enrollment token** (``enrollment_token`` in supervisor.yaml, single use): ``POST
   /machine/enroll`` with kind ``Daemon`` and the ``daemon_id`` → a credential, stored with mode
   0600, then step 1. A refused token is not sent again until the daemon restarts.
3. **Legacy daemon key** (``daemon_api_key``): ``POST /admin/terminals/daemons/auth`` — the
   trust-on-first-use path, while the engine still allows it. HTTP 410 means the engine retired
   it: the daemon then needs an enrollment token.

On 401 from any engine call the client signs in again once before giving up, and it refreshes
pre-emptively when the cached token is within ``_REFRESH_MARGIN_SEC`` of expiry.

An enrolled daemon (machine session) can also ask for EA-terminal enrollment tokens for its own
organisation (``request_terminal_enrollment_token``), which it drops into a terminal it launches.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import SupervisorConfig, default_config_path
from .machine_credentials import (
    MachineCredential,
    MachineCredentialStore,
    host_fingerprint,
    is_enrollment_token,
)

logger = logging.getLogger("lascodia.supervisor.engine")

# Refresh the cached token this many seconds before its declared expiry. Machine tokens live
# 900 s, so they renew after 600 s; legacy daemon tokens (480 min) after ~475 min.
_REFRESH_MARGIN_SEC = 300

# What a machine-credential answer means for the ladder.
_OK, _DEAD, _STOP, _FALLBACK = "ok", "dead", "stop", "fallback"


def _classify(status_code: int) -> str:
    """401 = refused for good (drop it); 404 / 408 / 429 / 5xx = the engine cannot do machine
    credentials now (old engine, outage) — keep it, let the legacy key try; anything else (403
    account or organisation refused, 409) = keep it, stop this attempt."""
    if status_code == 401:
        return _DEAD
    if status_code == 404 or status_code >= 500 or status_code in (408, 429):
        return _FALLBACK
    return _STOP


class EngineClient:
    def __init__(self, cfg: SupervisorConfig, transport: Optional[httpx.BaseTransport] = None) -> None:
        self._cfg = cfg

        # Cached access token + expiry.  Seeded from cfg.engine_token if the operator
        # pre-provisioned one; otherwise empty and the first _ensure_token() signs in.
        self._token: Optional[str] = cfg.engine_token or None
        self._token_expires_at: Optional[datetime] = None
        self._machine_session = False
        self._legacy_retired = False
        self._enrollment_spent = False

        credential_path = Path(cfg.credential_path) if cfg.credential_path \
            else default_config_path().parent / "machine_credential.json"
        self._credentials = MachineCredentialStore(credential_path, cfg.daemon_id, cfg.engine_url)

        # httpx doesn't let us mutate the default Authorization header cleanly after
        # construction, so we build the header per-call in _bearer_headers() instead.
        self._client = httpx.Client(
            base_url=cfg.engine_url.rstrip("/") + "/",
            timeout=15.0,
            transport=transport,
        )

    # ── lifecycle ────────────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    @property
    def machine_session(self) -> bool:
        """True when the current token came from this daemon's machine credential."""
        return self._machine_session

    @property
    def legacy_retired(self) -> bool:
        return self._legacy_retired

    # ── auth ─────────────────────────────────────────────────────────
    def _bearer_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _token_is_stale(self) -> bool:
        """True when the cached token is missing or within _REFRESH_MARGIN_SEC of its declared
        expiry.  Operator-provisioned tokens (no expiry tracked) are refreshed only on 401."""
        if not self._token:
            return True
        if self._token_expires_at is None:
            return False
        secs_left = (self._token_expires_at - datetime.now(timezone.utc)).total_seconds()
        return secs_left <= _REFRESH_MARGIN_SEC

    def _authenticate(self) -> bool:
        """Run the sign-in ladder. Returns True with self._token set."""
        legacy_only = False

        credential = self._credentials.load()
        if credential is not None:
            outcome = self._machine_token(credential)
            if outcome == _OK:
                return True
            if outcome == _STOP:
                return False
            if outcome == _DEAD:
                self._credentials.delete()
            else:
                legacy_only = True

        if not legacy_only and self._cfg.enrollment_token and not self._enrollment_spent:
            outcome = self._enroll()
            if outcome == _OK:
                return True
            if outcome == _FALLBACK:
                legacy_only = True

        if self._legacy_retired:
            logger.error(
                "daemon has no machine credential and the engine retired daemon-key sign-in — set "
                "enrollment_token in supervisor.yaml (a Daemon token from the console) and restart")
            return False
        return self._legacy_authenticate()

    def _machine_token(self, credential: MachineCredential) -> str:
        body = {"credentialId": credential.credential_id, "credentialBlob": credential.credential_blob}
        try:
            # Anonymous by design: the credential IS the proof.
            resp = self._client.post("machine/token", json=body)
        except httpx.HTTPError as ex:
            logger.warning("machine/token transport error: %s", type(ex).__name__)
            return _FALLBACK

        payload = _json(resp)
        if resp.status_code == 200 and payload.get("status"):
            data = payload.get("data") or {}
            token = data.get("accessToken")
            if not token:
                logger.error("machine/token answered without an access token")
                return _FALLBACK
            self._token = token
            self._token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(data.get("expiresInSeconds") or 900))
            self._machine_session = True
            new_blob = data.get("newCredentialBlob")
            if new_blob and new_blob != credential.credential_blob:
                # Rotation, or the engine re-encrypted the secret under a new key: keep the new one.
                self._credentials.save(credential.credential_id, new_blob)
            logger.info("daemon signed in with machine credential %d", credential.credential_id)
            return _OK

        outcome = _classify(resp.status_code)
        logger.warning("machine/token refused credential %d (HTTP %d, %s): %s",
                       credential.credential_id, resp.status_code, outcome, payload.get("message", "<no message>"))
        return outcome

    def _enroll(self) -> str:
        token = (self._cfg.enrollment_token or "").strip()
        if not is_enrollment_token(token):
            logger.error("enrollment_token in supervisor.yaml is not an lsc_enr_ token — ignoring it")
            self._enrollment_spent = True
            return _DEAD

        body = {
            "enrollmentToken": token,
            "kind": "Daemon",
            "instanceId": self._cfg.daemon_id,
            "terminalFingerprint": host_fingerprint(),
        }
        try:
            resp = self._client.post("machine/enroll", json=body)
        except httpx.HTTPError as ex:
            logger.warning("machine/enroll transport error: %s", type(ex).__name__)
            return _FALLBACK

        payload = _json(resp)
        data = payload.get("data") or {}
        if resp.status_code == 200 and payload.get("status") and data.get("credentialId") and data.get("credentialBlob"):
            credential = self._credentials.save(int(data["credentialId"]), str(data["credentialBlob"]))
            logger.info("daemon %s enrolled (credential %d, organisation %s)",
                        self._cfg.daemon_id, credential.credential_id, data.get("tenantId"))
            return self._machine_token(credential)

        outcome = _classify(resp.status_code)
        if outcome != _FALLBACK:
            # Used, expired, revoked, wrong kind, another organisation's daemon id: never again.
            self._enrollment_spent = True
            logger.error("enrollment refused (HTTP %d): %s — ask for a new Daemon enrollment token",
                         resp.status_code, payload.get("message", "<no message>"))
            return _DEAD
        logger.warning("enrollment unavailable (HTTP %d) — will retry", resp.status_code)
        return _FALLBACK

    def _legacy_authenticate(self) -> bool:
        """Exchange daemon_api_key for a token (trust-on-first-use; retired by the engine with 410)."""
        body = {
            "daemonId":     self._cfg.daemon_id,
            "name":         self._cfg.name,
            "baseUrl":      self._cfg.callback_base_url,
            "daemonApiKey": self._cfg.daemon_api_key,
        }
        try:
            # Anonymous call — no Authorization header.
            resp = self._client.post("admin/terminals/daemons/auth", json=body)
        except httpx.HTTPError as ex:
            logger.error("daemon /auth failed: %s", ex)
            return False

        payload = _json(resp)
        if resp.status_code == 410:
            self._legacy_retired = True
            logger.error("daemon-key sign-in retired by the engine: %s", payload.get("message", "<no message>"))
            return False
        if resp.status_code != 200:
            logger.error("daemon /auth failed: HTTP %d %s", resp.status_code, payload.get("message", ""))
            return False
        if not payload.get("status"):
            logger.error("daemon /auth rejected: %s", payload.get("message", "<no message>"))
            return False

        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            logger.error("daemon /auth returned no token")
            return False

        self._token = token
        self._machine_session = False
        expires_raw = data.get("expiresAt")
        if expires_raw:
            try:
                # ASP.NET serialises DateTime as ISO 8601 with "Z" suffix (Python >= 3.11 parses it).
                self._token_expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("couldn't parse expiresAt='%s' — token will refresh on 401 only", expires_raw)
                self._token_expires_at = None
        else:
            self._token_expires_at = None

        logger.info(
            "daemon authenticated with its daemon key; token expires at %s",
            self._token_expires_at.isoformat() if self._token_expires_at else "<unknown>")
        return True

    def _ensure_token(self) -> bool:
        """Sign in lazily before any request that needs a token.  Cheap when the token is fresh."""
        if not self._token_is_stale():
            return True
        return self._authenticate()

    # ── helpers ──────────────────────────────────────────────────────
    def _request_with_auth(self, method: str, url: str, **kwargs) -> Optional[httpx.Response]:
        """Wrap an httpx call so that:
          1. We ensure a fresh token before sending.
          2. On 401 we sign in again ONCE and retry.
          3. Transport errors become a None return + logged warning."""
        if not self._ensure_token():
            return None

        for attempt in (1, 2):
            headers = {**kwargs.pop("headers", {}), **self._bearer_headers()}
            try:
                resp = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as ex:
                logger.warning("%s %s transport error: %s", method, url, ex)
                return None

            if resp.status_code != 401 or attempt == 2:
                return resp

            logger.info("%s %s returned 401 — signing in again and retrying", method, url)
            # Force a fresh sign-in: the engine restarted with a new secret, the token was
            # revoked, or the credential was rotated out from under us.
            self._token = None
            self._token_expires_at = None
            self._machine_session = False
            if not self._authenticate():
                return resp  # return the 401 so caller sees the failure

        return None  # unreachable but mypy-friendly

    # ── public RPCs ──────────────────────────────────────────────────
    def register(self, installs_payload: list[dict]) -> Optional[dict]:
        """Send the daemon's identity + advertised installs.  Idempotent —
        re-registration overwrites the engine row (including
        InstallsJson, which is what we want as installs change over
        time).  Auth is handled transparently."""
        body = {
            "daemonId":     self._cfg.daemon_id,
            "name":         self._cfg.name,
            "baseUrl":      self._cfg.callback_base_url,
            "daemonApiKey": self._cfg.daemon_api_key,
            "installs":     installs_payload,
        }
        resp = self._request_with_auth("POST", "admin/terminals/daemons", json=body)
        if resp is None:
            return None
        try:
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as ex:
            logger.error("register failed: %s", ex)
            return None

    def heartbeat(self, sessions_payload: list[dict],
                  installs_payload: Optional[list[dict]] = None) -> bool:
        body = {
            "daemonId": self._cfg.daemon_id,
            "sessions": sessions_payload,
        }
        # Heartbeat also carries the current installs list so the engine's
        # InstallsJson cache picks up out-of-band changes (operator edits
        # supervisor.yaml directly, /admin/installs/{id} DELETE/PATCH) in
        # ≤heartbeat_interval_sec.  Engine accepts the field as optional
        # for backward-compat — old daemons that don't send it keep
        # whatever installs were last registered.
        if installs_payload is not None:
            body["installs"] = installs_payload
        resp = self._request_with_auth("POST", "admin/terminals/daemons/heartbeat", json=body)
        if resp is None:
            return False
        try:
            resp.raise_for_status()
            return True
        except httpx.HTTPError as ex:
            logger.warning("heartbeat failed: %s", ex)
            return False

    def request_terminal_enrollment_token(self, account_login: str, broker_server: str,
                                          label: str) -> Optional[dict]:
        """An EA-terminal enrollment token bound to ``account_login@broker_server`` in this daemon's
        organisation (``POST /machine/daemon/enrollments``) — ``{token, expiresAtUtc, ...}`` — or None.
        Needs a machine session: a legacy daemon-key token belongs to no organisation and the
        engine refuses it."""
        if not self._ensure_token():
            return None
        if not self._machine_session:
            logger.info("not requesting a terminal enrollment token: the daemon is not enrolled (no machine credential)")
            return None
        resp = self._request_with_auth("POST", "machine/daemon/enrollments", json={
            "kind": "EaTerminal",
            "accountLogin": account_login,
            "brokerServer": broker_server,
            "label": label[:128],
        })
        if resp is None:
            return None
        payload = _json(resp)
        data = payload.get("data") or {}
        if resp.status_code == 200 and payload.get("status") and is_enrollment_token(data.get("token")):
            return data
        logger.warning("terminal enrollment token refused (HTTP %d): %s",
                       resp.status_code, payload.get("message", "<no message>"))
        return None


def _json(resp: httpx.Response) -> dict:
    try:
        value = resp.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
