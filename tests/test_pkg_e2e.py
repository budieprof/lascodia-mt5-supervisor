"""End-to-end: the double-click installer (tools/build_pkg.sh -> .pkg) — its postinstall, run the way
Installer runs it (``postinstall <path of the .pkg>``), against the stub engine, in a temp HOME, with
the fake launchctl of test_bootstrap_e2e.  Run as a normal user, so it installs for that user (the
root branch — the console user lookup and ``launchctl asuser`` — is only exercised by a real install).

Covers: the code is read from the file name the console saves ("Lascodia Setup <code> (1).pkg" after a
second download); the package carries the engine address it was built for; a renamed package without
the code fails with a plain message and installs nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_bootstrap_e2e as base
from test_bootstrap_e2e import TOKEN, _bundle_path

REPO = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "darwin" and _bundle_path() and shutil.which("pkgbuild"),
                     "needs macOS, pkgbuild and a built bundle (make bundle)")
class PkgE2E(base.BootstrapE2E):
    def setUp(self):
        super().setUp()
        out = self.d / "pkg-out"
        r = subprocess.run([str(REPO / "tools" / "build_pkg.sh"), "--api-base", self.stub.base],
                           capture_output=True, text=True, env={**os.environ, "OUT": str(out)}, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pkg = next(out.glob("lascodia-supervisor-setup-*.pkg"))
        self.expanded = self.d / "pkg-x"
        subprocess.run(["pkgutil", "--expand", str(pkg), str(self.expanded)], check=True)
        self.scripts = self.expanded / "setup.pkg" / "Scripts"

    def run_postinstall(self, pkg_name: str) -> subprocess.CompletedProcess:
        env = self.env()
        env.pop("LASCODIA_API_BASE")            # the address must come from the package itself
        env.update({"LASCODIA_PKG_NO_DIALOG": "1", "LASCODIA_PKG_LOG": str(self.d / "install.log")})
        return subprocess.run([str(self.scripts / "postinstall"), f"/Users/x/Downloads/{pkg_name}", "/", "/"],
                              capture_output=True, text=True, env=env, timeout=180)

    def test_install_upgrade_selfupdate_rollback_uninstall(self):   # the one-line flow is covered by the parent
        self.skipTest("covered by test_bootstrap_e2e")

    def test_double_click_install(self):
        self.assertIn(f"API_BASE='{self.stub.base}'", (self.scripts / "install.sh").read_text())
        r = self.run_postinstall(f"Lascodia Setup {TOKEN} (1).pkg")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Connected", r.stdout)
        self.assertEqual(self.stub.enrolls[0]["kind"], "Daemon")
        self.assertTrue((self.root / "config" / "machine_credential.json").exists())
        self.assertIn("Connected", (self.d / "install.log").read_text())

        # opened again (an update): same computer, no second enrollment
        r = self.run_postinstall(f"Lascodia Setup {TOKEN}.pkg")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Upgrading", r.stdout)
        self.assertEqual(len(self.stub.enrolls), 1)

    def test_renamed_package_without_code(self):
        r = self.run_postinstall("installer.pkg")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no install code", r.stdout)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
