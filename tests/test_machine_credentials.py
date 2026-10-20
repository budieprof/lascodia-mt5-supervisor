"""Machine credentials (engine multi-tenancy Phase 5, EA ADR-0017): the daemon's sign-in ladder,
the 0600 credential file, and the EA enrollment token it drops into terminals it launches.

Run:  python -m unittest discover -s tests   (stdlib unittest; httpx's MockTransport stands in
for the engine, so nothing here touches a network)."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from lascodia_supervisor.config import SupervisorConfig, load_config
from lascodia_supervisor.engine_client import EngineClient
from lascodia_supervisor.machine_credentials import (
    EA_ENROLLMENT_TOKEN_FILE,
    MachineCredentialStore,
    is_enrollment_token,
    prepare_terminal_enrollment,
    terminal_has_machine_credential,
    write_private_file,
    write_terminal_enrollment_token,
)

ENGINE = "http://engine.test/api/v1/lascodia-trading-engine"
DAEMON_TOKEN = "lsc_enr_ab12cd34_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123"
EA_TOKEN = "lsc_enr_zz99yy88_abcdefghijABCDEFGHIJklmnopqrstKLMNOPQRST"
BLOB = "enc3:0a1b2c3d:" + "Q" * 60


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class FakeEngine:
    """Answers like the engine's MachineController and daemon auth, recording every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_answers: list[tuple[int, dict]] = []
        self.enroll_answer: tuple[int, dict] = (200, self.envelope({
            "credentialId": 41, "credentialBlob": BLOB, "tenantId": 7, "tradingAccountId": None,
            "instanceId": "mac-wine", "kind": "Daemon"}))
        self.legacy_answer: tuple[int, dict] = (200, self.envelope({
            "token": "legacy.daemon.token", "expiresAt": "2099-01-01T00:00:00Z", "daemonId": "mac-wine"}))
        self.daemon_enrollment_answer: tuple[int, dict] = (200, self.envelope({
            "id": 5, "token": EA_TOKEN, "expiresAtUtc": "2026-10-21T09:00:00Z", "kind": "EaTerminal"}))

    @staticmethod
    def envelope(data, status=True, message="Successful", code="00") -> dict:
        return {"data": data, "status": status, "message": message, "responseCode": code}

    def token_ok(self, token="machine.access.token", new_blob=None) -> tuple[int, dict]:
        return 200, self.envelope({"accessToken": token, "expiresInSeconds": 900, "tokenType": "Bearer",
                                   "credentialId": 41, "tenantId": 7, "kind": "Daemon",
                                   "newCredentialBlob": new_blob})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.replace("/api/v1/lascodia-trading-engine/", "")
        if path == "machine/token":
            status, body = self.token_answers.pop(0) if self.token_answers else self.token_ok()
        elif path == "machine/enroll":
            status, body = self.enroll_answer
        elif path == "admin/terminals/daemons/auth":
            status, body = self.legacy_answer
        elif path == "machine/daemon/enrollments":
            status, body = self.daemon_enrollment_answer
        elif path in ("admin/terminals/daemons", "admin/terminals/daemons/heartbeat"):
            status, body = 200, self.envelope(None)
        else:
            status, body = 404, {"status": False}
        return httpx.Response(status, json=body)

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("lascodia-trading-engine/", 1)[-1] for r in self.requests]

    def body(self, path: str) -> dict:
        request = next(r for r in self.requests if r.url.path.endswith(path))
        return json.loads(request.content)


class LadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.cred_path = Path(self.dir.name) / "machine_credential.json"
        self.engine = FakeEngine()

    def tearDown(self) -> None:
        self.dir.cleanup()

    def client(self, enrollment_token: str | None = None) -> EngineClient:
        cfg = SupervisorConfig(
            daemon_id="mac-wine", name="Mac", engine_url=ENGINE, daemon_api_key="k" * 32,
            enrollment_token=enrollment_token, credential_path=str(self.cred_path))
        return EngineClient(cfg, transport=httpx.MockTransport(self.engine.handler))

    def store(self) -> MachineCredentialStore:
        return MachineCredentialStore(self.cred_path, "mac-wine", ENGINE)

    def test_enrollment_token_is_exchanged_once_and_the_credential_kept_0600(self) -> None:
        client = self.client(DAEMON_TOKEN)
        self.assertTrue(client._authenticate())

        self.assertEqual(["machine/enroll", "machine/token"], self.engine.paths())
        enroll = self.engine.body("machine/enroll")
        self.assertEqual(DAEMON_TOKEN, enroll["enrollmentToken"])
        self.assertEqual("Daemon", enroll["kind"])
        self.assertEqual("mac-wine", enroll["instanceId"])
        self.assertNotIn("authorization", {k.lower() for k in self.engine.requests[0].headers})
        self.assertTrue(client.machine_session)
        self.assertEqual(0o600, mode_of(self.cred_path))
        stored = self.store().load()
        self.assertEqual((41, BLOB), (stored.credential_id, stored.credential_blob))

    def test_a_stored_credential_signs_in_without_the_token_or_the_key(self) -> None:
        self.store().save(41, BLOB)
        client = self.client(DAEMON_TOKEN)
        self.assertTrue(client._authenticate())
        self.assertEqual(["machine/token"], self.engine.paths())
        self.assertEqual({"credentialId": 41, "credentialBlob": BLOB}, self.engine.body("machine/token"))

    def test_a_revoked_credential_is_dropped_and_the_ladder_continues(self) -> None:
        self.store().save(41, BLOB)
        self.engine.token_answers = [(401, FakeEngine.envelope(None, False, "not valid", "-401"))]
        client = self.client()
        self.assertTrue(client._authenticate())   # falls through to the daemon key
        self.assertFalse(self.cred_path.exists())
        self.assertEqual(["machine/token", "admin/terminals/daemons/auth"], self.engine.paths())
        self.assertFalse(client.machine_session)

    def test_an_engine_without_machine_credentials_keeps_the_credential(self) -> None:
        self.store().save(41, BLOB)
        self.engine.token_answers = [(404, {"status": False})]
        client = self.client(DAEMON_TOKEN)
        self.assertTrue(client._authenticate())
        self.assertTrue(self.cred_path.exists())
        # The enrollment token is not spent on an engine that cannot do machine credentials.
        self.assertEqual(["machine/token", "admin/terminals/daemons/auth"], self.engine.paths())

    def test_refused_for_now_keeps_the_credential_and_stops(self) -> None:
        self.store().save(41, BLOB)
        self.engine.token_answers = [(403, FakeEngine.envelope(None, False, "Organisation 'x' is closed.", "-403"))]
        self.assertFalse(self.client()._authenticate())
        self.assertTrue(self.cred_path.exists())
        self.assertEqual(["machine/token"], self.engine.paths())

    def test_a_refused_enrollment_token_is_not_sent_again(self) -> None:
        self.engine.enroll_answer = (401, FakeEngine.envelope(None, False, "already used", "-401"))
        client = self.client(DAEMON_TOKEN)
        self.assertTrue(client._authenticate())       # daemon key still allowed
        self.assertTrue(client._authenticate())
        self.assertEqual(1, self.engine.paths().count("machine/enroll"))

    def test_retired_daemon_key_sign_in_says_what_to_do(self) -> None:
        self.engine.legacy_answer = (410, FakeEngine.envelope(None, False, "retired", "-410"))
        client = self.client()
        with self.assertLogs("lascodia.supervisor.engine", level="ERROR") as logs:
            self.assertFalse(client._authenticate())
            self.assertFalse(client._authenticate())
        self.assertTrue(client.legacy_retired)
        self.assertEqual(1, self.engine.paths().count("admin/terminals/daemons/auth"))
        self.assertTrue(any("enrollment_token" in line for line in logs.output))

    def test_a_rotated_blob_replaces_the_stored_one(self) -> None:
        self.store().save(41, BLOB)
        rotated = "enc3:0a1b2c3d:" + "R" * 60
        self.engine.token_answers = [self.engine.token_ok(new_blob=rotated)]
        self.assertTrue(self.client()._authenticate())
        self.assertEqual(rotated, self.store().load().credential_blob)

    def test_register_and_heartbeat_carry_the_machine_token(self) -> None:
        self.store().save(41, BLOB)
        client = self.client()
        self.assertIsNotNone(client.register([]))
        self.assertTrue(client.heartbeat([], []))
        register = next(r for r in self.engine.requests if r.url.path.endswith("admin/terminals/daemons"))
        self.assertEqual("Bearer machine.access.token", register.headers["authorization"])
        self.assertEqual(1, self.engine.paths().count("machine/token"))   # one token serves both

    def test_terminal_tokens_need_a_machine_session(self) -> None:
        legacy = self.client()
        self.assertIsNone(legacy.request_terminal_enrollment_token("5001", "Broker-Demo", "x"))
        self.assertNotIn("machine/daemon/enrollments", self.engine.paths())

        self.store().save(41, BLOB)
        enrolled = self.client()
        issued = enrolled.request_terminal_enrollment_token("5001", "Broker-Demo", "mac-wine/demo")
        self.assertEqual(EA_TOKEN, issued["token"])
        body = self.engine.body("machine/daemon/enrollments")
        self.assertEqual({"kind": "EaTerminal", "accountLogin": "5001", "brokerServer": "Broker-Demo",
                          "label": "mac-wine/demo"}, body)


class CredentialFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "nested" / "machine_credential.json"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_private_files_are_0600_even_over_a_wider_temp_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("old")
        os.chmod(tmp, 0o644)
        write_private_file(self.path, "secret")
        self.assertEqual(0o600, mode_of(self.path))
        self.assertEqual("secret", self.path.read_text())
        self.assertFalse(tmp.exists())

    def test_a_credential_of_another_daemon_or_engine_is_ignored_not_deleted(self) -> None:
        MachineCredentialStore(self.path, "other-daemon", ENGINE).save(41, BLOB)
        self.assertIsNone(MachineCredentialStore(self.path, "mac-wine", ENGINE).load())
        self.assertIsNone(MachineCredentialStore(self.path, "other-daemon", "http://elsewhere/api").load())
        self.assertTrue(self.path.exists())

    def test_malformed_files_are_no_credential(self) -> None:
        self.path.parent.mkdir(parents=True)
        for text in ("not json", "{}", json.dumps({"credentialId": 0, "credentialBlob": BLOB}),
                     json.dumps({"credentialId": 41, "credentialBlob": "short"})):
            self.path.write_text(text)
            self.assertIsNone(MachineCredentialStore(self.path, "mac-wine", ENGINE).load(), text)

    def test_the_config_default_keeps_the_credential_next_to_supervisor_yaml(self) -> None:
        config = Path(self.dir.name) / "supervisor.yaml"
        config.write_text("daemon_id: d\nname: n\nengine_url: http://e\ndaemon_api_key: '" + "k" * 32 + "'\n")
        os.environ["LASCODIA_SUPERVISOR_CONFIG"] = str(config)
        try:
            cfg = load_config()
        finally:
            del os.environ["LASCODIA_SUPERVISOR_CONFIG"]
        self.assertEqual(str(Path(self.dir.name) / "machine_credential.json"), cfg.credential_path)
        self.assertIsNone(cfg.enrollment_token)


class TerminalEnrollmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "MetaTrader 5"
        (self.root / "MQL5" / "Files" / "Lascodia").mkdir(parents=True)
        self.install = SimpleNamespace(install_id="demo", executable=str(self.root / "terminal64.exe"),
                                       account_login="5001", broker_name="Broker-Demo", mql5_dir=None)
        self.engine = SimpleNamespace(calls=[], request_terminal_enrollment_token=self.issue)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def issue(self, login, server, label):
        self.engine.calls.append((login, server, label))
        return {"token": EA_TOKEN, "expiresAtUtc": "2026-10-21T09:00:00Z"}

    def token_file(self) -> Path:
        return self.root / "MQL5" / "Files" / "Lascodia" / EA_ENROLLMENT_TOKEN_FILE

    def test_the_token_lands_where_the_ea_reads_it_0600(self) -> None:
        path = prepare_terminal_enrollment(self.engine, "mac-wine", self.install)
        self.assertEqual(self.token_file(), path)
        self.assertEqual([("5001", "Broker-Demo", "mac-wine/demo")], self.engine.calls)
        self.assertEqual(0o600, mode_of(path))
        lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
        self.assertEqual([EA_TOKEN], lines)   # the EA reads the first non-comment line

    def test_no_token_when_a_sibling_already_holds_a_credential(self) -> None:
        (self.root / "MQL5" / "Files" / "Lascodia" / "machine_5001_123_LASC-X.txt").write_text("v1\n1\nblob\n")
        self.assertTrue(terminal_has_machine_credential(self.root / "MQL5", "5001"))
        self.assertIsNone(prepare_terminal_enrollment(self.engine, "mac-wine", self.install))
        self.assertEqual([], self.engine.calls)

    def test_no_token_without_an_account_or_a_data_folder(self) -> None:
        self.assertIsNone(prepare_terminal_enrollment(
            self.engine, "mac-wine", SimpleNamespace(**{**vars(self.install), "account_login": None})))
        self.assertIsNone(prepare_terminal_enrollment(
            self.engine, "mac-wine", SimpleNamespace(**{**vars(self.install), "mql5_dir": "/nonexistent/MQL5"})))
        self.assertEqual([], self.engine.calls)

    def test_only_enrollment_tokens_are_written(self) -> None:
        self.assertTrue(is_enrollment_token(EA_TOKEN))
        self.assertFalse(is_enrollment_token("lsc_pat_ab12cd34_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123"))
        with self.assertRaises(ValueError):
            write_terminal_enrollment_token(self.root / "MQL5", "not-a-token", daemon_id="d", account="a", expires_at_utc=None)


if __name__ == "__main__":
    unittest.main()
