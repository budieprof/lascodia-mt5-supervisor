"""``engine_resolver: auto`` — use the system resolver, fall back to public DNS when the LAN is blocked.

Why: an organisation's second Mac may pin the engine name in /etc/hosts to a LAN address (a fast
path for the EAs).  macOS 15 "Local Network" privacy blocks LAN connections from a launchd agent
until someone clicks Allow on that Mac, and every request then fails with ``[Errno 65] No route
to host`` (or ``EPERM``).  ``auto`` starts with the system resolver; the first time a connection
to a private / link-local address fails with no-route / network-unreachable / not-permitted, it
switches to :class:`~lascodia_supervisor.public_dns.PublicDnsTransport` for the rest of the
process, logs that once, and retries the request.  A failure against a public address, or any
other error (refused, timeout, TLS), never switches — those are not this problem.
"""
from __future__ import annotations

import errno
import ipaddress
import logging
import re
import socket
import threading
from typing import Callable, Optional

import httpx

logger = logging.getLogger("lascodia.supervisor.dns")

# EHOSTUNREACH / ENETUNREACH / EPERM, plus the Winsock numbers for the first two.
SWITCH_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPERM, 10065, 10051})
_ERRNO_RE = re.compile(r"\[Errno (\d+)\]|\[WinError (\d+)\]")


def connect_errno(ex: BaseException) -> Optional[int]:
    """The OS error number behind an httpx/httpcore connect error, if one can be found."""
    seen: set[int] = set()
    cur: Optional[BaseException] = ex
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno:
            return cur.errno
        m = _ERRNO_RE.search(str(cur))
        if m:
            return int(m.group(1) or m.group(2))
        cur = cur.__cause__ or cur.__context__
    return None


def _system_addresses(host: str, port: int) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except (OSError, UnicodeError):
        return []


def is_lan_address(addr: str) -> bool:
    """Private (RFC 1918 / ULA) or link-local — what Local Network privacy covers.  Loopback is not."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    return ip.is_private or ip.is_link_local


class AutoResolverTransport(httpx.BaseTransport):
    def __init__(self, inner: Optional[httpx.BaseTransport] = None,
                 public_factory: Optional[Callable[[], httpx.BaseTransport]] = None,
                 resolve: Callable[[str, int], list[str]] = _system_addresses) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._public_factory = public_factory or _default_public
        self._resolve = resolve
        self._public: Optional[httpx.BaseTransport] = None
        self._lock = threading.Lock()

    @property
    def switched(self) -> bool:
        return self._public is not None

    def _should_switch(self, request: httpx.Request, ex: httpx.ConnectError) -> bool:
        code = connect_errno(ex)
        if code not in SWITCH_ERRNOS:
            return False
        host = request.url.host
        try:
            ipaddress.ip_address(host)
            return False   # an IP literal: public DNS cannot give a different answer
        except ValueError:
            pass
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        return any(is_lan_address(a) for a in self._resolve(host, port))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        public = self._public
        if public is not None:
            return public.handle_request(request)
        try:
            return self._inner.handle_request(request)
        except httpx.ConnectError as ex:
            if not self._should_switch(request, ex):
                raise
            with self._lock:
                if self._public is None:
                    self._public = self._public_factory()
                    logger.warning(
                        "cannot reach %s at its local-network address (%s) — this Mac is probably blocking "
                        "local-network access for background services; using its public address from now on",
                        request.url.host, ex)
            return self._public.handle_request(request)

    def close(self) -> None:
        self._inner.close()
        if self._public is not None:
            self._public.close()


def _default_public() -> httpx.BaseTransport:
    from .public_dns import PublicDnsTransport
    return PublicDnsTransport()


def transport_for(mode: str) -> Optional[httpx.BaseTransport]:
    """The httpx transport for an ``engine_resolver`` value (None = httpx default)."""
    if mode == "public":
        return _default_public()
    if mode == "auto":
        return AutoResolverTransport()
    return None
