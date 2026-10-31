"""Reach the engine by its PUBLIC address even when the host pins it to a LAN address.

Why: on a second Mac the engine name is pinned in /etc/hosts to the engine laptop's LAN IP
(a fast path the EAs use). macOS 15 "Local Network" privacy blocks LAN connections from a
launchd *agent* until someone clicks Allow on that machine's screen — every request then fails
with ``[Errno 65] No route to host``. The supervisor does not need the fast path (one heartbeat
every 30 s), so with ``engine_resolver: public`` it resolves the engine name through public
DNS-over-HTTPS and connects over the internet, which that privacy rule does not cover.

TLS is still verified against the real hostname (``sni_hostname``) and the Host header keeps the
real name, so this is the same request the system resolver would make, routed differently.
If public resolution fails and no earlier answer is cached, the request falls back to the
system resolver unchanged.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Callable, Optional

import httpx

logger = logging.getLogger("lascodia.supervisor.dns")

DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",      # Cloudflare (JSON API)
    "https://8.8.8.8/resolve",        # Google (JSON API)
)
MIN_TTL_SEC = 30
MAX_TTL_SEC = 300


def doh_lookup(name: str, client: Optional[httpx.Client] = None, timeout: float = 5.0) -> tuple[list[str], int]:
    """A records for ``name`` via DNS-over-HTTPS → (addresses, ttl). Empty list when every endpoint fails."""
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        for url in DOH_ENDPOINTS:
            try:
                resp = client.get(url, params={"name": name, "type": "A"},
                                  headers={"accept": "application/dns-json"})
                if resp.status_code != 200:
                    continue
                answers = [a for a in (resp.json().get("Answer") or []) if a.get("type") == 1]
                ips = []
                for a in answers:
                    try:
                        ips.append(str(ipaddress.IPv4Address(str(a.get("data", "")).strip())))
                    except ValueError:
                        continue
                if ips:
                    ttl = min(int(a.get("TTL", MAX_TTL_SEC)) for a in answers)
                    return ips, max(MIN_TTL_SEC, min(MAX_TTL_SEC, ttl))
            except (httpx.HTTPError, ValueError) as ex:
                logger.debug("DoH %s for %s failed: %s", url, name, ex)
        return [], 0
    finally:
        if own:
            client.close()


class PublicDnsTransport(httpx.BaseTransport):
    """Rewrites each request to a publicly-resolved IPv4 address of its host, keeping TLS/Host on the name."""

    def __init__(self, inner: Optional[httpx.BaseTransport] = None,
                 lookup: Callable[[str], tuple[list[str], int]] = doh_lookup,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._lookup = lookup
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, float]] = {}   # host → (ip, expires_at)
        self._warned = False

    def _resolve(self, host: str) -> Optional[str]:
        try:
            ipaddress.ip_address(host)
            return None                       # already an IP literal
        except ValueError:
            pass
        now = self._clock()
        with self._lock:
            hit = self._cache.get(host)
        if hit and hit[1] > now:
            return hit[0]
        ips, ttl = self._lookup(host)
        if ips:
            with self._lock:
                self._cache[host] = (ips[0], now + ttl)
            self._warned = False
            return ips[0]
        if hit:                               # stale but last known good beats the LAN pin
            return hit[0]
        if not self._warned:
            logger.warning("public DNS lookup of %s failed; using the system resolver", host)
            self._warned = True
        return None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ip = self._resolve(host)
        if ip:
            request.extensions = {**request.extensions, "sni_hostname": host}
            request.url = request.url.copy_with(host=ip)
            if "host" not in request.headers:
                request.headers["Host"] = host
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()
