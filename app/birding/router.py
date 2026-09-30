from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import current_principal
from app.birding.schemas import (
    EventClose,
    EventCreate,
    NotificationResult,
    RegistrationBlock,
    RegistrationCancel,
    RegistrationCreate,
    RegistrationUnblock,
)
from app.birding.service import BirdingService
from app.core.errors import AuthenticationError
from app.core.security import Principal

router = APIRouter(prefix="/api/birding", tags=["飞羽观鸟导赏"])

# 测试/运维钩子：注入可控时钟与故障外呼通道（生产环境保持 None）。
clock_override = None
sender_override = None


def service() -> BirdingService:
    return BirdingService(clock=clock_override, sender=sender_override)


def optional_principal(authorization: str | None = Header(default=None)) -> Principal | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return current_principal(authorization)
    except AuthenticationError:
        return None


# ----------------------------------------------------------------------- events

@router.post("/events", status_code=201)
def create_event(payload: EventCreate, principal: Principal = Depends(current_principal)):
    return service().create_event(payload.model_dump(), principal)


@router.get("/events")
def list_events(status: str | None = Query(default=None)):
    return {"items": service().list_events(status=status)}


@router.get("/events/{event_id}")
def event_summary(event_id: int):
    return service().event_summary(event_id)


@router.get("/events/{event_id}/roster")
def event_roster(event_id: int, principal: Principal = Depends(current_principal)):
    return service().roster(event_id, principal)


@router.post("/events/{event_id}/close")
def close_event(event_id: int, payload: EventClose, principal: Principal = Depends(current_principal)):
    return service().close_event(event_id, payload.reason, principal)


# ---------------------------------------------------------------- registrations

@router.post("/events/{event_id}/registrations")
def register(
    event_id: int,
    payload: RegistrationCreate,
    principal: Principal | None = Depends(optional_principal),
):
    body, status_code, _replayed = service().register(event_id, payload.model_dump(), principal)
    return JSONResponse(body, status_code=status_code)


@router.get("/registrations/{registration_id}")
def get_registration(
    registration_id: int,
    applicant: str | None = Query(default=None, max_length=64),
    principal: Principal | None = Depends(optional_principal),
):
    return service().registration(registration_id, principal, applicant)


@router.get("/registrations/{registration_id}/history")
def registration_history(
    registration_id: int,
    applicant: str | None = Query(default=None, max_length=64),
    principal: Principal | None = Depends(optional_principal),
):
    return {"items": service().history(registration_id, principal, applicant)}


@router.post("/registrations/{registration_id}/confirm")
def confirm_registration(
    registration_id: int,
    applicant: str | None = Query(default=None, max_length=64),
    principal: Principal | None = Depends(optional_principal),
):
    return service().confirm(registration_id, principal, applicant)


@router.post("/registrations/{registration_id}/cancel")
def cancel_registration(
    registration_id: int,
    payload: RegistrationCancel,
    applicant: str | None = Query(default=None, max_length=64),
    principal: Principal | None = Depends(optional_principal),
):
    return service().cancel(registration_id, payload.reason, principal, applicant)


@router.post("/registrations/{registration_id}/block")
def block_registration(registration_id: int, payload: RegistrationBlock, principal: Principal = Depends(current_principal)):
    return service().block(registration_id, payload.reason, principal)


@router.post("/registrations/{registration_id}/unblock")
def unblock_registration(registration_id: int, payload: RegistrationUnblock, principal: Principal = Depends(current_principal)):
    return service().unblock(registration_id, payload.reason, principal)


# --------------------------------------------------------------- worker / staff

@router.post("/sweep/expired")
def sweep_expired(principal: Principal = Depends(current_principal)):
    principal.require("birding.write")
    return service().sweep_expired()


@router.post("/notifications/dispatch")
def dispatch_due(principal: Principal = Depends(current_principal), limit: int = Query(default=50, ge=1, le=500)):
    principal.require("birding.write")
    return service().dispatch_due(limit=limit)


@router.post("/notifications/{notification_id}/delivery")
def report_delivery(notification_id: int, payload: NotificationResult, principal: Principal = Depends(current_principal)):
    principal.require("birding.write")
    return service().report_delivery(notification_id, payload.outcome, payload.error)


@router.post("/notifications/{notification_id}/retry")
def retry_notification(notification_id: int, principal: Principal = Depends(current_principal)):
    return service().retry_notification(notification_id, principal)
