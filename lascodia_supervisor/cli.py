"""Command line: ``python -m lascodia_supervisor <command>``.

  run                     run the daemon (default; what launchd / the Scheduled Task start)
  version | --version     print the version
  platform                print this computer's bundle platform (macos-arm64, windows-x64, ...)
  detect-installs         print the MetaTrader installs found here (YAML; --json)
  setup                   write / upgrade <root>/config/supervisor.yaml (used by the installers)
  wait-registered         wait until the daemon has registered with the engine; print one sentence
  verify-bundle DIR       check DIR against its MANIFEST.sha256

The installers call ``setup`` and ``wait-registered`` so the shell / PowerShell side stays thin.
The enrollment token reaches ``setup`` through the ``LASCODIA_ENROLLMENT_TOKEN`` environment
variable, never the command line (no other user can read it from the process list).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import string
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__

DEFAULT_PORT = 7770


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# ── setup ────────────────────────────────────────────────────────────────────

def sanitized_hostname(raw: Optional[str] = None) -> str:
    name = (raw or socket.gethostname() or "computer").split(".")[0].lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return (name or "computer")[:40]


def new_daemon_id(hostname: Optional[str] = None) -> str:
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{sanitized_hostname(hostname)}-{suffix}"


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    for fam, addr in ((socket.AF_INET, host), (socket.AF_INET, "0.0.0.0")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def pick_port(start: int = DEFAULT_PORT, tries: int = 30) -> int:
    for p in range(start, start + tries):
        if port_free(p):
            return p
    return start


def run_setup(root: Path, engine_url: str, token: str = "", home: Optional[str] = None,
              apps_dir: Optional[str] = None, hostname: Optional[str] = None,
              detected: Optional[list] = None) -> dict:
    """Create or upgrade the config.  Returns a summary dict (also used by tests).

    New install: fresh identity (daemon_id = hostname + 4 random characters, daemon_api_key),
    ``engine_resolver: auto``, ``bind_host: 127.0.0.1``, every detected terminal.
    Existing install: identity, engine URL, ports and every configured install are kept exactly;
    newly found terminals are appended; the token is stored only while no machine credential
    exists (it is single use and only needed to enroll)."""
    import yaml

    from .detect import detect, merge_installs
    from .machine_credentials import is_enrollment_token, write_private_file

    cfg_dir = root / "config"
    cfg_path = cfg_dir / "supervisor.yaml"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    found = detected if detected is not None else detect(home=home, apps_dir=apps_dir)
    token = (token or "").strip()
    if token and not is_enrollment_token(token):
        raise ValueError("the install code in this line is not valid — copy the whole line from the console again")

    existing: Optional[dict] = None
    if cfg_path.exists():
        existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    if existing is None:
        port = pick_port()
        host = hostname or socket.gethostname().split(".")[0]
        cfg = {
            "daemon_id": new_daemon_id(hostname),
            "name": host,
            "bind_host": "127.0.0.1",
            "bind_port": port,
            "callback_base_url": f"http://127.0.0.1:{port}",
            "engine_url": engine_url.rstrip("/"),
            "engine_resolver": "auto",
            "daemon_api_key": secrets.token_urlsafe(32),
            "enrollment_token": token,
            "heartbeat_interval_sec": 30,
            "watchdog": {"enabled": True, "max_restarts_per_hour": 3, "max_restarts_per_day": 8},
            "jobs_enabled": True,
            "installs": found,
        }
        added, created, notes, changed = found, True, [], True
    else:
        cfg = dict(existing)
        notes = []
        if engine_url and cfg.get("engine_url") and cfg["engine_url"].rstrip("/") != engine_url.rstrip("/"):
            notes.append(f"kept the engine address already set up here ({cfg['engine_url']})")
        cfg.setdefault("engine_url", engine_url.rstrip("/"))
        cred = Path(cfg.get("credential_path") or (cfg_dir / "machine_credential.json"))
        if token and not cred.exists():
            cfg["enrollment_token"] = token
        installs, added = merge_installs(list(cfg.get("installs") or []), found)
        cfg["installs"] = installs
        created = False
        changed = bool(added) or cfg.get("enrollment_token") != existing.get("enrollment_token") \
            or "engine_url" not in existing
        if changed:
            backup = cfg_path.with_name(f"supervisor.yaml.bak-{time.strftime('%Y%m%d%H%M%S')}")
            write_private_file(backup, cfg_path.read_text(encoding="utf-8"))

    header = ("# Written by the Lascodia supervisor installer.  Re-running the install line upgrades in place\n"
              "# and keeps this computer's identity; edit by hand only if you know you need to.\n")
    if changed:
        write_private_file(cfg_path, header + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return {"configPath": str(cfg_path), "created": created, "daemonId": cfg["daemon_id"],
            "installs": cfg["installs"], "added": added, "notes": notes,
            "bindHost": cfg.get("bind_host", "127.0.0.1"), "bindPort": cfg.get("bind_port", DEFAULT_PORT)}


def _describe(rows: list) -> str:
    names = [r.get("name") or r.get("install_id") for r in rows]
    return ", ".join(str(n) for n in names)


def cmd_setup(a) -> int:
    root = Path(a.root)
    try:
        summary = run_setup(root, a.engine_url, os.environ.get("LASCODIA_ENROLLMENT_TOKEN", ""),
                            home=a.home, apps_dir=a.apps_dir)
    except ValueError as ex:
        _say(f"Stopped: {ex}.")
        return 2
    n = len(summary["installs"])
    if summary["created"]:
        if n:
            _say(f"Found {n} MetaTrader 5 terminal{'s' if n != 1 else ''}: {_describe(summary['installs'])}.")
        else:
            _say("No MetaTrader 5 terminal found on this computer yet. Install MetaTrader 5 and log in once; "
                 "then paste the install line again to add it.")
    else:
        if summary["added"]:
            _say(f"Added {len(summary['added'])} new terminal(s): {_describe(summary['added'])}.")
        _say(f"Kept this computer's existing setup ({n} terminal{'s' if n != 1 else ''}).")
    for note in summary["notes"]:
        _say(f"Note: {note}.")
    return 0


# ── wait-registered ──────────────────────────────────────────────────────────

def cmd_wait_registered(a) -> int:
    from .status import read_status
    root = Path(a.root)
    state = root / "config" / "state"
    deadline = time.time() + a.timeout
    last = None
    while time.time() < deadline:
        st = read_status(state) or {}
        if st.get("startedAt", 0) >= a.since:
            last = st
            if st.get("registered") and st.get("registeredAt", 0) >= a.since:
                n = int(st.get("installCount") or 0)
                found = f"{n} terminal{'s' if n != 1 else ''} found" if n else "no terminals found yet"
                _say(f"Connected: {found}. Go back to the console to finish.")
                return 0
        time.sleep(1)
    if last is None:
        _say("The supervisor did not start. Its log is in " + str(root / "logs") +
             " — send that folder to support if pasting the line again does not help.")
        return 1
    err = last.get("lastError") or "It started but has not connected to the engine yet."
    _say(f"Not connected yet: {err} It keeps trying in the background; if the console does not show this "
         "computer within a few minutes, paste the line again.")
    return 1


# ── verify-bundle ────────────────────────────────────────────────────────────

def cmd_verify_bundle(a) -> int:
    from .selfupdate import verify_manifest
    ok, why = verify_manifest(Path(a.dir))
    _say(("OK: " if ok else "FAILED: ") + why)
    return 0 if ok else 1


def cmd_detect(a) -> int:
    from .detect import detect
    rows = detect(home=a.home, apps_dir=a.apps_dir)
    if a.json:
        _say(json.dumps(rows, indent=1))
    else:
        import yaml
        _say(yaml.safe_dump({"installs": rows}, sort_keys=False, allow_unicode=True).rstrip())
    return 0


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "run":
        from .main import run
        run()
        return 0
    if argv[0] in ("version", "--version", "-V"):
        _say(__version__)
        return 0
    if argv[0] == "platform":
        from .layout import platform_tag
        _say(platform_tag())
        return 0

    p = argparse.ArgumentParser(prog="lascodia_supervisor")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect-installs")
    d.add_argument("--json", action="store_true")
    d.add_argument("--home")
    d.add_argument("--apps-dir")
    s = sub.add_parser("setup")
    s.add_argument("--root", required=True)
    s.add_argument("--engine-url", required=True)
    s.add_argument("--home")
    s.add_argument("--apps-dir")
    w = sub.add_parser("wait-registered")
    w.add_argument("--root", required=True)
    w.add_argument("--since", type=float, default=0.0)
    w.add_argument("--timeout", type=float, default=60.0)
    v = sub.add_parser("verify-bundle")
    v.add_argument("dir")
    a = p.parse_args(argv)
    return {"detect-installs": cmd_detect, "setup": cmd_setup, "wait-registered": cmd_wait_registered,
            "verify-bundle": cmd_verify_bundle}[a.cmd](a)
