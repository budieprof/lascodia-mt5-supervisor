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

from .admin_api import AdminApiState, build_router as build_admin_router
from .config import SupervisorConfig, default_config_path, load_config
from .engine_client import EngineClient
from .process_manager import ProcessManager, TerminalSession

logger = logging.getLogger("lascodia.supervisor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)


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
    heartbeat_task: Optional[asyncio.Task] = None

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
    interval = STATE.cfg.heartbeat_interval_sec
    while True:
        try:
            STATE.pm.refresh()
            sessions_payload = [s.to_payload() for s in STATE.pm.list_sessions()]
            installs_payload = _installs_payload(STATE.cfg)
            STATE.engine.heartbeat(sessions_payload, installs_payload)
        except Exception:
            logger.exception("heartbeat loop iteration failed; will retry next tick")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE.cfg = load_config()
    STATE.pm = ProcessManager()
    STATE.engine = EngineClient(STATE.cfg)

    # Register with the engine.  Failure here is non-fatal — operator can fix
    # config + restart, and the heartbeat loop will retry on every tick.
    reg = STATE.engine.register(_installs_payload(STATE.cfg))
    if reg is not None:
        logger.info("registered with engine — daemon_id=%s", STATE.cfg.daemon_id)
    else:
        logger.warning("initial register failed; will retry via heartbeat loop")

    STATE.heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        if STATE.heartbeat_task is not None:
            STATE.heartbeat_task.cancel()
            try:
                await STATE.heartbeat_task
            except asyncio.CancelledError:
                pass
        STATE.engine.close()


app = FastAPI(
    title="Lascodia MT5 Supervisor",
    version="0.1.0",
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
    try:
        sess = STATE.pm.launch(install, reason=req.reason)
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
    uvicorn.run(
        "lascodia_supervisor.main:app",
        host=cfg.bind_host,
        port=cfg.bind_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
