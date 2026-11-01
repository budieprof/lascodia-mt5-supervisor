"""End-to-end: the real bootstrap/install.sh + the real macOS bundle, against a stub engine, in a temp
HOME, with a fake launchctl (emulates KeepAlive by restarting bin/run.sh when it exits).

Covers: one-line install → "Connected"; re-run = upgrade keeping identity; self-update to a newer
bundle (confirmed after registering); a broken newer bundle rolled back by the launcher; --uninstall.

Needs a built bundle for this Mac: ``make bundle`` (or set LASCODIA_E2E_BUNDLE).  Skipped otherwise.
Never touches the real launchd, /Applications terminals or the running supervisor: launchctl is a
fake, the label is a test label, HOME and the apps folder are temp dirs with no MetaTrader in them.
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from engine_stub import EngineStub

REPO = Path(__file__).resolve().parent.parent
TOKEN = "lsc_enr_e2etest1_" + "C" * 40
PLATFORM = "macos-arm64" if platform.machine() == "arm64" else "macos-x86_64"


def _bundle_path() -> Path | None:
    env = os.environ.get("LASCODIA_E2E_BUNDLE")
    if env:
        return Path(env)
    hits = sorted((REPO / "dist" / "bundles").glob(f"lascodia-supervisor-*-{PLATFORM}.tar.gz"))
    return hits[-1] if hits else None


FAKE_LAUNCHCTL = r"""#!/bin/sh
S="$FAKE_LAUNCHCTL_STATE"
case "$1" in
  print) [ -f "$S/pid" ] && kill -0 "$(cat "$S/pid")" 2>/dev/null; exit $? ;;
  bootstrap)
    prog=$(/usr/bin/plutil -extract ProgramArguments.1 raw -o - "$3")
    rm -f "$S/stop"
    ( while [ ! -f "$S/stop" ]; do
        /bin/sh "$prog" >>"$S/daemon.log" 2>&1 &
        echo $! > "$S/child"; wait $!
        [ -f "$S/stop" ] && break; sleep 1
      done ) </dev/null >/dev/null 2>&1 &
    echo $! > "$S/pid"; exit 0 ;;
  bootout)
    touch "$S/stop"
    [ -f "$S/child" ] && kill "$(cat "$S/child")" 2>/dev/null
    i=0; while [ $i -lt 20 ] && [ -f "$S/child" ] && kill -0 "$(cat "$S/child")" 2>/dev/null; do sleep 0.5; i=$((i+1)); done
    kill "$(cat "$S/pid")" 2>/dev/null; rm -f "$S/pid"; exit 0 ;;
  kickstart) exit 0 ;;
esac
exit 0
"""


def derive_bundle(src: Path, dest_dir: Path, version: str, broken: bool = False) -> tuple[bytes, str]:
    """A copy of the real bundle claiming ``version``; ``broken`` = ``run`` dies at start-up while
    ``version`` still answers (so it passes the pre-flip smoke test, as a subtle bad release would)."""
    work = dest_dir / f"b-{version}"
    work.mkdir(parents=True)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    site = work / "python" / "lib" / "python3.12" / "site-packages" / "lascodia_supervisor"
    init = site / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text()))
    (work / "VERSION").write_text(version + "\n")
    if broken:
        (site / "main.py").write_text("raise SystemExit('this release is broken on purpose')\n")
    rows = []
    for p in sorted(work.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts:
            rows.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(work).as_posix()}\n")
    (work / "MANIFEST.sha256").write_text("".join(rows))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in sorted(work.iterdir()):
            t.add(p, arcname=p.name)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


@unittest.skipUnless(sys.platform == "darwin" and _bundle_path(), "needs macOS and a built bundle (make bundle)")
class BootstrapE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lasc-e2e-")
        self.d = Path(self.tmp.name)
        self.home = self.d / "home"
        (self.home / "Library").mkdir(parents=True)
        (self.d / "apps").mkdir()
        self.state = self.d / "launchctl"
        self.state.mkdir()
        self.fake = self.d / "launchctl.sh"
        self.fake.write_text(FAKE_LAUNCHCTL)
        self.fake.chmod(0o755)
        self.root = self.home / "Library" / "Application Support" / "Lascodia" / "supervisor"
        self.bundle = _bundle_path()
        data = self.bundle.read_bytes()
        self.version = tarfile.open(self.bundle).extractfile("./VERSION").read().decode().strip()
        self.stub = EngineStub(TOKEN).__enter__()
        self.stub.bundles[PLATFORM] = (data, self.version, hashlib.sha256(data).hexdigest())

    def tearDown(self):
        (self.state / "stop").touch()
        for f in ("child", "pid"):
            try:
                os.kill(int((self.state / f).read_text()), 15)
            except (OSError, ValueError):
                pass
        time.sleep(1)
        self.stub.__exit__()
        self.tmp.cleanup()

    def env(self) -> dict:
        return {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(self.d),
                "LASCODIA_API_BASE": self.stub.base, "LASCODIA_LAUNCHCTL": str(self.fake),
                "FAKE_LAUNCHCTL_STATE": str(self.state), "LASCODIA_LAUNCHD_LABEL": "com.lascodia.supervisor.e2etest",
                "LASCODIA_APPS_DIR": str(self.d / "apps"), "LASCODIA_WAIT_SEC": "45",
                "LASCODIA_UPDATE_CONFIRM_SEC": "6", "LASCODIA_UPDATE_SLACK_SEC": "2"}

    def install(self, *args: str) -> subprocess.CompletedProcess:
        script = (REPO / "bootstrap" / "install.sh").read_text()
        # exactly how the console line runs it: the script arrives on stdin
        return subprocess.run(["sh", "-s", "--", *args], input=script, capture_output=True, text=True,
                              env=self.env(), timeout=180)

    def cfg(self) -> dict:
        return yaml.safe_load((self.root / "config" / "supervisor.yaml").read_text())

    def wait_for(self, cond, timeout: float, what: str):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.5)
        log = (self.state / "daemon.log").read_text(errors="replace")[-3000:] if (self.state / "daemon.log").exists() else ""
        sup = self.root / "logs" / "supervisor.log"
        tail = sup.read_text(errors="replace")[-3000:] if sup.exists() else ""
        self.fail(f"timed out waiting for {what}\n--- launchd output\n{log}\n--- supervisor.log\n{tail}")

    def versions_seen(self) -> list:
        return [h.get("supervisorVersion") for h in list(self.stub.heartbeats)]

    def test_install_upgrade_selfupdate_rollback_uninstall(self):
        # 1. the one line
        r = self.install(TOKEN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Connected", r.stdout)
        self.assertIn("No MetaTrader 5 terminal found", r.stdout)
        cfg = self.cfg()
        self.assertEqual((cfg["engine_resolver"], cfg["bind_host"]), ("auto", "127.0.0.1"))
        self.assertTrue((self.root / "config" / "machine_credential.json").exists())
        self.assertEqual(os.readlink(self.root / "current"), f"versions/{self.version}")
        plist = (self.home / "Library" / "LaunchAgents" / "com.lascodia.supervisor.e2etest.plist").read_text()
        self.assertIn(str(self.root / "bin" / "run.sh"), plist)
        self.assertIn("AbandonProcessGroup", plist)
        self.wait_for(lambda: self.stub.heartbeats, 30, "the first heartbeat")
        hb = self.stub.heartbeats[-1]
        self.assertEqual((hb["supervisorVersion"], hb["platform"]), (self.version, PLATFORM))
        self.assertEqual(self.stub.enrolls[0]["kind"], "Daemon")

        # 2. paste it again = upgrade in place, same identity (and a faster heartbeat for the test)
        cfg["heartbeat_interval_sec"] = 5
        (self.root / "config" / "supervisor.yaml").write_text(yaml.safe_dump(cfg))
        r = self.install(TOKEN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Upgrading", r.stdout)
        self.assertEqual(self.cfg()["daemon_id"], cfg["daemon_id"])
        self.assertEqual(len(self.stub.enrolls), 1)          # kept its credential: no second enrollment

        # 3. self-update to a newer release
        newer = ".".join(self.version.split(".")[:2] + [str(int(self.version.split(".")[2]) + 1)])
        data, sha = derive_bundle(self.bundle, self.d, newer)
        self.stub.releases["11"] = data
        self.stub.offer = {"version": newer, "platform": PLATFORM, "releaseId": 11, "sha256": sha}
        self.wait_for(lambda: newer in self.versions_seen(), 90, f"a heartbeat from {newer}")
        self.wait_for(lambda: not (self.root / "update.pending").exists(), 30, "the update to be confirmed")
        self.assertEqual(os.readlink(self.root / "current"), f"versions/{newer}")
        self.assertIn(f"version={newer}", (self.root / "update.last").read_text())

        # 4. a broken release is rolled back by the launcher and never retried
        broken = ".".join(newer.split(".")[:2] + [str(int(newer.split(".")[2]) + 1)])
        data, sha = derive_bundle(self.bundle, self.d, broken, broken=True)
        self.stub.releases["12"] = data
        self.stub.offer = {"version": broken, "platform": PLATFORM, "releaseId": 12, "sha256": sha}
        self.wait_for(lambda: (self.root / "update.rolledback").exists(), 120, "the rollback")
        self.assertEqual(os.readlink(self.root / "current"), f"versions/{newer}")
        self.assertIn(broken, (self.root / "update.bad").read_text())
        n = len(self.stub.heartbeats)
        self.wait_for(lambda: len(self.stub.heartbeats) > n + 1, 60, "heartbeats after the rollback")
        self.assertEqual(self.stub.heartbeats[-1]["supervisorVersion"], newer)
        self.assertEqual(os.readlink(self.root / "current"), f"versions/{newer}")

        # 5. uninstall
        r = self.install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Removed", r.stdout)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.lascodia.supervisor.e2etest.plist").exists())

    def test_failures_say_what_to_do(self):
        r = self.install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing its install code", r.stderr)
        r = self.install("lsc_enr_wrongone_" + "D" * 40)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("was not accepted", r.stderr)
        self.assertIn("Add a computer again", r.stderr)
        self.assertFalse((self.root / "config" / "supervisor.yaml").exists())
        self.stub.bundles[PLATFORM] = (b"garbage", "9.9.9", hashlib.sha256(b"different").hexdigest())
        r = self.install(TOKEN)
        self.assertIn("damaged", r.stderr)
        self.assertFalse((self.root / "current").exists())
