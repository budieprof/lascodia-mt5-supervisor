"""FastAPI entrypoint for the Lascodia MT5 Supervisor daemon."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
import uvicorn

from . import __version__, layout
from .admin_api import AdminApiState, build_router as build_admin_router
from .config import SupervisorConfig, default_config_path, load_config
from .engine_client import EngineClient
from .logsetup import setup_logging, supervisor_log_path
from .machine_credentials import prepare_terminal_enrollment
from .process_manager import ProcessManager
from .runtime import FAILED, Supervisor
from .selfupdate import Updater, extract_offer
from .status import write_status

logger = logging.getLogger("lascodia.supervisor")


class LaunchRequest(BaseModel):
    installId: str
    reason: Optional[str] = None


class LaunchResponse(BaseModel):
    sessionId: str
    pid: Optional[int]
    status: str


class _State:
    """Process-wide singleton holding config + the runtime managers."""
    cfg: SupervisorConfig
    pm: ProcessManager
    engine: EngineClient
    sup: Supervisor
    heartbeat_task: Optional[asyncio.Task] = None
    jobs_task: Optional[asyncio.Task] = None
    updater: Optional[Updater] = None
    update_task: Optional[asyncio.Task] = None
    registered: bool = False
    running_jobs: set = set()

    def busy(self) -> bool:
        """No self-update while a job or a terminal action is in flight."""
        return bool(self.running_jobs) or (getattr(self, "sup", None) is not None and self.sup.any_busy())

    def reload_config(self) -> None:
        """Re-read supervisor.yaml and rebuild the EngineClient so its
        cached base_url + apiKey reflect the new state.  Called by the
        admin API after every YAML mutation.  Heartbeat loop continues
        ticking against the new config on its next iteration.

        Re-creating EngineClient drops the cached JWT, which is fine —
        the lazy /auth bootstrap re-fetches one on the next call.
        """
        new_cfg = load_config()
        self.cfg = new_cfg
        # Engine client holds an httpx.Client with the cfg.engine_url
        # baked in.  Close the old one cleanly to release the socket
        # pool, then build a fresh client.
        try:
            self.engine.close()
        except Exception:
            pass
        self.engine = EngineClient(new_cfg)
        if getattr(self, "sup", None) is not None:
            self.sup.engine = self.engine
            self.sup.jobs.engine = self.engine


STATE = _State()


def _installs_payload(cfg: SupervisorConfig) -> list[dict]:
    # Lean shape sent to the engine on register / heartbeat — engine
    # caches as InstallsJson (jsonb) and surfaces verbatim to the
    # admin UI in the Terminals page card.  Wave-2 admin endpoints
    # serve a richer DTO (wineBinary / winePrefix / etc.); this lean
    # shape carries only what the operator sees in the install list +
    # the protection flag the admin UI uses to hide the delete button.
    from .admin_api import _is_protected_install  # local to avoid cycle
    return [
        {
            "installId":    i.install_id,
            "name":         i.name,
            "executable":   i.executable,
            "brokerName":   i.broker_name,
            "accountLogin": i.account_login,
            "isDefault":    _is_protected_install({
                "install_id": i.install_id,
                "executable": i.executable,
            }),
        }
        for i in cfg.installs
    ]


async def _heartbeat_loop() -> None:
    """Refresh process state then push to engine.  Drives the engine-side
    'IsOnline' computation — a missed heartbeat tips the daemon offline.

    Also pushes the current installs list so the engine's InstallsJson
    cache reflects out-of-band changes (operator deletes / patches via
    /admin/installs/*) within one heartbeat interval — without this,
    the admin UI's Terminals page renders stale install rows until the
    daemon re-registers."""
    while True:
        try:
            # Discover + adopt, read beacons / journal, run the watchdog (actions run on the
            # supervisor's own threads), then report the C4 runtime and take the engine's
            # desired state back.  Blocking work stays off the event loop.
            await asyncio.to_thread(STATE.sup.tick)
            if not STATE.registered:
                # The engine was unreachable (or refused us) at start-up: keep trying to register.
                if await asyncio.to_thread(STATE.engine.register, _installs_payload(STATE.cfg)) is not None:
                    _note_registered()
            sessions_payload = [s.to_payload() for s in STATE.pm.list_sessions()]
            installs_payload = STATE.sup.installs_payload(_installs_payload(STATE.cfg))
            ok, body = await asyncio.to_thread(
                STATE.engine.heartbeat_ex, sessions_payload, installs_payload,
                {"supervisorVersion": __version__, "platform": layout.platform_tag()})
            if ok:
                STATE.sup.apply_heartbeat_response(body)
                if STATE.registered:
                    _note_registered(heartbeat=True)
                _consider_update(body)
            else:
                write_status(STATE.cfg.state_dir, lastError=STATE.engine.last_error or
                             "The engine did not accept this computer's heartbeat.")
            _check_update_deadline()
        except Exception:
            logger.exception("heartbeat loop iteration failed; will retry next tick")
        await asyncio.sleep(STATE.cfg.heartbeat_interval_sec)


def _note_registered(heartbeat: bool = False) -> None:
    import time as _t
    first = not STATE.registered
    STATE.registered = True
    fields = {"registered": True, "lastError": "", "installCount": len(STATE.cfg.installs)}
    if first:
        fields["registeredAt"] = _t.time()
    if heartbeat:
        fields["lastHeartbeatAt"] = _t.time()
    write_status(STATE.cfg.state_dir, **fields)
    if first:
        logger.info("registered with engine — daemon_id=%s", STATE.cfg.daemon_id)
    if STATE.updater is not None and STATE.updater.enabled:
        STATE.updater.confirm()


def _request_restart(why: str) -> None:
    """Exit gracefully; launchd (KeepAlive) / the Scheduled Task launcher starts ``current`` again."""
    logger.warning("restarting the supervisor: %s", why)
    signal.raise_signal(signal.SIGTERM)


def _check_update_deadline() -> None:
    up = STATE.updater
    if up is None or not up.enabled:
        return
    res = up.check_deadline()
    if res is not None:
        write_status(STATE.cfg.state_dir, lastUpdate=res.message)
        if res.restart:
            _request_restart(res.message)


def _consider_update(body: dict) -> None:
    up = STATE.updater
    if up is None or (STATE.update_task is not None and not STATE.update_task.done()):
        return
    offer = extract_offer(body)
    if offer is None:
        return
    go, why = up.consider(offer, STATE.busy)
    if not go:
        logger.debug("self-update: %s", why)
        return
    logger.warning("self-update: %s", why)

    async def _apply() -> None:
        res = await asyncio.to_thread(up.apply, offer, STATE.busy)
        write_status(STATE.cfg.state_dir, lastUpdate=res.message)
        (logger.warning if res.ok else logger.error)("self-update: %s", res.message)
        if res.restart:
            _request_restart(res.message)

    STATE.update_task = asyncio.create_task(_apply())


async def _jobs_loop() -> None:
    """Pull job channel (C4): long-poll the engine, run each job on a worker thread, report the
    result.  Results that could not be delivered are retried before the next poll."""
    pending: list[tuple] = []
    backoff = 5
    running: set[asyncio.Task] = set()
    STATE.running_jobs = running

    async def run_one(job: dict) -> None:
        jid = job.get("jobId")
        logger.info("job %s: %s install=%s", jid, job.get("type"), job.get("installId"))
        try:
            status, message, data = await asyncio.to_thread(STATE.sup.run_job, job)
        except Exception as ex:  # pragma: no cover - run_job already catches
            status, message, data = FAILED, f"supervisor error: {ex}", {}
        logger.info("job %s: %s — %s", jid, status, message)
        if not await asyncio.to_thread(STATE.engine.post_job_result, jid, status, message, data):
            pending.append((jid, status, message, data, 0))

    while True:
        if not STATE.cfg.jobs_enabled:
            await asyncio.sleep(30)
            continue
        try:
            for item in list(pending):
                jid, status, message, data, tries = item
                pending.remove(item)
                if not await asyncio.to_thread(STATE.engine.post_job_result, jid, status, message, data) \
                        and tries < 20:
                    pending.append((jid, status, message, data, tries + 1))
            jobs = await asyncio.to_thread(STATE.engine.poll_jobs, STATE.cfg.jobs_wait_sec)
            if jobs is None:
                # Engine unreachable, or an engine without the job channel yet (404): back off.
                await asyncio.sleep(60 if STATE.engine.jobs_unsupported else backoff)
                backoff = min(60, backoff * 2)
                continue
            backoff = 5
            for job in jobs:
                t = asyncio.create_task(run_one(job))
                running.add(t)
                t.add_done_callback(running.discard)
            if not jobs:
                await asyncio.sleep(1)  # the engine already waited waitSec; do not spin if it did not
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job loop iteration failed")
            await asyncio.sleep(backoff)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time as _t
    STATE.cfg = load_config()
    setup_logging(STATE.cfg)
    STATE.engine = EngineClient(STATE.cfg)
    STATE.registered = False
    root = layout.install_root()
    STATE.updater = Updater(root, download=lambda rid: STATE.engine.download_supervisor_release(rid))
    logger.info("lascodia supervisor %s (%s) starting; %s", __version__, layout.platform_tag(),
                f"bundle install at {root}" if root else "not a bundle install — self-update off")
    write_status(STATE.cfg.state_dir, startedAt=_t.time(), pid=os.getpid(), version=__version__,
                 registered=False, registeredAt=0, lastError="", installCount=len(STATE.cfg.installs))

    def _before_launch(install) -> None:
        # Multi-tenancy Phase 5: hand a terminal without a machine credential an EA enrollment token.
        prepare_terminal_enrollment(STATE.engine, STATE.cfg.daemon_id, install)

    STATE.sup = Supervisor(lambda: STATE.cfg, engine=STATE.engine, before_launch=_before_launch,
                           supervisor_log=lambda: supervisor_log_path(STATE.cfg))
    STATE.pm = STATE.sup.pm

    # Register with the engine.  Failure here is non-fatal — operator can fix
    # config + restart, and the heartbeat loop will retry on every tick.
    reg = await asyncio.to_thread(STATE.engine.register, _installs_payload(STATE.cfg))
    if reg is not None:
        _note_registered()
    else:
        logger.warning("initial register failed; will retry via heartbeat loop")
        write_status(STATE.cfg.state_dir, lastError=STATE.engine.last_error or
                     "The engine did not accept this computer's registration yet.")

    STATE.heartbeat_task = asyncio.create_task(_heartbeat_loop())
    STATE.jobs_task = asyncio.create_task(_jobs_loop())
    try:
        yield
    finally:
        for task in (STATE.heartbeat_task, STATE.jobs_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        STATE.sup.shutdown()
        STATE.engine.close()


app = FastAPI(
    title="Lascodia MT5 Supervisor",
    version=__version__,
    lifespan=lifespan,
)


# ── Auth dependency ────────────────────────────────────────────────────

def require_daemon_auth(x_daemon_auth: Optional[str] = Header(default=None)) -> None:
    """Engine→daemon calls must carry the X-Daemon-Auth header matching the
    daemon_api_key we shipped on register.  Returns 401 otherwise."""
    if x_daemon_auth != STATE.cfg.daemon_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid X-Daemon-Auth")


# ── Admin router (install management, daemon ops) ──────────────────────
#
# Mounted on the live FastAPI app so all the admin endpoints land under
# /admin/...  Constructed with a state binder that lets the router
# access STATE.cfg through the same lazy-getter pattern the runtime
# endpoints use, so config reloads after yaml mutations take effect
# without re-mounting the router.
_admin_state = AdminApiState(
    get_cfg=lambda: STATE.cfg,
    reload_config=lambda: STATE.reload_config(),
    config_path=os.environ.get("LASCODIA_SUPERVISOR_CONFIG", str(default_config_path())),
    get_pm=lambda: STATE.pm,
)
app.include_router(build_admin_router(_admin_state, require_daemon_auth))


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "daemonId": STATE.cfg.daemon_id, "sessions": len(STATE.pm.list_sessions())}


@app.get("/installs")
def installs(_: None = Depends(require_daemon_auth)) -> list[dict]:
    return _installs_payload(STATE.cfg)


@app.get("/terminals")
def list_terminals(_: None = Depends(require_daemon_auth)) -> list[dict]:
    STATE.pm.refresh()
    return [
        {
            "sessionId":  s.session_id,
            "installId":  s.install_id,
            "pid":        s.pid,
            "status":     s.status,
            "launchedAt": s.launched_at,
            "lastSeenAt": s.last_seen_at,
            "stoppedAt":  s.stopped_at,
            "reason":     s.reason,
        }
        for s in STATE.pm.list_sessions()
    ]


@app.post("/terminals/launch", response_model=LaunchResponse)
def launch_terminal(req: LaunchRequest, _: None = Depends(require_daemon_auth)) -> LaunchResponse:
    install = next((i for i in STATE.cfg.installs if i.install_id == req.installId), None)
    if install is None:
        raise HTTPException(status_code=404, detail=f"unknown installId '{req.installId}'")
    # The controller's pre-launch hook drops an EA enrollment token when the terminal needs one.
    try:
        sess = STATE.pm.launch(install, reason=req.reason)
        STATE.sup.watchdog.note_operator_launch(install.install_id)
    except RuntimeError as ex:
        raise HTTPException(status_code=500, detail=str(ex)) from ex
    return LaunchResponse(sessionId=sess.session_id, pid=sess.pid, status=sess.status)


@app.post("/terminals/{session_id}/close")
def close_terminal(session_id: str, _: None = Depends(require_daemon_auth)) -> dict:
    if not STATE.pm.close(session_id):
        raise HTTPException(status_code=404, detail="session not running or unknown")
    return {"ok": True}


# ── Entry point ────────────────────────────────────────────────────────

def run() -> None:
    """Console-script entry point installed by setuptools."""
    cfg = load_config()
    setup_logging(cfg)
    uvicorn.run(
        "lascodia_supervisor.main:app",
        host=cfg.bind_host,
        port=cfg.bind_port,
        log_level="info",
        # Leave logging to setup_logging: uvicorn's loggers propagate into the rotating
        # supervisor.log instead of growing launchd's stderr.log forever.
        log_config=None,
    )


if __name__ == "__main__":
    run()
