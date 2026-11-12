import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router, ws_router
from app.config import get_settings
from app.engine.cst import get_model
from app.orchestrator import SessionManager
from app.store import Store
from app.wx.service import WxService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.manager = SessionManager(s, Store(s.data_dir))
    app.state.manager.start_loop()
    app.state.wx = WxService(s, app.state.manager)
    app.state.wx.start()
    lg = logging.getLogger("cabinshield")
    m = get_model()
    lg.info("env=%s model=%s self_check=%.4fs planner=%s wx=%s", s.env, m.version, m.self_check(), s.planner, s.wx_mode)
    if s.env == "prod" and not s.api_key:
        lg.warning("CS_API_KEY is empty in prod — write endpoints are unauthenticated!")
    yield
    await app.state.wx.shutdown()
    await app.state.manager.shutdown()


app = FastAPI(
    title="CabinShield Ground API", version=__version__, lifespan=lifespan,
    description="Ground side of the CabinShield crew app: schedule, briefing package, turbulence forecast uplink, "
                "tablet sync & AI decision audit, on-device model distribution, ops console (PoC).",
)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_list, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["ETag"])
app.include_router(router)
app.include_router(ws_router)


@app.get("/healthz", tags=["infra"])
def healthz():
    m = get_model()
    return {"ok": True, "version": __version__, "env": get_settings().env, "model": m.version,
            "flights": len(app.state.manager.sessions),
            "wx": {"mode": app.state.wx.status["mode"], "error": app.state.wx.status["last_error"]}}
