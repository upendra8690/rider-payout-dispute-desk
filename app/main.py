"""FastAPI application: wiring, startup (schema + seed + payment worker) and the ops dashboard."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.agents.agent import DisputeAgent
from app.agents.interpreter import Interpreter
from app.api.routes import ops_router, router
from app.config import PROJECT_ROOT, Settings, get_settings
from app.database import init_db, session_scope
from app.integrations.payswift import PaySwiftClient
from app.services.ops import OpsService
from app.services.payments import PaymentService, PaymentWorker
from app.services.seed import seed_reference_data
from app.utils.logging import configure_logging, get_logger

log = get_logger(__name__)
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


@dataclass
class Services:
    settings: Settings
    payswift: PaySwiftClient
    payments: PaymentService
    agent: DisputeAgent
    ops: OpsService
    worker: PaymentWorker | None = None
    ready: bool = False


def build_services(settings: Settings) -> Services:
    payswift = PaySwiftClient(settings.payswift_base_url)
    payments = PaymentService(payswift, settings)
    agent = DisputeAgent(settings, payswift, payments, Interpreter(settings))
    ops = OpsService(settings, payswift, payments)
    return Services(settings, payswift, payments, agent, ops)


def _startup(svc: Services, start_worker: bool) -> None:
    for attempt in range(60):  # Postgres may still be starting
        try:
            init_db()
            with session_scope() as s:
                seed_reference_data(s, svc.settings.data_dir)
            break
        except Exception as exc:
            log.warning("database not ready, retrying", extra={"attempt": attempt, "error": str(exc)[:200]})
            time.sleep(1)
    else:
        raise RuntimeError("database never became ready")
    if start_worker:
        svc.worker = PaymentWorker(svc.payments, svc.settings.worker_poll_seconds)
        svc.worker.start()
    svc.ready = True
    log.info("service ready", extra={"llm": svc.settings.llm_provider if svc.settings.llm_enabled else "none",
                                     "payswift": svc.settings.payswift_base_url})


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    svc = build_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        t = threading.Thread(target=_startup, args=(svc, start_worker), daemon=True)
        t.start()
        t.join()
        yield
        if svc.worker:
            svc.worker.stop()

    app = FastAPI(title="Rider Payout Dispute Desk", version=__version__, lifespan=lifespan)
    app.state.services = svc
    app.include_router(router)
    app.include_router(ops_router)
    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request):
        return templates.TemplateResponse(request, "index.html", {"version": __version__})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


app = create_app()
