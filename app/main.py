import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router, ws_router
from app.config import get_settings
from app.orchestrator import SessionManager
from app.planner import make_planner
from app.store import AuditStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    planner = make_planner(s)
    app.state.manager = SessionManager(s, planner, AuditStore(s.data_dir))
    lg = logging.getLogger("cabinshield")
    lg.info("planner=%s env=%s", planner.name, s.env)
    if s.env == "prod" and not s.api_key:
        lg.warning("CS_API_KEY is empty in prod — write endpoints are unauthenticated!")
    yield
    await app.state.manager.shutdown()


app = FastAPI(title="CabinShield-SM API", version=__version__, lifespan=lifespan,
              description="TTA·CST·CSM 계산 + 생성형 AI 후보 → Safety Kernel 검증 (PoC)")

app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_list,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(ws_router)


@app.get("/healthz")
def healthz():
    s = get_settings()
    return {"ok": True, "version": __version__, "planner": app.state.manager.planner.name, "env": s.env}
