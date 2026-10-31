import unittest

import httpx

from lascodia_supervisor.public_dns import PublicDnsTransport


class Recorder(httpx.BaseTransport):
    def __init__(self):
        self.seen = []

    def handle_request(self, request):
        self.seen.append(request)
        return httpx.Response(200, json={"ok": True})


class PublicDnsTransportTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.lookups = []
        self.answer = (["104.21.12.211"], 60)

        def lookup(name):
            self.lookups.append(name)
            return self.answer

        self.inner = Recorder()
        self.t = PublicDnsTransport(inner=self.inner, lookup=lookup, clock=lambda: self.now)
        self.client = httpx.Client(transport=self.t, base_url="https://app.example.com/api/")

    def test_connects_to_public_ip_but_keeps_hostname_for_tls_and_host_header(self):
        self.client.get("x")
        req = self.inner.seen[0]
        self.assertEqual(req.url.host, "104.21.12.211")
        self.assertEqual(req.extensions["sni_hostname"], "app.example.com")
        self.assertEqual(req.headers["host"], "app.example.com")
        self.assertEqual(req.url.path, "/api/x")

    def test_answer_cached_for_ttl_then_refreshed(self):
        self.client.get("a")
        self.client.get("b")
        self.assertEqual(len(self.lookups), 1)
        self.now += 61
        self.client.get("c")
        self.assertEqual(len(self.lookups), 2)

    def test_stale_answer_used_when_lookup_fails(self):
        self.client.get("a")
        self.now += 61
        self.answer = ([], 0)
        self.client.get("b")
        self.assertEqual(self.inner.seen[-1].url.host, "104.21.12.211")

    def test_no_answer_ever_falls_back_to_system_resolver(self):
        self.answer = ([], 0)
        self.client.get("a")
        self.assertEqual(self.inner.seen[-1].url.host, "app.example.com")
        self.assertNotIn("sni_hostname", self.inner.seen[-1].extensions)

    def test_ip_literal_untouched(self):
        c = httpx.Client(transport=self.t, base_url="http://127.0.0.1:5081/")
        c.get("x")
        self.assertEqual(self.inner.seen[-1].url.host, "127.0.0.1")
        self.assertEqual(self.lookups, [])


if __name__ == "__main__":
    unittest.main()
