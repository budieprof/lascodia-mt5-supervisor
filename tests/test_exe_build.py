"""Double-click Windows installer: the .exe builds from install.ps1 with the engine address baked in.

Running it needs Windows (see the Windows VM check in the self-heal program log); here the build
and its guards are checked. Skipped without makensis.
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "tools" / "build_exe.sh"
API = "https://app.example.com/api/v1/lascodia-trading-engine"

pytestmark = pytest.mark.skipif(shutil.which("makensis") is None, reason="makensis not installed")


def build(tmp_path, *args):
    return subprocess.run(["bash", str(BUILD), *args], cwd=REPO, env={**os.environ, "OUT": str(tmp_path)},
                          capture_output=True, text=True, timeout=300)


def test_builds_a_windows_executable_with_its_checksum(tmp_path):
    r = build(tmp_path, "--api-base", API)
    assert r.returncode == 0, r.stderr + r.stdout
    version = (REPO / "bootstrap" / "windows-exe" / "VERSION").read_text().strip()
    exe = tmp_path / f"lascodia-supervisor-setup-{version}.exe"
    data = exe.read_bytes()
    assert data[:2] == b"MZ" and b"PE\0\0" in data[:4096]
    assert (tmp_path / (exe.name + ".sha256")).read_text().strip() == hashlib.sha256(data).hexdigest()
    assert API in r.stdout


def test_refuses_an_unsafe_or_missing_engine_address(tmp_path):
    assert build(tmp_path, "--api-base", "https://x.example.com/'; Remove-Item C:\\ #").returncode != 0
    assert build(tmp_path).returncode != 0
    unbaked = tmp_path / "install.ps1"
    shutil.copy(REPO / "bootstrap" / "install.ps1", unbaked)   # still has the placeholder
    r = build(tmp_path, "--install-ps1", str(unbaked))
    assert r.returncode != 0 and "engine address" in r.stderr


def test_the_scripts_it_ships_are_ascii():
    # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page.
    for f in ("bootstrap/install.ps1", "bootstrap/windows-exe/setup.ps1"):
        (REPO / f).read_bytes().decode("ascii")
