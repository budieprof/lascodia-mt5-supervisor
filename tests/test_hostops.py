"""Engine-host operations (gaps program, zero-downtime): gates, the scripts each job runs and how, streamed progress,
status collection — all against temp dirs and fake docker / fake processes. Nothing touches docker or the engine."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lascodia_supervisor import hostops
from lascodia_supervisor.config import SupervisorConfig
from lascodia_supervisor.jobs import JobRunner

IMAGE = "sha256:" + "ab" * 32


class FakePopen:
    """Records argv/env/cwd and replays scripted output + exit code."""
    calls: list = []

    def __init__(self, output: str, rc: int):
        self.output, self.rc = output, rc

    def __call__(self, argv, cwd=None, env=None, **kw):
        FakePopen.calls.append({"argv": argv, "cwd": cwd, "env": env})
        proc = SimpleNamespace(stdout=io.StringIO(self.output), wait=lambda: self.rc, kill=lambda: None)
        return proc


class HostOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "engine"
        (self.repo / "scripts" / "backup").mkdir(parents=True)
        (self.repo / "docker-compose.yml").write_text("services: {}\n")
        self.backups = root / "LascodiaBackups"
        self.bg = root / "bluegreen"
        self.bg.mkdir()
        FakePopen.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, enabled=True, allow_deploy=False):
        return hostops.OpsSettings(enabled, str(self.repo), allow_deploy, str(self.backups), str(self.bg),
                                   caddyfile=str(Path(self.tmp.name) / "Caddyfile"))

    def run_job(self, jtype, params=None, settings=None, output="line 1\nline 2\n", rc=0, sent=None):
        send = (lambda text: sent.append(text) or True) if sent is not None else None
        return hostops.execute({"jobId": "j1", "type": jtype, "params": params or {}}, settings or self.settings(),
                               send, run=lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
                               popen=FakePopen(output, rc))

    # ── gates ─────────────────────────────────────────────────────────────

    def test_everything_is_refused_while_ops_is_off(self):
        for jtype in hostops.HOST_JOB_TYPES:
            status, message, _ = self.run_job(jtype, settings=self.settings(enabled=False))
            self.assertEqual(status, hostops.FAILED)
            self.assertIn("ops.enabled", message)
        self.assertEqual(FakePopen.calls, [])

    def test_engine_repo_must_be_the_engine_checkout(self):
        s = hostops.OpsSettings(True, str(Path(self.tmp.name) / "nowhere"))
        status, message, _ = self.run_job("HostBackupNow", settings=s)
        self.assertEqual(status, hostops.FAILED)
        self.assertIn("docker-compose.yml", message)

    def test_deploy_needs_allow_deploy_on_this_host_too(self):
        status, message, _ = self.run_job("HostDeployApi", {"imageId": IMAGE})
        self.assertEqual(status, hostops.FAILED)
        self.assertIn("allow_deploy", message)
        self.assertEqual(FakePopen.calls, [])

    def test_deploy_rejects_anything_but_a_full_image_id(self):
        status, _, _ = self.run_job("HostDeployApi", {"imageId": "latest"}, settings=self.settings(allow_deploy=True))
        self.assertEqual(status, hostops.FAILED)
        self.assertEqual(FakePopen.calls, [])

    # ── what runs ─────────────────────────────────────────────────────────

    def test_deploy_runs_the_swap_script_pinned_to_the_confirmed_image(self):
        (self.bg / "state").write_text("active=api-green\nhandover_seconds=2\n")
        sent = []
        status, message, data = self.run_job("HostDeployApi", {"imageId": IMAGE},
                                             settings=self.settings(allow_deploy=True), sent=sent)
        self.assertEqual(status, hostops.SUCCEEDED)
        self.assertIn("api-green leads", message)
        call = FakePopen.calls[0]
        self.assertTrue(call["argv"][-1].endswith("scripts/redeploy-api-bluegreen.sh"))
        self.assertEqual(call["env"]["EXPECT_IMAGE_ID"], IMAGE)
        self.assertEqual(call["cwd"], str(self.repo))
        self.assertEqual(data["tail"], ["line 1", "line 2"])
        self.assertIn("line 2", "".join(sent))   # streamed to the engine

    def test_rollback_and_dry_run_flags(self):
        self.run_job("HostDeployApi", {"rollback": True, "dryRun": True}, settings=self.settings(allow_deploy=True))
        argv = FakePopen.calls[0]["argv"]
        self.assertIn("--rollback", argv)
        self.assertIn("--dry-run", argv)
        self.assertNotIn("EXPECT_IMAGE_ID", FakePopen.calls[0]["env"])

    def test_a_refused_swap_reports_failed_with_the_reason(self):
        status, message, data = self.run_job("HostDeployApi", {"imageId": IMAGE}, rc=2,
                                             settings=self.settings(allow_deploy=True))
        self.assertEqual(status, hostops.FAILED)
        self.assertIn("refused", message)
        self.assertEqual(data["exitCode"], 2)

    def test_backup_and_drill_run_their_scripts(self):
        self.assertEqual(self.run_job("HostBackupNow")[0], hostops.SUCCEEDED)
        self.assertTrue(FakePopen.calls[0]["argv"][-1].endswith("scripts/backup/backup-nightly.sh"))
        status, message, _ = self.run_job("HostRestoreDrill", rc=75)
        self.assertEqual(status, hostops.FAILED)
        self.assertIn("heavy-job slot", message)
        self.assertTrue(FakePopen.calls[1]["argv"][-1].endswith("scripts/backup/run-restore-drill.sh"))

    def test_progress_lines_that_could_not_be_sent_are_sent_with_the_next_batch(self):
        attempts = []
        ok = iter([False, True])
        sink = hostops.ProgressSink(lambda text: attempts.append(text) or next(ok), every_sec=0)
        sink.add("a")      # send fails → kept
        sink.add("b")      # sent with "a"
        self.assertEqual(attempts, ["a", "a\nb"])

    # ── status ────────────────────────────────────────────────────────────

    def test_status_reports_state_backups_and_drill_without_secrets(self):
        (self.bg / "state").write_text("active=api-blue\nprevious=api-green\n")
        (self.bg / "history").write_text("\n".join(f"line {i}" for i in range(30)))
        day = self.backups / "db" / "2026" / "11" / "01"
        day.mkdir(parents=True)
        (day / "lascodia-20261101T023000Z.manifest").write_text(
            "format=lascodia-backup-manifest/2\ncreated_utc=20261101T023000Z\nlatest_migration=X\nbytes.engine_dump=100\nbytes.roles_sql=5\n")
        (self.backups / "status").mkdir()
        (self.backups / "status" / "db-last-success").write_text("20261101T023000Z lascodia-20261101T023000Z.manifest\n")
        (self.backups / "status" / "drill-last.json").write_text(json.dumps({"passed": True, "rtoDatabaseSeconds": 912}))
        (self.repo / "scripts" / "backup" / "backup.env").write_text(
            "BACKUP_REMOTE=aws\nBACKUP_REMOTE_URL=s3://b/p\nBACKUP_ENCRYPTION=age\nBACKUP_PASSPHRASE_FILE=/secret\nBACKUP_AGE_IDENTITY=/k\n")
        status, _, data = self.run_job("HostOpsStatus")
        self.assertEqual(status, hostops.SUCCEEDED)
        self.assertEqual(data["bluegreen"]["state"]["active"], "api-blue")
        self.assertEqual(len(data["bluegreen"]["history"]), 20)
        b = data["backups"]
        self.assertEqual(b["newest"]["bytes"], 105)
        self.assertEqual(b["lastDrill"]["rtoDatabaseSeconds"], 912)
        self.assertEqual(b["remoteUrl"], "s3://b/p")
        text = json.dumps(data)
        self.assertNotIn("/secret", text)
        self.assertNotIn("BACKUP_AGE_IDENTITY", text)
        self.assertEqual({c["service"] for c in data["containers"]} >= {"api-blue", "api-green", "engine-gw"}, True)

    # ── wiring ────────────────────────────────────────────────────────────

    def test_job_runner_routes_host_jobs_with_progress(self):
        posted = []
        engine = SimpleNamespace(post_job_progress=lambda jid, text: posted.append((jid, text)) or True)
        runner = JobRunner(lambda: [], None, None, None, engine, lambda: None, ops=lambda: self.settings(enabled=False))
        status, message, _ = runner.execute({"jobId": "j9", "type": "HostOpsStatus", "installId": ""})
        self.assertEqual(status, hostops.FAILED)
        self.assertIn("ops.enabled", message)

    def test_config_parses_ops_and_defaults_off(self):
        base = {"daemon_id": "d", "name": "n", "engine_url": "http://e", "daemon_api_key": "x" * 16}
        self.assertIsNone(SupervisorConfig(**base).ops)
        cfg = SupervisorConfig(**base, ops={"enabled": True, "engine_repo": "/r"})
        self.assertTrue(cfg.ops.enabled)
        self.assertFalse(cfg.ops.allow_deploy)
        s = hostops.OpsSettings.from_config(cfg.ops)
        self.assertEqual(str(s.engine_repo), "/r")


if __name__ == "__main__":
    unittest.main()
