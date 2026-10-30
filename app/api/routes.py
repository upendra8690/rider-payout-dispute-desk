"""HTTP routes. The interface in BRIEF.md / SUBMISSION.md is kept exactly; ops routes are extra."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import require_ops_token, services
from app.database import get_db
from app.schemas.messages import InboundMessage, OpsDecisionIn, ReplyOut
from app.services.ops import OpsError, trace_for
from app.utils.ids import normalize_rider_id

router = APIRouter()
ops_router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/health")
def health(db: Session = Depends(get_db), svc=Depends(services)):
    try:
        db.execute(text("SELECT 1"))
        seeded = db.execute(text("SELECT count(*) FROM trips")).scalar() > 0
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "starting", "db": f"error: {type(exc).__name__}"})
    if not seeded or not svc.ready:
        return JSONResponse(status_code=503, content={"status": "starting", "db": "ok", "seeded": seeded})
    return {
        "status": "ok", "db": "ok", "seeded": True,
        "payswift_reachable": svc.payswift.health(timeout=1.0),
        "llm": svc.settings.llm_provider if svc.settings.llm_enabled else "none (rule parser)",
        "payment_worker": svc.worker.is_alive() if svc.worker else False,
    }


@router.post("/messages", response_model=ReplyOut)
def post_message(msg: InboundMessage, svc=Depends(services)) -> ReplyOut:
    return ReplyOut(**svc.agent.handle(msg))


@router.get("/trace/{rider_id}")
def get_trace(rider_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rid = normalize_rider_id(rider_id)
    if not rid:
        raise HTTPException(status_code=422, detail="rider_id must look like R003")
    return [{k: v for k, v in step.items() if k in ("at", "type", "name", "input", "output", "message_id")}
            for step in trace_for(db, rid)]


@ops_router.get("/pending")
def ops_pending(db: Session = Depends(get_db), svc=Depends(services)) -> list[dict]:
    return svc.ops.pending(db)


def _ops_call(fn, *args):
    try:
        return fn(*args)
    except OpsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@ops_router.post("/items/{item_id}/approve", dependencies=[Depends(require_ops_token)])
def ops_approve(item_id: str, body: OpsDecisionIn, svc=Depends(services)):
    return _ops_call(svc.ops.approve, item_id, body.actor, body.note)


@ops_router.post("/items/{item_id}/reject", dependencies=[Depends(require_ops_token)])
def ops_reject(item_id: str, body: OpsDecisionIn, svc=Depends(services)):
    return _ops_call(svc.ops.reject, item_id, body.actor, body.note)


@ops_router.post("/items/{item_id}/resolve", dependencies=[Depends(require_ops_token)])
def ops_resolve(item_id: str, body: OpsDecisionIn, svc=Depends(services)):
    return _ops_call(svc.ops.resolve, item_id, body.actor, body.note)


@ops_router.get("/items", dependencies=[Depends(require_ops_token)])
def ops_items(status: str = Query("pending"), rider_id: str | None = None, db: Session = Depends(get_db),
              svc=Depends(services)):
    return svc.ops.items(db, status=status, rider_id=normalize_rider_id(rider_id) if rider_id else None)


@ops_router.get("/stats", dependencies=[Depends(require_ops_token)])
def ops_stats(db: Session = Depends(get_db), svc=Depends(services)):
    return svc.ops.stats(db)


@ops_router.get("/conversations", dependencies=[Depends(require_ops_token)])
def ops_conversations(db: Session = Depends(get_db), svc=Depends(services)):
    return svc.ops.conversations(db)


@ops_router.get("/riders/{rider_id}", dependencies=[Depends(require_ops_token)])
def ops_rider(rider_id: str, db: Session = Depends(get_db), svc=Depends(services)):
    rid = normalize_rider_id(rider_id)
    if not rid:
        raise HTTPException(status_code=422, detail="rider_id must look like R003")
    return svc.ops.rider_detail(db, rid)


@ops_router.get("/disputes", dependencies=[Depends(require_ops_token)])
def ops_disputes(status: str | None = None, db: Session = Depends(get_db), svc=Depends(services)):
    return svc.ops.disputes(db, status)


@ops_router.get("/payments", dependencies=[Depends(require_ops_token)])
def ops_payments(rider_id: str | None = None, db: Session = Depends(get_db), svc=Depends(services)):
    return svc.ops.payments_list(db, normalize_rider_id(rider_id) if rider_id else None)


@ops_router.get("/audit", dependencies=[Depends(require_ops_token)])
def ops_audit(rider_id: str | None = None, limit: int = Query(300, ge=1, le=2000), db: Session = Depends(get_db),
              svc=Depends(services)):
    return svc.ops.audit_log(db, normalize_rider_id(rider_id) if rider_id else None, limit)
