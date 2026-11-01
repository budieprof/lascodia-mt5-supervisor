"""Engine-host operations (gaps program, zero-downtime): the console's Operations → Platform page.

Job types (contract C4 job channel, host-wide — no install):
  HostOpsStatus     report blue/green state + swap history, colour containers, the deployable image, backups, drills
  HostBackupNow     run ``scripts/backup/backup-nightly.sh``
  HostRestoreDrill  run ``scripts/backup/run-restore-drill.sh``
  HostDeployApi     run ``scripts/redeploy-api-bluegreen.sh`` ({imageId, rollback, dryRun})

They run HERE, on the engine host, outside every engine container — a deploy is never run by the process it replaces.
Off unless ``ops.enabled`` (supervisor.yaml); deploys additionally need ``ops.allow_deploy``. The engine checks its own
gates (Ops:ConsoleDeploy:Enabled, typed confirmation, high-risk window, four-eyes) before it ever queues a deploy; this
side is defence in depth and the only side that can touch docker.

Long jobs stream their output to the engine (``POST …/jobs/{id}/progress``), best effort: a deploy disconnects the
engine for a few seconds, so unsent lines are kept and sent with the next batch.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("lascodia.supervisor.hostops")

SUCCEEDED, FAILED = "Succeeded", "Failed"
HOST_JOB_TYPES = ("HostOpsStatus", "HostBackupNow", "HostRestoreDrill", "HostDeployApi")
_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_PROGRESS_BATCH = 16 * 1024


class OpsSettings:
    """What hostops needs from ``SupervisorConfig.ops`` (kept separate so tests build it directly)."""

    def __init__(self, enabled: bool, engine_repo: str, allow_deploy: bool = False, backup_dir: str = "",
                 bluegreen_state_dir: str = "", caddyfile: str = "/opt/homebrew/etc/Caddyfile",
                 compose_project: str = "lascodia-trading-engine", docker: str = "docker",
                 deploy_timeout_sec: int = 2400, backup_timeout_sec: int = 6 * 3600) -> None:
        self.enabled = enabled
        self.engine_repo = Path(os.path.expanduser(engine_repo)) if engine_repo else None
        self.allow_deploy = allow_deploy
        self.backup_dir = Path(os.path.expanduser(backup_dir or "~/LascodiaBackups"))
        self.bluegreen_state_dir = Path(os.path.expanduser(bluegreen_state_dir or "~/.lascodia/bluegreen"))
        self.caddyfile = Path(caddyfile)
        self.compose_project = compose_project
        self.docker = docker
        self.deploy_timeout_sec = deploy_timeout_sec
        self.backup_timeout_sec = backup_timeout_sec

    @classmethod
    def from_config(cls, ops) -> "OpsSettings":
        if ops is None:
            return cls(False, "")
        return cls(ops.enabled, ops.engine_repo or "", ops.allow_deploy, ops.backup_dir or "",
                   ops.bluegreen_state_dir or "", ops.caddyfile, ops.compose_project, ops.docker,
                   ops.deploy_timeout_sec, ops.backup_timeout_sec)


# ── running a host script with streamed output ─────────────────────────────────

class ProgressSink:
    """Collects output lines and ships them in batches (best effort, never blocks the script)."""

    def __init__(self, send: Optional[Callable[[str], bool]], every_sec: float = 2.0) -> None:
        self._send = send
        self._every = every_sec
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._last = 0.0
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        line = line.rstrip("\n")
        with self._lock:
            self.lines.append(line)
            self._buf.append(line)
        if time.monotonic() - self._last >= self._every:
            self.flush()

    def flush(self) -> None:
        if self._send is None:
            return
        with self._lock:
            if not self._buf:
                return
            text = "\n".join(self._buf)[-MAX_PROGRESS_BATCH:]
            self._last = time.monotonic()
        try:
            ok = self._send(text)
        except Exception:  # pragma: no cover - the engine client never raises, but be sure
            ok = False
        if ok:
            with self._lock:
                self._buf.clear()


def run_streamed(argv: list[str], cwd: Optional[Path], env: dict, timeout_sec: int, sink: ProgressSink,
                 popen=subprocess.Popen) -> int:
    """Run ``argv`` (no shell), stream stdout+stderr lines into ``sink``; kill it after ``timeout_sec``."""
    proc = popen(argv, cwd=str(cwd) if cwd else None, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 text=True, bufsize=1)
    killer = threading.Timer(timeout_sec, lambda: proc.kill())
    killer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sink.add(line)
        return proc.wait()
    finally:
        killer.cancel()
        sink.flush()


# ── status ──────────────────────────────────────────────────────────────────────

def _kv_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _docker_json(settings: OpsSettings, args: list[str], run=subprocess.run) -> Optional[object]:
    try:
        res = run([settings.docker, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def collect_status(settings: OpsSettings, run=subprocess.run) -> dict:
    """Everything the Platform page shows that only the host knows. Never includes a secret."""
    status: dict = {"collectedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    bg = settings.bluegreen_state_dir
    status["bluegreen"] = {"state": _kv_file(bg / "state"), "history": _tail(bg / "history", 20),
                           "swapInProgress": (bg / "swap.lock").exists()}

    containers = []
    for service in ("api-blue", "api-green", "engine-gw", "api", "alertmanager", "research", "llm"):
        name = f"{settings.compose_project}-{service}-1"
        data = _docker_json(settings, ["inspect", name], run)
        if not isinstance(data, list) or not data:
            containers.append({"service": service, "exists": False})
            continue
        c = data[0]
        st = c.get("State") or {}
        containers.append({
            "service": service, "exists": True, "running": bool(st.get("Running")),
            "status": st.get("Status"), "startedAt": st.get("StartedAt"), "finishedAt": st.get("FinishedAt"),
            "health": (st.get("Health") or {}).get("Status"), "image": c.get("Image"),
            "restartCount": c.get("RestartCount"),
        })
    status["containers"] = containers

    image = _docker_json(settings, ["image", "inspect", "lascodia-trading-engine-api:latest"], run)
    status["deployableImage"] = (
        {"id": image[0].get("Id"), "created": image[0].get("Created"),
         "revision": ((image[0].get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.revision")}
        if isinstance(image, list) and image else None)

    status["backups"] = collect_backups(settings)
    try:
        status["caddyBlueGreen"] = "engine_money_path" in settings.caddyfile.read_text(errors="replace")
    except OSError:
        status["caddyBlueGreen"] = None
    status["opsDeployAllowed"] = settings.allow_deploy
    return status


def collect_backups(settings: OpsSettings) -> dict:
    d = settings.backup_dir
    out: dict = {"dir": str(d), "exists": d.is_dir()}
    last = _tail(d / "status" / "db-last-success", 1)
    out["lastSuccess"] = last[0] if last else None
    manifests = sorted((d / "db").rglob("lascodia-*.manifest")) if (d / "db").is_dir() else []
    if manifests:
        m = _kv_file(manifests[-1])
        total = sum(int(v) for k, v in m.items() if k.startswith("bytes.") and v.isdigit())
        out["newest"] = {"manifest": manifests[-1].name, "createdUtc": m.get("created_utc"),
                         "latestMigration": m.get("latest_migration"), "encryption": m.get("encryption"),
                         "bytes": total, "durationSeconds": m.get("duration_seconds")}
        out["count"] = len(manifests)
    drill = d / "status" / "drill-last.json"
    try:
        out["lastDrill"] = json.loads(drill.read_text())
    except (OSError, ValueError):
        out["lastDrill"] = None
    # Destination + schedule — never the keys.
    env_file = settings.engine_repo / "scripts" / "backup" / "backup.env" if settings.engine_repo else None
    env = _kv_file(env_file) if env_file else {}
    out["configured"] = bool(env)
    out["remote"] = env.get("BACKUP_REMOTE", "none") if env else None
    out["remoteUrl"] = env.get("BACKUP_REMOTE_URL") if env and env.get("BACKUP_REMOTE", "none") != "none" else None
    out["encryption"] = env.get("BACKUP_ENCRYPTION") if env else None
    plist = Path(os.path.expanduser("~/Library/LaunchAgents/com.lascodia.backup.plist"))
    out["scheduleInstalled"] = plist.exists()
    if plist.exists():
        text = plist.read_text(errors="replace")
        h = re.search(r"<key>Hour</key><integer>(\d+)</integer>", text)
        mi = re.search(r"<key>Minute</key><integer>(\d+)</integer>", text)
        if h and mi:
            out["schedule"] = f"daily {int(h.group(1)):02d}:{int(mi.group(1)):02d} local time"
    return out


# ── the dispatcher ─────────────────────────────────────────────────────────────

def _script_env(settings: OpsSettings, extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
    env.update(extra or {})
    return env


def execute(job: dict, settings: OpsSettings, send_progress: Optional[Callable[[str], bool]],
            run=subprocess.run, popen=subprocess.Popen) -> tuple[str, str, dict]:
    jtype = str(job.get("type") or "")
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    if not settings.enabled:
        return FAILED, ("host operations are off on this supervisor — set ops.enabled: true and ops.engine_repo in "
                        "supervisor.yaml on the ENGINE host"), {}
    repo = settings.engine_repo
    if repo is None or not (repo / "docker-compose.yml").is_file():
        return FAILED, f"ops.engine_repo ({repo}) is not the engine checkout (no docker-compose.yml)", {}

    if jtype == "HostOpsStatus":
        return SUCCEEDED, "host status collected", collect_status(settings, run)

    sink = ProgressSink(send_progress)
    if jtype == "HostBackupNow":
        rc = run_streamed(["/bin/bash", str(repo / "scripts" / "backup" / "backup-nightly.sh")], repo,
                          _script_env(settings), settings.backup_timeout_sec, sink, popen)
        data = {"exitCode": rc, "backups": collect_backups(settings), "tail": sink.lines[-40:]}
        return (SUCCEEDED if rc == 0 else FAILED), \
            ("backup completed" if rc == 0 else f"backup FAILED (exit {rc}) — see the output"), data

    if jtype == "HostRestoreDrill":
        rc = run_streamed(["/bin/bash", str(repo / "scripts" / "backup" / "run-restore-drill.sh")], repo,
                          _script_env(settings), settings.backup_timeout_sec, sink, popen)
        data = {"exitCode": rc, "lastDrill": collect_backups(settings).get("lastDrill"), "tail": sink.lines[-40:]}
        if rc == 75:
            return FAILED, "the heavy-job slot was busy (a build or another drill) — run the drill again later", data
        return (SUCCEEDED if rc == 0 else FAILED), \
            ("restore drill PASSED" if rc == 0 else f"restore drill {'REFUSED' if rc == 2 else 'FAILED'} (exit {rc})"), data

    if jtype == "HostDeployApi":
        if not settings.allow_deploy:
            return FAILED, "deploys are off on this supervisor (ops.allow_deploy: false in supervisor.yaml)", {}
        rollback = params.get("rollback") is True
        dry_run = params.get("dryRun") is True
        argv = ["/bin/bash", str(repo / "scripts" / "redeploy-api-bluegreen.sh")]
        extra = {}
        if rollback:
            argv.append("--rollback")
        else:
            image = str(params.get("imageId") or "")
            if not _IMAGE_RE.match(image):
                return FAILED, "HostDeployApi needs imageId sha256:<64 hex>", {}
            extra["EXPECT_IMAGE_ID"] = image
        if dry_run:
            argv.append("--dry-run")
        rc = run_streamed(argv, repo, _script_env(settings, extra), settings.deploy_timeout_sec, sink, popen)
        state = _kv_file(settings.bluegreen_state_dir / "state")
        data = {"exitCode": rc, "dryRun": dry_run, "rollback": rollback, "state": state, "tail": sink.lines[-60:]}
        if rc == 0:
            what = "dry run complete (nothing changed)" if dry_run else \
                f"{state.get('active', '?')} leads (handover {state.get('handover_seconds', '?')} s)"
            return SUCCEEDED, ("rollback: " if rollback else "deploy: ") + what, data
        return FAILED, {2: "refused by the swap script's preflight — nothing changed",
                        1: "the swap FAILED — the output says which colour serves"}.get(rc, f"exit {rc}"), data

    return FAILED, f"unknown host job type '{jtype}'", {}
