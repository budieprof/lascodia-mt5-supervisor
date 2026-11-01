"""A tiny stand-in for the engine endpoints the installer and a bundle-installed daemon use (C9)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PREFIX = "/api/v1/lascodia-trading-engine/"


class EngineStub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.bundles: dict[str, tuple[bytes, str, str]] = {}    # platform → (bytes, version, sha)
        self.releases: dict[str, bytes] = {}                     # releaseId → bytes
        self.offer = None
        self.heartbeats: list[dict] = []
        self.registers: list[dict] = []
        self.enrolls: list[dict] = []
        self.lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                u = urlparse(self.path)
                path = u.path[len(PREFIX):] if u.path.startswith(PREFIX) else None
                if path == "onboarding/supervisor/bundle":
                    if self.headers.get("X-Enrollment-Token") != stub.token:
                        return self._send(401, {"status": False, "message": "unknown token"})
                    plat = (parse_qs(u.query).get("platform") or [""])[0]
                    if plat not in stub.bundles:
                        return self._send(404, {"status": False})
                    data, ver, sha = stub.bundles[plat]
                    return self._send(200, data, "application/octet-stream",
                                      {"X-Bundle-Version": ver, "X-Bundle-Sha256": sha})
                if path == "admin/terminals/daemons/jobs":
                    return self._send(404, {"status": False})
                if path and path.startswith("platform/supervisor/releases/") and path.endswith("/binary"):
                    rid = path.split("/")[3]
                    if rid in stub.releases:
                        return self._send(200, stub.releases[rid], "application/octet-stream")
                return self._send(404, {"status": False})

            def do_POST(self):
                u = urlparse(self.path)
                path = u.path[len(PREFIX):] if u.path.startswith(PREFIX) else None
                body = self._json()
                if path == "machine/enroll":
                    stub.enrolls.append(body)
                    if body.get("enrollmentToken") != stub.token:
                        return self._send(401, {"status": False, "message": "used"})
                    return self._send(200, {"status": True, "data": {"credentialId": 41, "credentialBlob": "b" * 48,
                                                                     "tenantId": 1}})
                if path == "machine/token":
                    return self._send(200, {"status": True, "data": {"accessToken": "tok", "expiresInSeconds": 900}})
                if path == "admin/terminals/daemons":
                    stub.registers.append(body)
                    return self._send(200, {"status": True, "data": {"id": 1}})
                if path == "admin/terminals/daemons/heartbeat":
                    with stub.lock:
                        stub.heartbeats.append(body)
                        offer = stub.offer
                    data = {"installs": []}
                    if offer:
                        data["supervisorUpdate"] = offer
                    return self._send(200, {"status": True, "data": data})
                return self._send(404, {"status": False})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{PREFIX.rstrip('/')}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
