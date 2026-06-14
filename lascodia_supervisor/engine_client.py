"""HTTP client for daemon→engine calls — auto-bootstraps its JWT.

The pre-v0.2 client required the operator to mint a JWT manually and
paste it into supervisor.yaml as ``engine_token``.  v0.2+ auto-bootstraps:
on first use the client hits ``POST /admin/terminals/daemons/auth`` with
the daemon's ``daemon_api_key`` and gets back a short-lived JWT, then
uses that for ``register`` / ``heartbeat`` / future calls.  On 401 the
client re-authenticates once before giving up, and it pre-emptively
refreshes when the cached token is within ``_REFRESH_MARGIN_SEC`` of
expiry to avoid mid-request races with the heartbeat loop.

The auth endpoint is TOFU (trust-on-first-use): first daemon to claim
a ``daemon_id`` pins its ``daemon_api_key`` on the engine row; later
auths for the same id must present the same key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from .config import SupervisorConfig

logger = logging.getLogger("lascodia.supervisor.engine")

# Refresh the cached JWT this many seconds before its declared expiry.
# JWTs default to 480-minute TTL engine-side; a 5-minute margin means we
# almost never serve a 401-due-to-expiry to the heartbeat loop.
_REFRESH_MARGIN_SEC = 300


class EngineClient:
    def __init__(self, cfg: SupervisorConfig) -> None:
        self._cfg = cfg

        # Cached JWT + expiry.  Seeded from cfg.engine_token if the
        # operator pre-provisioned one; otherwise empty and the first
        # _ensure_token() call goes through /auth.
        self._token: Optional[str] = cfg.engine_token or None
        self._token_expires_at: Optional[datetime] = None

        # httpx doesn't let us mutate the default Authorization header
        # cleanly after construction, so we build the header per-call
        # in _bearer_headers() instead.
        self._client = httpx.Client(
            base_url=cfg.engine_url.rstrip("/") + "/",
            timeout=15.0,
        )

    # ── lifecycle ────────────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    # ── auth ─────────────────────────────────────────────────────────
    def _bearer_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _token_is_stale(self) -> bool:
        """True when the cached token is missing or within _REFRESH_MARGIN_SEC
        of its declared expiry.  Operator-provisioned tokens (no expiry
        tracked) are never considered stale — they're refreshed only on
        401."""
        if not self._token:
            return True
        if self._token_expires_at is None:
            # Pre-provisioned token — we don't know when it expires.
            return False
        secs_left = (self._token_expires_at - datetime.now(timezone.utc)).total_seconds()
        return secs_left <= _REFRESH_MARGIN_SEC

    def _authenticate(self) -> bool:
        """Exchange daemon_api_key for a fresh JWT.  Returns True on
        success.  Sets self._token + self._token_expires_at."""
        body = {
            "daemonId":     self._cfg.daemon_id,
            "name":         self._cfg.name,
            "baseUrl":      self._cfg.callback_base_url,
            "daemonApiKey": self._cfg.daemon_api_key,
        }
        try:
            # Anonymous call — no Authorization header.
            resp = self._client.post("admin/terminals/daemons/auth", json=body)
            resp.raise_for_status()
            payload = resp.json() or {}
        except httpx.HTTPError as ex:
            logger.error("daemon /auth failed: %s", ex)
            return False

        if not payload.get("status"):
            logger.error("daemon /auth rejected: %s", payload.get("message", "<no message>"))
            return False

        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            logger.error("daemon /auth returned no token in payload: %s", payload)
            return False

        self._token = token
        expires_raw = data.get("expiresAt")
        if expires_raw:
            try:
                # ASP.NET serialises DateTime as ISO 8601 with "Z" suffix.
                # Python 3.11 parses that natively; older versions need a
                # tweak.  Daemon's pyproject pins >= 3.11, so this is safe.
                self._token_expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("couldn't parse expiresAt='%s' — token will refresh on 401 only", expires_raw)
                self._token_expires_at = None
        else:
            self._token_expires_at = None

        logger.info(
            "daemon authenticated; token expires at %s",
            self._token_expires_at.isoformat() if self._token_expires_at else "<unknown>")
        return True

    def _ensure_token(self) -> bool:
        """Lazily authenticate before any request that needs a JWT.  Idempotent
        and cheap when the cached token is still fresh."""
        if not self._token_is_stale():
            return True
        return self._authenticate()

    # ── helpers ──────────────────────────────────────────────────────
    def _request_with_auth(self, method: str, url: str, **kwargs) -> Optional[httpx.Response]:
        """Wrap an httpx call so that:
          1. We ensure a fresh token before sending.
          2. On 401 we re-authenticate ONCE and retry.
          3. All other HTTP errors bubble up as a None return + logged warning."""
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

            logger.info("%s %s returned 401 — re-authenticating and retrying", method, url)
            # Force a re-auth on 401: clear the cached token so
            # _ensure_token has to fetch a fresh one.  This handles the
            # case where the engine restarted with a new JWT secret and
            # invalidated every issued token.
            self._token = None
            self._token_expires_at = None
            if not self._authenticate():
                return resp  # return the 401 so caller sees the failure

        return None  # unreachable but mypy-friendly

    # ── public RPCs ──────────────────────────────────────────────────
    def register(self, installs_payload: list[dict]) -> Optional[dict]:
        """Send the daemon's identity + advertised installs.  Idempotent —
        re-registration overwrites the engine row (including
        InstallsJson, which is what we want as installs change over
        time).  Auth is handled transparently — call this without
        worrying about token freshness."""
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
