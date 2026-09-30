from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=200)
    leader: str = Field(min_length=1, max_length=50)
    start_at: datetime
    capacity: int = Field(gt=0, le=10_000)
    waitlist_capacity: int = Field(default=0, ge=0, le=100_000)
    confirm_timeout_minutes: int = Field(default=30, gt=0, le=7 * 24 * 60)
    registration_opens_at: datetime
    registration_closes_at: datetime


class EventClose(BaseModel):
    reason: str = Field(default="", max_length=500)


class RegistrationCreate(BaseModel):
    # 同一身份由 applicant 唯一标识（如手机号/证件号），服务端做去空格与大小写归一。
    applicant: str = Field(min_length=1, max_length=64)
    applicant_name: str = Field(min_length=1, max_length=50)
    phone: str = Field(default="", max_length=40)
    # 客户端幂等键：同键同体重放返回同一结果；同键不同体返回 409。
    idempotency_key: str = Field(min_length=1, max_length=128)


class RegistrationCancel(BaseModel):
    reason: str = Field(default="", max_length=500)


class RegistrationBlock(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class RegistrationUnblock(BaseModel):
    reason: str = Field(default="", max_length=500)


class NotificationRetry(BaseModel):
    pass


class NotificationResult(BaseModel):
    """供发送方（志愿者/测试）回报外呼结果，驱动重试而不触碰占位状态。"""

    outcome: Literal["sent", "failed"]
    error: str = Field(default="", max_length=500)
