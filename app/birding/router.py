from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import current_principal
from app.birding.schemas import BlockDeliveryRequest, DisqualifyRequest, EventCreate, SignupRequest
from app.birding.service import BirdingService
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.security import Principal

router = APIRouter(prefix="/api/birding", tags=["公益观鸟导赏"])

PERM_WRITE = "birding.event.write"
PERM_ADMIN = "birding.event.admin"
PERM_SIGNUP = "birding.signup"


def service() -> BirdingService:
    return BirdingService()


def require_owner_or_admin(event_id: int, principal: Principal) -> None:
    """场次组织方隔离：只有创建者本人或平台管理员可以管理该场次。"""
    if principal.can(PERM_ADMIN):
        return
    event = service().connection.execute("SELECT created_by FROM birding_events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        raise NotFoundError("活动场次不存在")
    if event["created_by"] != principal.username:
        raise PermissionDeniedError("只能操作本团队创建的场次")


# -------------------------------------------------------------------- events


@router.post("/events", status_code=201)
def create_event(payload: EventCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(PERM_WRITE)
    return service().create_event(payload.model_dump(), actor=principal.username)


@router.get("/events")
def list_events(
    status: str | None = Query(default=None, pattern="^(draft|open|closed)$"),
    principal: Principal = Depends(current_principal),
) -> dict:
    del principal
    return {"items": service().list_events(status=status)}


@router.get("/events/{event_id}")
def get_event(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    del principal
    return service().get_event(event_id)


@router.post("/events/{event_id}/close")
def close_event(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    require_owner_or_admin(event_id, principal)
    return service().close_event(event_id, actor=principal.username)


# ----------------------------------------------------------------- signups


@router.post("/events/{event_id}/signups", status_code=201)
def signup(event_id: int, payload: SignupRequest, principal: Principal = Depends(current_principal)):
    principal.require(PERM_SIGNUP)
    registration, created = service().signup(event_id, payload.model_dump(), actor=principal.username)
    return JSONResponse(status_code=201 if created else 200, content=registration)


@router.post("/events/{event_id}/signups/{participant_id}/confirm")
def confirm_signup(event_id: int, participant_id: str, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(PERM_SIGNUP)
    return service().confirm(event_id, participant_id, actor=principal.username)


@router.post("/events/{event_id}/signups/{participant_id}/cancel")
def cancel_signup(event_id: int, participant_id: str, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(PERM_SIGNUP)
    return service().cancel(event_id, participant_id, actor=principal.username)


# -------------------------------------------------------------- eligibility


@router.post("/events/{event_id}/disqualifications")
def disqualify(event_id: int, payload: DisqualifyRequest, principal: Principal = Depends(current_principal)) -> dict:
    require_owner_or_admin(event_id, principal)
    return service().disqualify(event_id, payload.model_dump(), actor=principal.username)


@router.post("/events/{event_id}/signups/{participant_id}/requalify")
def requalify(event_id: int, participant_id: str, principal: Principal = Depends(current_principal)) -> dict:
    require_owner_or_admin(event_id, principal)
    return service().requalify(event_id, participant_id, actor=principal.username)


# ------------------------------------------------------------------ rosters


@router.get("/events/{event_id}/registrations")
def list_registrations(
    event_id: int,
    status: str | None = Query(default=None, pattern="^(offered|confirmed|waitlisted|expired|cancelled|ineligible)$"),
    principal: Principal = Depends(current_principal),
) -> dict:
    require_owner_or_admin(event_id, principal)
    return {"items": service().list_registrations(event_id, status=status)}


@router.get("/events/{event_id}/waitlist")
def waitlist(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    require_owner_or_admin(event_id, principal)
    return {"items": service().waitlist_queue(event_id)}


@router.get("/events/{event_id}/audit")
def event_audit(event_id: int, principal: Principal = Depends(current_principal)) -> dict:
    require_owner_or_admin(event_id, principal)
    return {"items": service().audit_trail(event_id=event_id)}


# ------------------------------------------------------- operational endpoints


@router.post("/expirations/run")
def run_expirations(
    event_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require(PERM_ADMIN)
    return service().expire_due_offers(event_id=event_id, actor=principal.username)


@router.post("/notifications/process")
def process_notifications(
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require(PERM_ADMIN)
    return service().process_notifications(limit=limit, actor=principal.username)


@router.get("/notifications")
def list_notifications(
    event_id: int | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(pending|sending|sent|failed|cancelled)$"),
    principal: Principal = Depends(current_principal),
) -> dict:
    if event_id is None:
        principal.require(PERM_ADMIN)
    else:
        require_owner_or_admin(event_id, principal)
    return {"items": service().list_notifications(event_id, status=status)}


@router.post("/notifications/delivery-blocks")
def block_delivery(payload: BlockDeliveryRequest, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(PERM_ADMIN)
    return service().block_delivery(payload.contact, payload.reason, actor=principal.username)


@router.delete("/notifications/delivery-blocks/{contact}")
def unblock_delivery(contact: str, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(PERM_ADMIN)
    return service().unblock_delivery(contact, actor=principal.username)
