"""Self-update (C9): the decision, staging + verification, flip, confirm, rollback (in-process and by
the macOS launcher), version pruning.  Temp dirs only; nothing is restarted."""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from lascodia_supervisor import layout, selfupdate
from lascodia_supervisor.selfupdate import Updater, decide, extract_offer

REPO = Path(__file__).resolve().parent.parent
SHA = "a" * 64


def make_bundle(version: str, *, zip_fmt: bool = False, corrupt: bool = False, bad_version_file: bool = False) -> bytes:
    files = {"VERSION": (version if not bad_version_file else "9.9.9") + "\n",
             "python/bin/python3": "#!/bin/sh\n", "launcher/run.sh": "#!/bin/sh\n"}
    manifest = "".join(f"{hashlib.sha256(c.encode()).hexdigest()}  {n}\n" for n, c in sorted(files.items()))
    if corrupt:
        files["launcher/run.sh"] = "tampered\n"
    files["MANIFEST.sha256"] = manifest
    buf = io.BytesIO()
    if zip_fmt:
        with zipfile.ZipFile(buf, "w") as z:
            for n, c in files.items():
                z.writestr(n, c)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for n, c in files.items():
                data = c.encode()
                info = tarfile.TarInfo(n)
                info.size = len(data)
                info.mode = 0o755
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def offer_for(version: str, payload: bytes, platform: str = "macos-arm64") -> dict:
    return {"version": version, "platform": platform, "releaseId": 7, "sha256": hashlib.sha256(payload).hexdigest()}


class DecideTests(unittest.TestCase):
    def test_table(self):
        o = {"version": "0.5.0", "platform": "macos-arm64", "releaseId": 1, "sha256": SHA}
        rows = [
            (dict(), True),
            (dict(enabled=False), False),
            (dict(offer=None), False),
            (dict(busy=True), False),
            (dict(bad={"0.5.0"}), False),
            (dict(current="0.5.0"), False),
            (dict(current="0.10.0"), False),            # numeric, not string, comparison
            (dict(platform="windows-x64"), False),
            (dict(offer={**o, "sha256": "short"}), False),
        ]
        for over, want in rows:
            with self.subTest(over=over):
                args = dict(offer=o, current="0.4.0", platform="macos-arm64", bad=set(), busy=False, enabled=True)
                args.update(over)
                go, why = decide(**args)
                self.assertEqual(go, want, why)

    def test_extract_offer_shapes(self):
        o = {"version": "0.5.0", "releaseId": 3, "sha256": SHA}
        self.assertEqual(extract_offer({"data": {"supervisorUpdate": o}}), o)
        self.assertEqual(extract_offer({"supervisorUpdate": o, "data": "Heartbeat acknowledged"}), o)
        self.assertIsNone(extract_offer({"data": {"installs": []}}))
        self.assertIsNone(extract_offer({"data": {"supervisorUpdate": {"version": "1"}}}))


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "versions" / "0.4.0").mkdir(parents=True)
        layout.set_current(self.root, "0.4.0", "symlink")
        self.now = 1_000_000.0
        self.smoke_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def updater(self, payload=None, version="0.4.0", style="symlink", smoke_ok=True):
        def smoke(d, v):
            self.smoke_calls.append((d, v))
            return (smoke_ok, "ok" if smoke_ok else "boom")
        return Updater(self.root, version=version, platform="macos-arm64",
                       download=lambda rid: (payload, "") if payload is not None else (None, "HTTP 500"),
                       smoke=smoke, clock=lambda: self.now, style=style)

    def test_apply_flip_confirm(self):
        payload = make_bundle("0.5.0")
        up = self.updater(payload)
        res = up.apply(offer_for("0.5.0", payload), busy=lambda: False)
        self.assertTrue(res.ok and res.restart, res.message)
        self.assertEqual(layout.read_current(self.root), "0.5.0")
        p = layout.read_kv(self.root / layout.PENDING)
        self.assertEqual((p["from"], p["to"]), ("0.4.0", "0.5.0"))
        self.assertEqual(int(p["deadline"]), int(self.now) + selfupdate.CONFIRM_WINDOW_SEC + selfupdate.RESTART_SLACK_SEC)
        self.assertEqual(len(self.smoke_calls), 1)
        self.assertFalse(any(d.name.startswith(".staging") for d in (self.root / "versions").iterdir()))
        # the old process must not confirm; the new one does
        self.assertFalse(up.confirm())
        new = self.updater(version="0.5.0")
        self.assertTrue(new.confirm())
        self.assertFalse((self.root / layout.PENDING).exists())
        self.assertEqual(layout.read_kv(self.root / layout.LAST)["version"], "0.5.0")

    def test_zip_bundle_and_pointer_style(self):
        (self.root / "current").unlink()
        layout.set_current(self.root, "0.4.0", "pointer")
        payload = make_bundle("0.5.0", zip_fmt=True)
        res = self.updater(payload, style="pointer").apply(offer_for("0.5.0", payload, "windows-x64"), lambda: False)
        self.assertTrue(res.ok, res.message)
        self.assertEqual((self.root / "current.txt").read_text().strip(), "0.5.0")

    def test_rejections_leave_current_alone(self):
        good = make_bundle("0.5.0")
        cases = [
            ("download fails", None, offer_for("0.5.0", good), True),
            ("sha mismatch", good, {**offer_for("0.5.0", good), "sha256": SHA}, True),
            ("manifest damaged", make_bundle("0.5.0", corrupt=True), None, True),
            ("VERSION disagrees", make_bundle("0.5.0", bad_version_file=True), None, True),
            ("smoke test fails", good, offer_for("0.5.0", good), False),
        ]
        for name, payload, offer, smoke_ok in cases:
            with self.subTest(name):
                offer = offer or offer_for("0.5.0", payload)
                res = self.updater(payload, smoke_ok=smoke_ok).apply(offer, lambda: False)
                self.assertFalse(res.ok)
                self.assertFalse(res.restart)
                self.assertEqual(layout.read_current(self.root), "0.4.0")
                self.assertFalse((self.root / layout.PENDING).exists())
                self.assertFalse((self.root / "versions" / "0.5.0").exists())

    def test_busy_at_the_last_moment_aborts(self):
        payload = make_bundle("0.5.0")
        calls = iter([True])
        res = self.updater(payload).apply(offer_for("0.5.0", payload), busy=lambda: next(calls, True))
        self.assertFalse(res.ok)
        self.assertEqual(layout.read_current(self.root), "0.4.0")

    def test_deadline_rollback_marks_bad(self):
        payload = make_bundle("0.5.0")
        self.updater(payload).apply(offer_for("0.5.0", payload), lambda: False)
        new = self.updater(version="0.5.0")
        self.assertIsNone(new.check_deadline())                      # still inside the window
        self.now += selfupdate.CONFIRM_WINDOW_SEC + selfupdate.RESTART_SLACK_SEC + 1
        res = new.check_deadline()
        self.assertTrue(res.ok and res.restart)
        self.assertEqual(layout.read_current(self.root), "0.4.0")
        self.assertIn("0.5.0", layout.bad_versions(self.root))
        self.assertTrue((self.root / layout.ROLLED_BACK).exists())
        # the old version now refuses the same offer
        go, why = self.updater().consider(offer_for("0.5.0", payload), lambda: False)
        self.assertFalse(go)
        self.assertIn("rolled back", why)

    def test_old_version_never_rolls_back_on_someone_elses_pending(self):
        payload = make_bundle("0.5.0")
        self.updater(payload).apply(offer_for("0.5.0", payload), lambda: False)
        self.now += 10_000
        self.assertIsNone(self.updater(version="0.4.0").check_deadline())

    def test_not_a_bundle_install_never_updates(self):
        up = Updater(None, version="0.4.0", platform="macos-arm64")
        go, why = up.consider({"version": "9.0.0", "platform": "macos-arm64", "releaseId": 1, "sha256": SHA}, lambda: False)
        self.assertFalse(go)
        self.assertIn("not a bundle install", why)

    def test_prune_keeps_current_pending_from_and_newest(self):
        for v in ("0.1.0", "0.2.0", "0.3.0", "0.5.0", "0.6.0"):
            (self.root / "versions" / v).mkdir()
        layout.write_kv(self.root / layout.PENDING, {"from": "0.1.0", "to": "0.6.0", "deadline": 0})
        removed = self.updater().prune(keep=2)
        self.assertEqual(sorted(removed), ["0.2.0", "0.3.0"])
        left = sorted(d.name for d in (self.root / "versions").iterdir())
        self.assertEqual(left, ["0.1.0", "0.4.0", "0.5.0", "0.6.0"])


class LauncherRollbackTests(unittest.TestCase):
    """launcher/run.sh rolls back a pending update past its deadline before starting Python."""

    def _root(self, d: Path) -> Path:
        root = d / "root with space"
        for v in ("0.4.0", "0.5.0"):
            py = root / "versions" / v / "python" / "bin" / "python3"
            py.parent.mkdir(parents=True)
            py.write_text(f"#!/bin/sh\necho ran {v} \"$LASCODIA_SUPERVISOR_HOME\" \"$@\"\n")
            py.chmod(0o755)
        (root / "bin").mkdir()
        (root / "bin" / "run.sh").write_bytes((REPO / "launcher" / "run.sh").read_bytes())
        os.symlink("versions/0.5.0", root / "current")
        return root

    def run_launcher(self, root: Path) -> str:
        return subprocess.run(["/bin/sh", str(root / "bin" / "run.sh")], capture_output=True, text=True,
                              timeout=20).stdout.strip()

    def test_past_deadline_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(Path(d))
            layout.write_kv(root / layout.PENDING, {"from": "0.4.0", "to": "0.5.0", "deadline": int(time.time()) - 1})
            out = self.run_launcher(root)
            self.assertTrue(out.startswith("ran 0.4.0"), out)
            self.assertIn(str(root), out)
            self.assertIn("-I -m lascodia_supervisor run", out)
            self.assertEqual(layout.read_current(root), "0.4.0")
            self.assertIn("0.5.0", layout.bad_versions(root))
            self.assertFalse((root / layout.PENDING).exists())

    def test_inside_deadline_runs_new_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(Path(d))
            layout.write_kv(root / layout.PENDING, {"from": "0.4.0", "to": "0.5.0", "deadline": int(time.time()) + 300})
            self.assertTrue(self.run_launcher(root).startswith("ran 0.5.0"))
            self.assertTrue((root / layout.PENDING).exists())


class LayoutTests(unittest.TestCase):
    def test_parse_version_and_platform(self):
        self.assertLess(selfupdate.parse_version("0.9.9"), selfupdate.parse_version("0.10.0"))
        self.assertEqual(selfupdate.parse_version("v1.2.3-rc1"), (1, 2, 3))
        self.assertRegex(layout.platform_tag(), r"^(macos|windows|linux)-")

    def test_install_root_from_env(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["LASCODIA_SUPERVISOR_HOME"] = d
            try:
                self.assertIsNone(layout.install_root())
                (Path(d) / "versions").mkdir()
                self.assertEqual(layout.install_root(), Path(d))
            finally:
                del os.environ["LASCODIA_SUPERVISOR_HOME"]
