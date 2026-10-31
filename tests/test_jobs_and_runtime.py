"""Job execution (temp dirs), prune selection, journal signatures, the C4 heartbeat payload, the
desired-state round trip, the engine job-channel calls and log hygiene — all against fakes."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from fakes import FakeHost, make_layout

from lascodia_supervisor import prune
from lascodia_supervisor.beacons import iso_utc
from lascodia_supervisor.config import SupervisorConfig
from lascodia_supervisor.desired import DesiredStateStore, extract_desired
from lascodia_supervisor.engine_client import EngineClient
from lascodia_supervisor.jobs import FAILED, SUCCEEDED, deploy_ea_binary, resolve_log, tail_file
from lascodia_supervisor.journal import JournalWatcher, count_signatures
from lascodia_supervisor.logsetup import trim_launchd_log
from lascodia_supervisor.runtime import Supervisor
from lascodia_supervisor.wake import WakeDetector

ENGINE = "http://engine.test/api/v1/lascodia-trading-engine"
DAY = 86400


def utf16(text: str) -> bytes:
    return b"\xff\xfe" + text.encode("utf-16-le")


def age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = make_layout(Path(self.tmp.name), "exness")
        self.target = self.lay.mql5 / "Experts" / "LascodiaEA.ex5"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_atomically_keeps_prev_and_reports_both_shas(self):
        self.target.write_bytes(b"old")
        new = b"new-binary"
        ok, msg, data = deploy_ea_binary(self.lay.install, new, hashlib.sha256(new).hexdigest())
        self.assertTrue(ok, msg)
        self.assertEqual(self.target.read_bytes(), new)
        self.assertEqual((self.target.parent / "LascodiaEA.ex5.prev").read_bytes(), b"old")
        self.assertEqual(data["previousSha256"], hashlib.sha256(b"old").hexdigest())
        self.assertEqual(data["sha256"], hashlib.sha256(new).hexdigest())
        self.assertFalse((self.target.parent / "LascodiaEA.ex5.new").exists())

    def test_sha_mismatch_writes_nothing(self):
        self.target.write_bytes(b"old")
        ok, msg, _ = deploy_ea_binary(self.lay.install, b"tampered", "0" * 64)
        self.assertFalse(ok)
        self.assertIn("does not match", msg)
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_same_binary_is_a_noop_success(self):
        self.target.write_bytes(b"same")
        ok, msg, data = deploy_ea_binary(self.lay.install, b"same", hashlib.sha256(b"same").hexdigest())
        self.assertTrue(ok)
        self.assertFalse(data["changed"])

    def test_first_install_without_previous(self):
        ok, _, data = deploy_ea_binary(self.lay.install, b"x", hashlib.sha256(b"x").hexdigest())
        self.assertTrue(ok)
        self.assertEqual(data["previousSha256"], "")


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = make_layout(Path(self.tmp.name), "exness")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ea_log_utf16_decoded_and_bounded(self):
        folder = self.lay.mql5 / "Files" / "Lascodia"
        (folder / "lascodia_LASC-1_20261030.log").write_bytes(utf16("old\n"))
        p = folder / "lascodia_LASC-1_20261031.log"
        p.write_bytes(utf16("".join(f"line {i}\n" for i in range(5000))))
        path, is16, err = resolve_log("ea", self.lay.install, "LASC-1", None)
        self.assertEqual((path, is16, err), (p, True, ""))
        text = tail_file(path, 3, True)
        self.assertEqual(text.splitlines(), ["line 4997", "line 4998", "line 4999"])
        self.assertEqual(len(tail_file(path, 999999, True).splitlines()), 2000)

    def test_journal_newest_day(self):
        (self.lay.term / "logs" / "20261030.log").write_bytes(utf16("a\n"))
        (self.lay.term / "logs" / "20261031.log").write_bytes(utf16("b\nc\n"))
        path, _, _ = resolve_log("journal", self.lay.install, None, None)
        self.assertEqual(path.name, "20261031.log")
        self.assertEqual(tail_file(path, 10, True), "b\nc")

    def test_instance_id_path_traversal_refused(self):
        _, _, err = resolve_log("ea", self.lay.install, "../../etc", None)
        self.assertIn("not allowed", err)

    def test_supervisor_log_is_utf8(self):
        p = Path(self.tmp.name) / "supervisor.log"
        p.write_text("x\ny\n", encoding="utf-8")
        path, is16, _ = resolve_log("supervisor", None, None, p)
        self.assertEqual(tail_file(path, 1, is16), "y")


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = make_layout(Path(self.tmp.name), "exness")
        self.lasc = self.lay.mql5 / "Files" / "Lascodia"
        for d in ("ledger", "slo", "workflows", "outbox", "config"):
            (self.lasc / d).mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, rel: str, days: float, size: int = 10) -> Path:
        p = self.lay.term / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        age(p, days)
        return p

    def test_selection_rules(self):
        old_journal = self.mk("logs/20260901.log", 60)
        new_journal = self.mk("logs/20261031.log", 0)
        only_old_mql_log = self.mk("MQL5/Logs/20260801.log", 90)       # newest of its kind → kept
        old_bridge = self.mk("MQL5/Files/Lascodia/bridge_20260501.log", 120)
        newest_bridge = self.mk("MQL5/Files/Lascodia/bridge_20260502.log", 119)
        stale_ledger = self.mk("MQL5/Files/Lascodia/ledger/LASC-OLD-111_events.jsonl", 45, 1000)
        stale_seq = self.mk("MQL5/Files/Lascodia/ledger/LASC-OLD-111_events.seq", 45)
        stale_state = self.mk("MQL5/Files/Lascodia/workflows/LASC-OLD-111_workflow_state.json", 45)
        stale_slo = self.mk("MQL5/Files/Lascodia/slo/LASC-OLD-111_slo_history.jsonl", 45)
        live_ledger = self.mk("MQL5/Files/Lascodia/ledger/LASC-LIVE-222_events.jsonl", 0)
        live_slo = self.mk("MQL5/Files/Lascodia/slo/LASC-LIVE-222_slo_history.jsonl", 1)
        apikey = self.mk("MQL5/Files/Lascodia/apikey_LASC-OLD-111.txt", 100)
        machine = self.mk("MQL5/Files/Lascodia/machine_5001_LASC-OLD-111.txt", 100)
        override = self.mk("MQL5/Files/Lascodia/config_override_LASC-OLD-111.json", 100)
        outbox = self.mk("MQL5/Files/Lascodia/outbox/LASC-OLD-111_x.json", 100)
        picked = {c.path for c in prune.select(self.lay.term, self.lay.mql5)}
        self.assertIn(old_journal, picked)
        self.assertIn(old_bridge, picked)
        self.assertIn(stale_ledger, picked)
        self.assertIn(stale_slo, picked)
        for keep in (new_journal, only_old_mql_log, newest_bridge, stale_seq, stale_state, live_ledger,
                     live_slo, apikey, machine, override, outbox):
            self.assertNotIn(keep, picked, keep.name)

    def test_newest_of_a_kind_survives_even_when_stale(self):
        only = self.mk("MQL5/Files/Lascodia/ledger/LASC-OLD-111_events.jsonl", 45)
        self.assertEqual(prune.select(self.lay.term, self.lay.mql5), [])
        self.assertTrue(only.exists())

    def test_dry_run_deletes_nothing_and_real_run_deletes(self):
        p = self.mk("logs/20260901.log", 60, 123)
        self.mk("logs/20261031.log", 0)
        r = prune.run(self.lay.term, self.lay.mql5, dry_run=True)
        self.assertEqual((r["fileCount"], r["totalBytes"], r["deletedCount"]), (1, 123, 0))
        self.assertTrue(p.exists())
        r = prune.run(self.lay.term, self.lay.mql5, dry_run=False)
        self.assertEqual((r["deletedCount"], r["freedBytes"]), (1, 123))
        self.assertFalse(p.exists())


class JournalTests(unittest.TestCase):
    def test_signatures(self):
        text = ("Ticks\told tick USDCHF (tick: 1971.07.14 00:22:42.000, last: 2026.10.27 19:10:31.215)\n"
                "Ticks\told tick EURGBP (tick: 2026.05.15 14:17:14.155, last: 2026.05.15 14:19:32.355)\n"
                "Books\tinvalid book item[EURUSD,buy,1.11288,-800000000000000]\n"
                "A debugger has been found running in your system\n")
        self.assertEqual(count_signatures(text), {"oldTick": 1, "invalidBook": 1, "debuggerFound": 1})

    def test_incremental_scan_and_thresholds(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d)
            p = logs / "20261031.log"
            p.write_bytes(utf16("normal line\n"))
            w = JournalWatcher(logs)
            self.assertFalse(any(w.scan(1000).values()))
            bad = "".join("old tick X (tick: 1970.01.01 00:00:03.000, last: 2026)\n" for _ in range(25))
            with p.open("ab") as f:
                f.write(bad.encode("utf-16-le"))
            sig = w.scan(1010)
            self.assertTrue(sig["oldTick"])
            self.assertEqual(w.last_counts["oldTick"], 25)
            self.assertEqual(w.scan(1020)["oldTick"], True)           # stays raised for the window
            self.assertEqual(w.last_counts["oldTick"], 0)             # nothing re-read
            self.assertFalse(w.scan(1010 + 901)["oldTick"])
            # A new day's journal is read from its start.
            (logs / "20261101.log").write_bytes(utf16(bad))
            self.assertTrue(w.scan(9000)["oldTick"])
            p = logs / "20261101.log"
            # A stray invalid-book line is not a signal.
            with p.open("ab") as f:
                f.write("invalid book item[x]\n".encode("utf-16-le"))
            self.assertFalse(w.scan(5000)["invalidBook"])


    def test_history_before_the_supervisor_started_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            bad = "".join("old tick X (tick: 1970.01.01 00:00:03.000, last: 2026)\n" for _ in range(50))
            (Path(d) / "20261031.log").write_bytes(utf16(bad))
            self.assertFalse(JournalWatcher(Path(d)).scan(1000)["oldTick"])


class WakeTests(unittest.TestCase):
    def test_wall_vs_monotonic_gap_means_sleep(self):
        clock = {"wall": 1000.0, "mono": 50.0}
        w = WakeDetector(wall=lambda: clock["wall"], mono=lambda: clock["mono"], waketime=lambda: None)
        w.tick()
        clock["wall"] += 30
        clock["mono"] += 30
        w.tick()
        self.assertFalse(w.woke_within(300))
        clock["wall"] += 3600   # slept an hour: monotonic did not move
        clock["mono"] += 1
        w.tick()
        self.assertTrue(w.woke_within(300))
        clock["wall"] += 400
        clock["mono"] += 400
        self.assertFalse(w.woke_within(300))

    def test_kernel_waketime(self):
        w = WakeDetector(wall=lambda: 10_000.0, mono=lambda: 1.0, waketime=lambda: 9_900.0)
        w.tick()
        self.assertTrue(w.woke_within(300))


class DesiredStateTests(unittest.TestCase):
    def test_extract_enveloped_and_bare(self):
        rows = [{"installId": "a", "desiredState": "Stopped", "autoHeal": False}]
        self.assertEqual(extract_desired({"data": {"installs": rows}, "status": True}), rows)
        self.assertEqual(extract_desired({"installs": rows}), rows)
        self.assertIsNone(extract_desired({"data": "Heartbeat acknowledged", "status": True}))

    def test_defaults_missing_keeps_last_known_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            managed = SimpleNamespace(install_id="m", managed=True)
            unmanaged = SimpleNamespace(install_id="u", managed=False)
            s = DesiredStateStore(d)
            self.assertEqual(s.get(managed), ("Running", True, False))
            self.assertEqual(s.get(unmanaged), ("Stopped", False, False))
            s.apply([{"installId": "m", "desiredState": "Stopped", "autoHeal": True}])
            s.apply([])                       # missing → keep last known
            s.apply([{"installId": "m", "desiredState": "Bogus"}])
            self.assertEqual(s.get(managed), ("Stopped", True, True))
            self.assertEqual(DesiredStateStore(d).get(managed), ("Stopped", True, True))


def _supervisor(tmp: Path, host: FakeHost, layouts, engine=None) -> tuple[Supervisor, SupervisorConfig]:
    cfg = SupervisorConfig(daemon_id="d", name="d", engine_url=ENGINE, daemon_api_key="k" * 32,
                           installs=[l.install for l in layouts], state_dir=str(tmp / "state"),
                           log_dir=str(tmp / "logs"))
    for l in layouts:
        host.app_layouts[l.app] = l
    wake = WakeDetector(wall=time.time, mono=time.monotonic, waketime=lambda: None)
    sup = Supervisor(lambda: cfg, ops=host, engine=engine, wake=wake, apps_dir=layouts[0].apps,
                     synchronous_actions=True)
    return sup, cfg


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = make_layout(self.root, "alpha")
        self.b = make_layout(self.root, "beta")
        self.host = FakeHost()

    def tearDown(self):
        self.tmp.cleanup()

    def write_beacon(self, lay, phase="running", tick_ago=5.0):
        now = time.time()
        b = {"schema": 1, "instanceId": "LASC-1", "accountLogin": 1, "server": "S", "chartId": "1",
             "eaVersion": "8.47.259", "phase": phase, "state": "RUNNING", "initFailReason": "", "authState": "ok",
             "lastTimerUtc": iso_utc(now - tick_ago), "writtenAtUtc": iso_utc(now - tick_ago), "terminalBuild": 5834}
        (lay.mql5 / "Files" / "Lascodia" / "beacon" / "LASC-1.json").write_text(json.dumps(b))
        return b

    def test_heartbeat_runtime_shape(self):
        t, w, _ = self.host.add_terminal(self.a, created=time.time() - 7200)
        beacon = self.write_beacon(self.a)
        (self.a.mql5 / "Experts" / "LascodiaEA.ex5").write_bytes(b"ea")
        sup, cfg = _supervisor(self.root, self.host, [self.a, self.b])
        sup.tick()
        rows = sup.installs_payload([{"installId": "alpha", "name": "A"}, {"installId": "beta", "name": "B"}])
        rt = rows[0]["runtime"]
        self.assertEqual(set(rt), {"running", "terminalPid", "wrapperPid", "adopted", "launchMode", "beacons",
                                   "beaconReadError", "watchdog", "journalSignals", "eaBinarySha256",
                                   "diskFreeBytes"})
        self.assertEqual((rt["running"], rt["terminalPid"], rt["wrapperPid"], rt["adopted"], rt["launchMode"]),
                         (True, t, w, True, "app"))
        self.assertEqual(rt["beacons"], [beacon])
        self.assertEqual(rt["eaBinarySha256"], hashlib.sha256(b"ea").hexdigest())
        self.assertEqual(set(rt["watchdog"]), {"state", "restartsLastHour", "lastActionUtc", "lastReason",
                                               "nextAttemptUtc"})
        self.assertEqual(set(rt["journalSignals"]), {"oldTick", "invalidBook", "debuggerFound"})
        self.assertGreater(rt["diskFreeBytes"], 0)
        self.assertFalse(rows[1]["runtime"]["running"])
        self.assertEqual(rows[0]["name"], "A")

    def test_managed_absent_terminal_is_started_by_the_watchdog(self):
        sup, _ = _supervisor(self.root, self.host, [self.a])
        sup.tick()
        self.assertIn(("OPEN", self.a.app, ()), self.host.calls)
        self.assertEqual(sup.watchdog.state("alpha").status, "restarting")

    def test_engine_stopped_desired_state_is_obeyed(self):
        sup, _ = _supervisor(self.root, self.host, [self.a])
        sup.apply_heartbeat_response({"data": {"installs": [
            {"installId": "alpha", "desiredState": "Stopped", "autoHeal": True}]}, "status": True})
        sup.tick()
        self.assertEqual(self.host.calls, [])

    def test_unmanaged_install_left_alone(self):
        self.a.install.managed = False
        sup, _ = _supervisor(self.root, self.host, [self.a])
        sup.tick()
        self.assertEqual(self.host.calls, [])
        self.assertEqual(sup.watchdog.state("alpha").status, "disabled")

    def test_wedged_terminal_restarted(self):
        t, w, _ = self.host.add_terminal(self.a, created=time.time() - 7200)
        self.write_beacon(self.a, tick_ago=1800)
        sup, _ = _supervisor(self.root, self.host, [self.a])
        sup.tick()
        self.assertIn(("TERM", t, "wine"), self.host.calls)
        self.assertIn(("OPEN", self.a.app, ()), self.host.calls)

    def test_fresh_beacon_no_action(self):
        self.host.add_terminal(self.a, created=time.time() - 7200)
        self.write_beacon(self.a, tick_ago=5)
        sup, _ = _supervisor(self.root, self.host, [self.a])
        sup.tick()
        self.assertEqual(self.host.calls, [])
        self.assertEqual(sup.watchdog.state("alpha").status, "ok")

    def test_jobs_through_runtime(self):
        engine = SimpleNamespace(download_release=lambda rid: (b"bin", ""))
        sup, _ = _supervisor(self.root, self.host, [self.a], engine=engine)
        self.a.install.managed = False   # keep the watchdog out of this test
        st, msg, data = sup.run_job({"jobId": 1, "type": "StartTerminal", "installId": "alpha", "params": {}})
        self.assertEqual(st, SUCCEEDED, msg)
        self.assertTrue(sup.watchdog.state("alpha").pending_start)
        self.assertEqual(sup.watchdog.state("alpha").auto_actions, [])
        st, msg, _ = sup.run_job({"jobId": 2, "type": "StopTerminal", "installId": "alpha", "params": {"reason": "op"}})
        self.assertEqual(st, SUCCEEDED, msg)
        sha = hashlib.sha256(b"bin").hexdigest()
        st, msg, data = sup.run_job({"jobId": 3, "type": "DeployEaBinary", "installId": "alpha",
                                     "params": {"releaseId": 7, "sha256": sha}})
        self.assertEqual((st, data["sha256"]), (SUCCEEDED, sha), msg)
        st, msg, data = sup.run_job({"jobId": 4, "type": "PruneFiles", "installId": "alpha", "params": {}})
        self.assertEqual(st, SUCCEEDED)
        self.assertTrue(data["dryRun"])
        self.assertEqual(sup.run_job({"jobId": 5, "type": "Nope", "installId": "alpha"})[0], FAILED)
        self.assertEqual(sup.run_job({"jobId": 6, "type": "StartTerminal", "installId": "zzz"})[0], FAILED)
        st, msg, _ = sup.run_job({"jobId": 7, "type": "DeployEaBinary", "installId": "alpha",
                                  "params": {"releaseId": 7}})
        self.assertEqual(st, FAILED)


class EngineJobChannelTests(unittest.TestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, tuple] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("lascodia-trading-engine/", 1)[-1]
        if path == "admin/terminals/daemons/auth":
            return httpx.Response(200, json={"status": True, "data": {"token": "t", "expiresAt": "2099-01-01T00:00:00Z"}})
        status, kw = self.answers.get(path, (404, {"json": {"status": False}}))
        return httpx.Response(status, **kw)

    def client(self) -> EngineClient:
        d = tempfile.mkdtemp()
        cfg = SupervisorConfig(daemon_id="mac", name="m", engine_url=ENGINE, daemon_api_key="k" * 32,
                               credential_path=str(Path(d) / "c.json"))
        return EngineClient(cfg, transport=httpx.MockTransport(self.handler))

    def test_poll_parses_envelope(self):
        jobs = [{"jobId": 9, "type": "TailLog", "installId": "a", "params": {"kind": "ea"}}]
        self.answers["admin/terminals/daemons/jobs"] = (200, {"json": {"status": True, "data": jobs}})
        c = self.client()
        self.assertEqual(c.poll_jobs(20), jobs)
        req = self.requests[-1]
        self.assertEqual(req.url.params["waitSec"], "20")
        self.assertEqual(req.headers["authorization"], "Bearer t")

    def test_poll_404_means_unsupported(self):
        c = self.client()
        self.assertIsNone(c.poll_jobs(1))
        self.assertTrue(c.jobs_unsupported)

    def test_post_result(self):
        self.answers["admin/terminals/daemons/jobs/9/result"] = (200, {"json": {"status": True}})
        c = self.client()
        self.assertTrue(c.post_job_result(9, "Succeeded", "ok", {"x": 1}))
        self.assertEqual(json.loads(self.requests[-1].content), {"status": "Succeeded", "message": "ok", "data": {"x": 1}})

    def test_download_raw_and_base64(self):
        self.answers["platform/ea/releases/7/binary"] = (200, {"content": b"\x00bin",
                                                               "headers": {"content-type": "application/octet-stream"}})
        self.assertEqual(self.client().download_release(7), (b"\x00bin", ""))
        self.answers["platform/ea/releases/7/binary"] = (200, {"json": {"status": True, "data": {
            "contentBase64": base64.b64encode(b"bin").decode()}}})
        self.assertEqual(self.client().download_release(7), (b"bin", ""))

    def test_heartbeat_ex_returns_body(self):
        body = {"status": True, "data": {"installs": [{"installId": "a", "desiredState": "Running", "autoHeal": True}]}}
        self.answers["admin/terminals/daemons/heartbeat"] = (200, {"json": body})
        ok, got = self.client().heartbeat_ex([], [{"installId": "a", "runtime": {"running": True}}])
        self.assertTrue(ok)
        self.assertEqual(got, body)
        sent = json.loads(self.requests[-1].content)
        self.assertEqual(sent["installs"][0]["runtime"], {"running": True})


class LogHygieneTests(unittest.TestCase):
    def test_trim_keeps_tail_and_truncates(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stderr.log"
            p.write_bytes(b"a" * (3 * 1024 * 1024) + b"END")
            self.assertTrue(trim_launchd_log(p, 1024 * 1024))
            self.assertEqual(p.stat().st_size, 0)
            self.assertTrue((Path(d) / "stderr.log.1").read_bytes().endswith(b"END"))
            self.assertFalse(trim_launchd_log(p, 1024 * 1024))


if __name__ == "__main__":
    unittest.main()
