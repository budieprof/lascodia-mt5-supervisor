"""Install detection (macOS + Windows layouts in temp dirs), installer setup (new / upgrade), and
Windows native launch mode with the fake host."""
from __future__ import annotations

import os
import plistlib
import re
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from lascodia_supervisor import cli, detect, host
from lascodia_supervisor.config import MT5Install
from lascodia_supervisor.terminal_control import TerminalController

from fakes import FakeHost

TOKEN = "lsc_enr_abcd1234_" + "A" * 40


def mac_app(apps: Path, name: str, bundle_id: str) -> Path:
    app = apps / f"{name}.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return app


def mac_prefix(home: Path, name: str, login: str = "", server: str = "") -> Path:
    term = home / "Library" / "Application Support" / name / "drive_c" / "Program Files" / "MetaTrader 5"
    (term / "config").mkdir(parents=True)
    (term / "terminal64.exe").write_bytes(b"MZ")
    if login:
        (term / "config" / "common.ini").write_bytes(f"[Common]\r\nLogin={login}\r\nServer={server}\r\n".encode("utf-16"))
    return term / "terminal64.exe"


class MacDetectTests(unittest.TestCase):
    def test_apps_and_orphan_prefixes(self):
        with tempfile.TemporaryDirectory() as d:
            home, apps = Path(d) / "home", Path(d) / "Applications"
            mac_app(apps, "MetaTrader 5", "net.metaquotes.wine.MetaTrader5")
            mac_app(apps, "MetaTrader 5 - exness", "net.metaquotes.wine.exness")
            mac_app(apps, "MetaTrader 5 - empty", "net.metaquotes.wine.empty")      # no prefix → skipped
            mac_prefix(home, "net.metaquotes.wine.metatrader5", "5049397557", "MetaQuotes-Demo")
            mac_prefix(home, "net.metaquotes.wine.exness")
            mac_prefix(home, "net.metaquotes.wine.old")                               # no app → wine, unmanaged
            rows = {r["install_id"]: r for r in detect.detect_macos(str(home), str(apps))}
            self.assertEqual(set(rows), {"wine-mt5-default", "exness", "old"})
            self.assertEqual(rows["wine-mt5-default"]["account_login"], "5049397557")
            self.assertEqual(rows["wine-mt5-default"]["broker_name"], "MetaQuotes-Demo")
            self.assertEqual((rows["exness"]["launch_mode"], rows["exness"]["managed"]), ("app", True))
            self.assertEqual((rows["old"]["launch_mode"], rows["old"]["managed"]), ("wine", False))
            for r in rows.values():
                MT5Install(**r)   # every row is a valid config entry


class WindowsDetectTests(unittest.TestCase):
    def test_program_files_and_origin_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pf, appdata = d / "Program Files", d / "AppData" / "Roaming"
            for folder in ("MetaTrader 5", "Exness MetaTrader 5", "IC Markets/MetaTrader 5 Terminal", "Notepad++"):
                (pf / folder).mkdir(parents=True)
            for folder in ("MetaTrader 5", "Exness MetaTrader 5", "IC Markets/MetaTrader 5 Terminal"):
                (pf / folder / "terminal64.exe").write_bytes(b"MZ")
            (pf / "Notepad++" / "terminal64.exe").write_bytes(b"MZ")                 # not MetaTrader
            data = appdata / "MetaQuotes" / "Terminal" / "D0E8209F77C8CF37AD8BF550E51FF075"
            (data / "config").mkdir(parents=True)
            (data / "origin.txt").write_bytes(str(pf / "Exness MetaTrader 5").encode("utf-16"))   # BOM + UTF-16LE
            (data / "config" / "common.ini").write_text("Login=222117954\nServer=Exness-MT5Real30\n")
            rows = {r["install_id"]: r for r in detect.detect_windows({"ProgramFiles": str(pf), "APPDATA": str(appdata)})}
            self.assertEqual(set(rows), {"metatrader-5", "exness-metatrader-5", "metatrader-5-terminal"})
            ex = rows["exness-metatrader-5"]
            self.assertEqual(ex["mql5_dir"], str(data / "MQL5"))
            self.assertEqual((ex["account_login"], ex["broker_name"]), ("222117954", "Exness-MT5Real30"))
            self.assertEqual((ex["launch_mode"], ex["managed"], ex["use_wine"]), ("native", True, False))
            # no origin.txt → portable layout: data next to the exe
            self.assertEqual(rows["metatrader-5"]["mql5_dir"], str(pf / "MetaTrader 5" / "MQL5"))

    def test_read_origin_encodings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "origin.txt"
            for raw in ("C:\\MT5".encode("utf-16"), "C:\\MT5".encode("utf-16-le"), b"C:\\MT5\r\n"):
                p.write_bytes(raw)
                self.assertEqual(detect.read_origin(p), "C:\\MT5")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.found = [{"install_id": "exness", "name": "Exness", "executable": "/x/exness/terminal64.exe",
                       "launch_mode": "app", "managed": True}]

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self) -> dict:
        return yaml.safe_load((self.root / "config" / "supervisor.yaml").read_text())

    def test_fresh_install(self):
        s = cli.run_setup(self.root, "https://app.example.com/api/v1/lascodia-trading-engine/", TOKEN,
                          hostname="Ops-MacBook.local", detected=self.found)
        self.assertTrue(s["created"])
        c = self.cfg()
        self.assertRegex(c["daemon_id"], r"^ops-macbook-[a-z0-9]{4}$")
        self.assertEqual(c["engine_url"], "https://app.example.com/api/v1/lascodia-trading-engine")
        self.assertEqual((c["engine_resolver"], c["bind_host"], c["enrollment_token"]), ("auto", "127.0.0.1", TOKEN))
        self.assertGreaterEqual(len(c["daemon_api_key"]), 16)
        self.assertEqual(c["installs"], self.found)
        mode = stat.S_IMODE((self.root / "config" / "supervisor.yaml").stat().st_mode)
        self.assertEqual(mode, 0o600)
        from lascodia_supervisor.config import SupervisorConfig
        SupervisorConfig(**c)

    def test_rerun_keeps_identity_and_operator_settings(self):
        cli.run_setup(self.root, "https://a/api", TOKEN, hostname="h", detected=self.found)
        first = self.cfg()
        # the operator turned exness off; a credential now exists; a second terminal appeared
        first["installs"][0]["managed"] = False
        (self.root / "config" / "supervisor.yaml").write_text(yaml.safe_dump(first))
        (self.root / "config" / "machine_credential.json").write_text("{}")
        found2 = self.found + [{"install_id": "exness", "name": "Other", "executable": "/x/other/terminal64.exe",
                                "managed": True}]
        token2 = "lsc_enr_zzzz9999_" + "B" * 40
        s = cli.run_setup(self.root, "https://b/api", token2, hostname="h", detected=found2)
        c = self.cfg()
        self.assertFalse(s["created"])
        for k in ("daemon_id", "daemon_api_key", "bind_port", "engine_url"):
            self.assertEqual(c[k], first[k], k)
        self.assertEqual(c["enrollment_token"], TOKEN)                 # credential exists: token not needed
        self.assertFalse(c["installs"][0]["managed"])                  # never rewritten
        self.assertEqual([i["install_id"] for i in c["installs"]], ["exness", "exness-2"])
        self.assertTrue(s["notes"])                                    # the engine-address note
        self.assertTrue(list((self.root / "config").glob("supervisor.yaml.bak-*")))

    def test_rerun_without_credential_takes_the_new_token(self):
        cli.run_setup(self.root, "https://a/api", TOKEN, hostname="h", detected=[])
        token2 = "lsc_enr_zzzz9999_" + "B" * 40
        cli.run_setup(self.root, "https://a/api", token2, hostname="h", detected=[])
        self.assertEqual(self.cfg()["enrollment_token"], token2)

    def test_unchanged_rerun_does_not_rewrite(self):
        cli.run_setup(self.root, "https://a/api", TOKEN, hostname="h", detected=self.found)
        p = self.root / "config" / "supervisor.yaml"
        p.write_text(p.read_text() + "# operator comment\n")
        cli.run_setup(self.root, "https://a/api", TOKEN, hostname="h", detected=self.found)
        self.assertIn("# operator comment", p.read_text())

    def test_bad_token_is_refused(self):
        with self.assertRaises(ValueError):
            cli.run_setup(self.root, "https://a/api", "not-a-token", detected=[])

    def test_port_picking(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        try:
            self.assertFalse(cli.port_free(busy))
            self.assertNotEqual(cli.pick_port(busy, tries=5), busy)
        finally:
            s.close()


class WindowsNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.exe = d / "Program Files" / "Exness MetaTrader 5" / "terminal64.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"MZ")
        self.data = d / "AppData" / "MetaQuotes" / "Terminal" / "HASH"
        self.inst = MT5Install(install_id="exness", name="Exness", executable=str(self.exe), launch_mode="native",
                               mql5_dir=str(self.data / "MQL5"), managed=True)
        self.h = FakeHost()
        self.ctl = TerminalController(self.h, lambda: [self.inst], stop_grace_sec=1, start_timeout_sec=10,
                                      apps_dir=str(d / "none"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_from_mql5_dir(self):
        self.assertEqual(host.data_dir(self.inst), self.data)
        self.assertEqual(host.journal_dir(self.inst), self.data / "logs")

    def test_discovery_by_exe_path_case_insensitive(self):
        win_path = str(self.exe).replace("/", "\\").upper()
        pid = self.h.add("terminal64.exe", exe=win_path, cmd=(win_path,))
        self.h.add("terminal64.exe", exe="C:\\Other\\terminal64.exe", cmd=("C:\\Other\\terminal64.exe",))
        d = self.ctl.discover(self.inst)
        self.assertEqual((d.terminal_pid, d.mapped_by, d.wrapper_pid, d.wineserver_pids), (pid, "exe", None, []))

    def test_start_spawns_the_exe_directly_and_stop_terminates(self):
        res = self.ctl.start(self.inst, reason="test")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.data["launchMode"], "native")
        spawn = [c for c in self.h.calls if c[0] == "SPAWN"][0]
        self.assertEqual(spawn[1], (str(self.exe),))
        self.assertFalse(any(c[0] == "OPEN" for c in self.h.calls))
        res = self.ctl.stop(self.inst, reason="test")
        self.assertTrue(res.ok, res.message)
        self.assertEqual([c[0] for c in self.h.calls if c[0] in ("TERM", "KILL")], ["TERM"])

    def test_stubborn_terminal_is_killed(self):
        self.ctl.start(self.inst)
        pid = self.ctl.discover(self.inst).terminal_pid
        self.h.stubborn.add(pid)
        res = self.ctl.stop(self.inst)
        self.assertTrue(res.ok)
        self.assertEqual([c[0] for c in self.h.calls if c[1] == pid and c[0] in ("TERM", "KILL")], ["TERM", "KILL"])

    def test_missing_exe_fails_plainly(self):
        self.exe.unlink()
        res = self.ctl.start(self.inst)
        self.assertFalse(res.ok)
        self.assertIn("not found", res.message)

    def test_native_is_the_windows_default(self):
        inst = MT5Install(install_id="a", name="a", executable="C:\\MT5\\terminal64.exe")
        orig = host.is_windows
        host.is_windows = lambda: True
        try:
            self.assertEqual(host.effective_launch_mode(inst), "native")
        finally:
            host.is_windows = orig
