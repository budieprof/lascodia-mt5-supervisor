"""engine_resolver: auto — switch to public DNS only for a LAN address that is blocked."""
from __future__ import annotations

import errno
import unittest

import httpx

from lascodia_supervisor.config import SupervisorConfig
from lascodia_supervisor.resolver import AutoResolverTransport, connect_errno, is_lan_address, transport_for


class _Failing(httpx.BaseTransport):
    def __init__(self, err: int):
        self.err, self.calls = err, 0

    def handle_request(self, request):
        self.calls += 1
        try:
            raise OSError(self.err, "boom")
        except OSError as os_err:
            raise httpx.ConnectError(f"[Errno {self.err}] boom") from os_err


class _Ok(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        return httpx.Response(200, json={"ok": True})


def client(inner, addrs):
    public = _Ok()
    t = AutoResolverTransport(inner=inner, public_factory=lambda: public, resolve=lambda h, p: addrs)
    return httpx.Client(transport=t, base_url="https://engine.example.com/api/"), t, public


class AutoResolverTests(unittest.TestCase):
    def test_private_ehostunreach_switches_for_good(self):
        inner = _Failing(errno.EHOSTUNREACH)
        c, t, public = client(inner, ["192.168.1.20"])
        self.assertEqual(c.get("x").status_code, 200)
        self.assertTrue(t.switched)
        c.get("y")
        self.assertEqual((inner.calls, public.calls), (1, 2))   # never goes back to the system path

    def test_other_switch_errnos_and_link_local(self):
        for err, addr in ((errno.ENETUNREACH, "10.0.0.5"), (errno.EPERM, "169.254.10.1"), (errno.EHOSTUNREACH, "fd00::1")):
            with self.subTest(err=err, addr=addr):
                c, t, _ = client(_Failing(err), [addr])
                self.assertEqual(c.get("x").status_code, 200)
                self.assertTrue(t.switched)

    def test_public_address_failure_does_not_switch(self):
        c, t, _ = client(_Failing(errno.EHOSTUNREACH), ["93.184.216.34"])
        with self.assertRaises(httpx.ConnectError):
            c.get("x")
        self.assertFalse(t.switched)

    def test_refused_or_loopback_does_not_switch(self):
        for err, addr in ((errno.ECONNREFUSED, "192.168.1.20"), (errno.EHOSTUNREACH, "127.0.0.1")):
            with self.subTest(err=err, addr=addr):
                c, t, _ = client(_Failing(err), [addr])
                with self.assertRaises(httpx.ConnectError):
                    c.get("x")
                self.assertFalse(t.switched)

    def test_ip_literal_never_switches(self):
        inner = _Failing(errno.EHOSTUNREACH)
        t = AutoResolverTransport(inner=inner, public_factory=_Ok, resolve=lambda h, p: ["192.168.1.20"])
        with self.assertRaises(httpx.ConnectError):
            httpx.Client(transport=t).get("http://192.168.1.20:5081/x")
        self.assertFalse(t.switched)

    def test_success_stays_on_system_path(self):
        ok = _Ok()
        c, t, public = client(ok, ["192.168.1.20"])
        c.get("x")
        self.assertFalse(t.switched)
        self.assertEqual((ok.calls, public.calls), (1, 0))

    def test_helpers(self):
        self.assertEqual(connect_errno(httpx.ConnectError("[Errno 65] No route to host")), 65)
        self.assertTrue(is_lan_address("172.16.4.4"))
        self.assertFalse(is_lan_address("8.8.8.8"))
        self.assertFalse(is_lan_address("::1"))

    def test_default_is_auto(self):
        cfg = SupervisorConfig(daemon_id="d", name="n", engine_url="http://x/api", daemon_api_key="k" * 20)
        self.assertEqual(cfg.engine_resolver, "auto")
        self.assertIsInstance(transport_for("auto"), AutoResolverTransport)
        self.assertIsNone(transport_for("system"))
