"""Adoption mapping (pid → install) and the start / stop / restart sequences, against fakes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fakes import WIN_EXE, FakeHost, make_layout

from lascodia_supervisor import host
from lascodia_supervisor.host import ProcInfo, discover, is_terminal_process
from lascodia_supervisor.process_manager import ProcessManager
from lascodia_supervisor.terminal_control import TerminalController


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.a = make_layout(root, "metatrader5", bundle_id="net.metaquotes.wine.MetaTrader5")
        self.b = make_layout(root, "exness")
        self.installs = [self.a.install, self.b.install]
        self.apps = self.a.apps

    def tearDown(self):
        self.tmp.cleanup()

    def test_terminal_recognised_by_command_line_not_name(self):
        live = ProcInfo(1, 2, "wine", "/x/wine", (WIN_EXE, ""), 0.0)
        self.assertTrue(is_terminal_process(live))
        self.assertTrue(is_terminal_process(ProcInfo(1, 2, "terminal64.exe", "", (), 0.0)))
        self.assertFalse(is_terminal_process(ProcInfo(1, 2, "wine", "", ("C:\\windows\\system32\\services.exe",), 0.0)))
        self.assertFalse(is_terminal_process(ProcInfo(1, 2, "wine", "", ("C:\\MT5\\metatester64.exe",), 0.0)))

    def test_app_found_by_bundle_id_case_insensitively(self):
        self.assertEqual(host.resolve_app_path(self.a.install, self.apps), self.a.app)
        self.assertEqual(host.resolve_app_path(self.b.install, self.apps), self.b.app)
        self.assertEqual(host.effective_launch_mode(self.a.install, self.apps), "app")
        self.a.install.launch_mode = "wine"
        self.assertEqual(host.effective_launch_mode(self.a.install, self.apps), "wine")

    def test_no_app_means_wine_mode(self):
        inst = self.b.install.model_copy(update={"wine_prefix": "/nowhere/net.metaquotes.wine.zzz"})
        self.assertIsNone(host.resolve_app_path(inst, self.apps))
        self.assertEqual(host.effective_launch_mode(inst, self.apps), "wine")

    def test_maps_by_lsof_wrapper_and_wineserver(self):
        h = FakeHost()
        t, w, ws = h.add_terminal(self.a)
        d = discover(self.installs, h.list_processes(), h.prefix_of_pid, self.apps)
        self.assertEqual((d["metatrader5"].terminal_pid, d["metatrader5"].wrapper_pid), (t, w))
        self.assertEqual(d["metatrader5"].wineserver_pids, [ws])
        self.assertEqual(d["metatrader5"].mapped_by, "lsof")
        self.assertFalse(d["exness"].running)

    def test_maps_by_env_then_wrapper_parent_when_lsof_blind(self):
        h = FakeHost()
        t1 = h.add("wine", cmd=(WIN_EXE,), env_prefix=self.b.prefix)
        w = h.add("MetaTrader 5", exe=f"{self.a.app}/Contents/MacOS/MetaTrader 5")
        t2 = h.add("wine", cmd=(WIN_EXE,), ppid=w)
        d = discover(self.installs, h.list_processes(), lambda pid: None, self.apps)
        self.assertEqual(d["exness"].terminal_pid, t1)
        self.assertEqual(d["exness"].mapped_by, "env")
        self.assertEqual(d["metatrader5"].terminal_pid, t2)
        self.assertEqual(d["metatrader5"].mapped_by, "wrapper")

    def test_unix_path_in_cmdline_maps_wine_mode_terminal(self):
        h = FakeHost()
        t = h.add("wine64-preloader", cmd=("/x/wine64", self.b.install.executable))
        d = discover(self.installs, h.list_processes(), lambda pid: None, self.apps)
        self.assertEqual(d["exness"].terminal_pid, t)

    def test_unknown_prefix_is_ignored(self):
        h = FakeHost()
        h.add("wine", cmd=(WIN_EXE,), lsof_prefix="/Users/x/Library/Application Support/net.metaquotes.wine.test")
        d = discover(self.installs, h.list_processes(), h.prefix_of_pid, self.apps)
        self.assertFalse(any(x.running for x in d.values()))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = make_layout(Path(self.tmp.name), "exness")
        self.h = FakeHost()
        self.h.app_layouts[self.lay.app] = self.lay
        self.c = TerminalController(self.h, lambda: [self.lay.install], apps_dir=self.lay.apps)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_terms_terminal_then_wrapper_then_lingering_wineserver(self):
        t, w, ws = self.h.add_terminal(self.lay)
        self.h.stubborn.add(ws)  # wineserver lingers and ignores TERM → KILL
        res = self.c.stop(self.lay.install, "test")
        self.assertTrue(res.ok, res.message)
        sigs = [(c[0], c[1]) for c in self.h.calls]
        self.assertEqual(sigs, [("TERM", t), ("TERM", w), ("TERM", ws), ("KILL", ws)])

    def test_stop_escalates_to_sigkill_for_stubborn_terminal(self):
        t, w, _ = self.h.add_terminal(self.lay)
        self.h.stubborn.add(t)
        res = self.c.stop(self.lay.install)
        self.assertTrue(res.ok)
        self.assertEqual([c[:2] for c in self.h.calls[:3]], [("TERM", t), ("KILL", t), ("TERM", w)])

    def test_stop_reports_failure_when_process_survives(self):
        t, _, _ = self.h.add_terminal(self.lay)
        self.h.unkillable.add(t)
        res = self.c.stop(self.lay.install)
        self.assertFalse(res.ok)
        self.assertIn("did not stop", res.message)

    def test_stop_when_not_running_is_a_success_noop(self):
        res = self.c.stop(self.lay.install)
        self.assertTrue(res.ok)
        self.assertEqual(self.h.calls, [])

    def test_start_app_mode_uses_open_a_and_waits_for_terminal(self):
        res = self.c.start(self.lay.install, "test")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.h.calls[0], ("OPEN", self.lay.app, ()))
        self.assertEqual(res.data["launchMode"], "app")
        self.assertIsNotNone(res.data["terminalPid"])

    def test_start_kills_orphan_wrapper_first(self):
        w = self.h.add("MetaTrader 5", exe=f"{self.lay.app}/Contents/MacOS/MetaTrader 5")
        res = self.c.start(self.lay.install)
        self.assertTrue(res.ok)
        self.assertEqual(self.h.calls[0][:2], ("TERM", w))
        self.assertEqual(self.h.calls[1][0], "OPEN")

    def test_start_fails_honestly_when_terminal_never_appears(self):
        self.h.open_starts_terminal = False
        self.c.start_timeout_sec = 10
        res = self.c.start(self.lay.install)
        self.assertFalse(res.ok)
        self.assertIn("did not appear", res.message)

    def test_start_already_running(self):
        self.h.add_terminal(self.lay)
        res = self.c.start(self.lay.install)
        self.assertTrue(res.ok)
        self.assertTrue(res.data["alreadyRunning"])
        self.assertFalse(any(c[0] == "OPEN" for c in self.h.calls))

    def test_wine_mode_spawns_with_prefix(self):
        self.lay.install.launch_mode = "wine"
        self.lay.install.wine_binary = "/bin/sh"  # any executable file
        self.h.open_starts_terminal = False
        self.c.start_timeout_sec = 4
        self.c.start(self.lay.install)
        spawn = [c for c in self.h.calls if c[0] == "SPAWN"][0]
        self.assertEqual(spawn[1][-1], self.lay.install.executable)
        self.assertEqual(spawn[2], self.lay.prefix)

    def test_restart_stops_then_starts(self):
        t, w, _ = self.h.add_terminal(self.lay)
        res = self.c.restart(self.lay.install, "wedged")
        self.assertTrue(res.ok, res.message)
        kinds = [c[0] for c in self.h.calls]
        self.assertEqual(kinds[:2], ["TERM", "TERM"])
        self.assertIn("OPEN", kinds)
        self.assertNotEqual(res.data["terminalPid"], t)


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lay = make_layout(Path(self.tmp.name), "exness")
        self.state = str(Path(self.tmp.name) / "state")
        self.h = FakeHost()
        self.h.app_layouts[self.lay.app] = self.lay
        self.c = TerminalController(self.h, lambda: [self.lay.install], apps_dir=self.lay.apps)

    def tearDown(self):
        self.tmp.cleanup()

    def test_running_terminal_adopted_and_session_survives_restart(self):
        t, w, _ = self.h.add_terminal(self.lay)
        pm = ProcessManager(self.state, self.c, lambda: [self.lay.install])
        pm.refresh()
        [s] = pm.list_sessions()
        self.assertTrue(s.adopted)
        self.assertEqual((s.pid, s.wrapper_pid, s.status), (t, w, "Running"))
        # A new supervisor process: same session id, no duplicate.
        pm2 = ProcessManager(self.state, self.c, lambda: [self.lay.install])
        pm2.refresh()
        self.assertEqual([x.session_id for x in pm2.list_sessions()], [s.session_id])

    def test_dead_terminal_marked_crashed_and_relaunch_adopted_as_new_session(self):
        t, _, _ = self.h.add_terminal(self.lay)
        pm = ProcessManager(self.state, self.c, lambda: [self.lay.install])
        pm.refresh()
        self.h.procs.clear()
        pm.refresh()
        self.assertEqual(pm.list_sessions()[0].status, "Crashed")
        self.h.add_terminal(self.lay)
        pm.refresh()
        statuses = sorted(s.status for s in pm.list_sessions())
        self.assertEqual(statuses, ["Crashed", "Running"])

    def test_launch_records_starting_session_then_adopts_pid(self):
        pm = ProcessManager(self.state, self.c, lambda: [self.lay.install])
        self.h.open_starts_terminal = False
        sess = pm.launch(self.lay.install, reason="engine")
        self.assertEqual((sess.status, sess.pid), ("Starting", None))
        pm.refresh()
        self.assertEqual(pm.get(sess.session_id).status, "Starting")  # within the grace
        t, _, _ = self.h.add_terminal(self.lay)
        pm.refresh()
        self.assertEqual((pm.get(sess.session_id).status, pm.get(sess.session_id).pid), ("Running", t))

    def test_orphan_scan_excludes_adopted(self):
        self.h.add_terminal(self.lay)
        other = self.h.add("wine", cmd=(WIN_EXE,), lsof_prefix="/elsewhere/net.metaquotes.wine.test")
        pm = ProcessManager(self.state, self.c, lambda: [self.lay.install])
        pm.refresh()
        self.assertEqual([o["pid"] for o in pm.scan_orphans()], [other])


if __name__ == "__main__":
    unittest.main()
