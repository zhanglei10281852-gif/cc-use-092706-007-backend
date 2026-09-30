from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, description="场次编码，团队内唯一")
    title: str = Field(..., min_length=1, max_length=120)
    leader: str = Field(default="", max_length=60)
    location: str = Field(default="", max_length=120)
    start_at: str | None = Field(default=None, max_length=40)
    end_at: str | None = Field(default=None, max_length=40)
    capacity: int = Field(..., ge=1, le=10_000)
    waitlist_capacity: int | None = Field(default=None, ge=0, le=100_000, description="候补上限，留空表示不限")
    confirm_timeout_minutes: int = Field(..., ge=1, le=10_080)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("场次编码不能为空")
        return code


class SignupRequest(BaseModel):
    participant_id: str = Field(..., min_length=3, max_length=40, description="报名人身份标识（证件号/会员号）")
    participant_name: str = Field(..., min_length=1, max_length=50)
    contact: str = Field(..., min_length=3, max_length=40, description="通知送达目标（手机号等）")

    @field_validator("participant_id")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("participant_name", "contact")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class DisqualifyRequest(BaseModel):
    participant_id: str = Field(..., min_length=3, max_length=40)
    reason: str = Field(..., min_length=1, max_length=300)


class BlockDeliveryRequest(BaseModel):
    contact: str = Field(..., min_length=3, max_length=40)
    reason: str = Field(default="模拟通知渠道失败", max_length=200)


RegistrationStatus = Literal["offered", "confirmed", "waitlisted", "expired", "cancelled", "ineligible"]
EventStatus = Literal["draft", "open", "closed"]
NotificationStatus = Literal["pending", "sending", "sent", "failed", "cancelled"]
