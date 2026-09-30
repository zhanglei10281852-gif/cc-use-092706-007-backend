from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from app.birding.schema import SCHEMA
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService
from app.services.idempotency import IdempotencyService

# 占名额状态：offered 为预留（待确认），confirmed 为已确认持有。
SEAT_STATUSES = ("offered", "confirmed")
# 候补状态：blocked 保留排队位置但暂不参与晋升。
WAITLIST_STATUSES = ("registered", "blocked")
ACTIVE_STATUSES = ("registered", "blocked", "offered", "confirmed")
TERMINAL_STATUSES = ("expired", "cancelled", "rejected")

# 通知重试节奏（秒），按已尝试次数退避。
RETRY_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
MAX_NOTIFICATION_ATTEMPTS = 5

NotificationSender = Callable[[dict[str, Any]], None]

_Unset = object()


def default_sender(notification: dict[str, Any]) -> None:
    """内置外呼通道：模拟即时送达。生产环境可替换为短信/公众号网关。"""
    return None


def normalize_applicant(value: str) -> str:
    return value.strip()


class BirdingService:
    """活动场次、报名、确认期限与候补队列的完整流转。"""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        clock: Clock | None = None,
        sender: NotificationSender | None = None,
    ) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.sender = sender or default_sender

    # ------------------------------------------------------------------ schema

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    # ------------------------------------------------------------------ events

    def create_event(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("birding.write")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        opens = payload["registration_opens_at"]
        closes = payload["registration_closes_at"]
        start = payload["start_at"]
        if closes <= opens:
            raise ValidationError("报名截止时间必须晚于开放时间")
        if start <= closes:
            raise ValidationError("活动开始时间必须晚于报名截止时间")
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO birding_events(title,location,leader,start_at,capacity,waitlist_capacity,"
                "confirm_timeout_minutes,registration_opens_at,registration_closes_at,status,"
                "created_by,created_by_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                (
                    payload["title"].strip(), payload["location"].strip(), payload["leader"].strip(),
                    to_storage(start), payload["capacity"], payload["waitlist_capacity"],
                    payload["confirm_timeout_minutes"], to_storage(opens), to_storage(closes),
                    principal.user_id, principal.display_name, now, now,
                ),
            )
            event_id = int(cursor.lastrowid)
            event = self._event(connection, event_id)
            self._audit(connection, AuditContext(principal.user_id, principal.display_name),
                        action="birding.event.create", resource_type="birding_event", resource_id=event_id,
                        after=event)
            return event

    def list_events(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status not in (None, "open", "closed"):
            raise ValidationError("活动状态只能是 open 或 closed")
        sql = "SELECT * FROM birding_events"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY start_at DESC, id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def event_summary(self, event_id: int) -> dict[str, Any]:
        event = self._event(self.connection, event_id)
        counts = self.connection.execute(
            "SELECT status,COUNT(*) AS amount FROM birding_registrations WHERE event_id=? GROUP BY status",
            (event_id,),
        ).fetchall()
        summary = dict(event)
        summary["status_counts"] = {row["status"]: row["amount"] for row in counts}
        summary["occupied"] = sum(summary["status_counts"].get(s, 0) for s in SEAT_STATUSES)
        summary["waitlisted"] = sum(summary["status_counts"].get(s, 0) for s in WAITLIST_STATUSES)
        return summary

    def roster(self, event_id: int, principal: Principal) -> dict[str, Any]:
        principal.require("birding.read")
        summary = self.event_summary(event_id)
        rows = self.connection.execute(
            "SELECT * FROM birding_registrations WHERE event_id=? ORDER BY queued_at ASC, id ASC",
            (event_id,),
        ).fetchall()
        summary["registrations"] = [dict(row) for row in rows]
        notifications = self.connection.execute(
            "SELECT n.* FROM birding_notifications n JOIN birding_registrations r ON r.id=n.registration_id "
            "WHERE r.event_id=? ORDER BY n.id",
            (event_id,),
        ).fetchall()
        summary["notifications"] = [dict(row) for row in notifications]
        return summary

    def close_event(self, event_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birding.write")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            event = self._event_row(connection, event_id)
            if event["status"] != "open":
                raise ConflictError("活动已经关闭")
            before = dict(event)
            connection.execute(
                "UPDATE birding_events SET status='closed',closed_at=?,close_reason=?,updated_at=? WHERE id=?",
                (now, reason.strip(), now, event_id),
            )
            # 关闭报名：清退所有未确认/候补报名；已确认参与者保留。
            outstanding = connection.execute(
                "SELECT * FROM birding_registrations WHERE event_id=? AND status IN ('registered','blocked','offered') ORDER BY id",
                (event_id,),
            ).fetchall()
            for reg in outstanding:
                self._history(connection, reg, "rejected", None, "活动关闭，清退未确认报名", principal.display_name, now)
                connection.execute(
                    "UPDATE birding_registrations SET status='rejected',seat_no=NULL,updated_at=? WHERE id=?",
                    (now, reg["id"]),
                )
                self._void_pending_notifications(connection, int(reg["id"]), "活动关闭")
                self._audit_registration(connection, principal, "birding.registration.reject_on_close",
                                         reg, "rejected", now, reason=reason, after_seat=None)
            after = self._event(connection, event_id)
            self._audit(connection, AuditContext(principal.user_id, principal.display_name),
                        action="birding.event.close", resource_type="birding_event", resource_id=event_id,
                        before=before, after=after,
                        metadata={"rejected": len(outstanding), "reason": reason})
            return self.event_summary(event_id)

    # ------------------------------------------------------------------ register

    def register(
        self,
        event_id: int,
        payload: dict[str, Any],
        principal: Principal | None,
    ) -> tuple[dict[str, Any], int, bool]:
        """返回 (报名单视图, HTTP 状态码, 是否为重放)。同一身份/同一幂等键均幂等。"""
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        applicant = normalize_applicant(payload["applicant"])
        actor, actor_id = self._actor(principal, applicant)
        # 幂等记录按“活动 + 身份”隔离：同一身份重复请求永远命中同一报名单。
        scope = f"birding.register:event:{event_id}:applicant:{applicant.casefold()}"
        with transaction(immediate=True) as connection:
            idempotency = IdempotencyService(connection, self.clock)
            stored = idempotency.lookup(scope, payload["idempotency_key"], payload)
            if stored is not None:
                # 重放返回报名单当前状态（确认/取消后再重放也能看到最新值），而非首次快照。
                row = connection.execute(
                    "SELECT * FROM birding_registrations WHERE id=?", (stored.body.get("id"),)
                ).fetchone()
                if row is not None:
                    body = self._registration_view(connection, row, duplicate=stored.body.get("duplicate", False))
                    return body, stored.status_code, True
                return stored.body, stored.status_code, True

            event = self._event_row(connection, event_id)
            if event["status"] != "open":
                raise ConflictError("活动已关闭，无法报名")
            if not (event["registration_opens_at"] <= now < event["registration_closes_at"]):
                raise ConflictError("当前不在报名开放时段内")

            existing = connection.execute(
                "SELECT * FROM birding_registrations WHERE event_id=? AND applicant=? COLLATE NOCASE",
                (event_id, applicant),
            ).fetchone()

            if existing is not None and existing["status"] in ACTIVE_STATUSES:
                # 同一身份重复请求：不产生新占位，返回当前报名单（200 + duplicate 标记）。
                body = self._registration_view(connection, existing, duplicate=True)
                idempotency.save(scope, payload["idempotency_key"], payload, body, 200)
                self._audit(connection, AuditContext(actor_id, actor),
                            action="birding.registration.duplicate", resource_type="birding_registration",
                            resource_id=existing["id"], metadata={"status": existing["status"]})
                return body, 200, False

            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO birding_registrations(event_id,applicant,applicant_name,phone,status,"
                    "queued_at,created_at,updated_at) VALUES(?,?,?,?,'registered',?,?,?)",
                    (event_id, applicant, payload["applicant_name"].strip(), payload.get("phone", "").strip(),
                     now, now, now),
                )
                reg_id = int(cursor.lastrowid)
                from_status = None
            else:
                # 终态后重新报名：复用同一行排到队尾，历史轨迹完整保留。
                self._reset_notifications(connection, int(existing["id"]))
                connection.execute(
                    "UPDATE birding_registrations SET status='registered',seat_no=NULL,offered_at=NULL,"
                    "offer_expires_at=NULL,confirmed_at=NULL,cancelled_at=NULL,queued_at=?,updated_at=? WHERE id=?",
                    (now, now, existing["id"]),
                )
                reg_id = int(existing["id"])
                from_status = existing["status"]
            reg = self._registration_row(connection, reg_id)
            self._history(connection, reg, "registered", None,
                          "新报名" if from_status is None else "终态后重新报名", actor, now,
                          from_status=from_status)

            self._admit_or_waitlist(connection, event, reg, principal, actor_id, now, now_dt)

            final = self._registration_row(connection, reg_id)
            body = self._registration_view(connection, final)
            idempotency.save(scope, payload["idempotency_key"], payload, body, 201)
            return body, 201, False

    def cancel(self, registration_id: int, reason: str, principal: Principal | None, applicant: str | None) -> dict[str, Any]:
        actor, actor_id = self._actor(principal, applicant)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            reg = self._registration_row(connection, registration_id)
            self._require_owner_or_staff(reg, principal, applicant, staff_permission="birding.write")
            if reg["status"] in TERMINAL_STATUSES:
                # 已是终态：取消幂等返回，不重复触发候补晋升。
                return self._registration_view(connection, reg)
            is_staff = principal is not None and principal.can("birding.write")
            if reg["status"] == "confirmed" and not is_staff:
                raise ConflictError("已确认的名额请联系志愿者团队退订")
            freed_seat = reg["seat_no"] if reg["status"] in SEAT_STATUSES else None
            self._history(connection, reg, "cancelled", None, reason or "报名人取消", actor, now)
            connection.execute(
                "UPDATE birding_registrations SET status='cancelled',seat_no=NULL,cancelled_at=?,updated_at=? WHERE id=?",
                (now, now, registration_id),
            )
            self._void_pending_notifications(connection, registration_id, "报名取消")
            self._audit_registration(connection, principal if principal and principal.user_id else None,
                                     "birding.registration.cancel", reg, "cancelled", now,
                                     actor=actor, actor_id=actor_id, reason=reason, after_seat=None)
            if freed_seat is not None:
                # 只晋升仍符合资格（registered）的最早候补；blocked 原样保留。
                self._promote_one(connection, reg["event_id"], freed_seat, actor, now, now_dt=self.clock.now())
            return self._registration_view(connection, self._registration_row(connection, registration_id))

    def confirm(self, registration_id: int, principal: Principal | None, applicant: str | None) -> dict[str, Any]:
        actor, actor_id = self._actor(principal, applicant)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            reg = self._registration_row(connection, registration_id)
            self._require_owner_or_staff(reg, principal, applicant, staff_permission="birding.write")
            if reg["status"] == "confirmed":
                return self._registration_view(connection, reg, duplicate=True)
            if reg["status"] != "offered":
                raise ConflictError("当前报名状态不允许确认")
            if reg["offer_expires_at"] and reg["offer_expires_at"] <= now:
                raise ConflictError("确认期限已过，名额将由系统回收")
            self._history(connection, reg, "confirmed", reg["seat_no"], "获得名额后确认", actor, now)
            connection.execute(
                "UPDATE birding_registrations SET status='confirmed',confirmed_at=?,updated_at=? WHERE id=?",
                (now, now, registration_id),
            )
            self._audit_registration(connection, principal if principal and principal.user_id else None,
                                     "birding.registration.confirm", reg, "confirmed", now,
                                     actor=actor, actor_id=actor_id)
            return self._registration_view(connection, self._registration_row(connection, registration_id))

    # --------------------------------------------------------- staff eligibility

    def block(self, registration_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birding.write")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            reg = self._registration_row(connection, registration_id)
            if reg["status"] != "registered":
                raise ConflictError("只有候补中（registered）的报名可以暂停资格")
            self._history(connection, reg, "blocked", None, reason, principal.display_name, now)
            connection.execute(
                "UPDATE birding_registrations SET status='blocked',updated_at=? WHERE id=?",
                (now, registration_id),
            )
            self._audit_registration(connection, principal, "birding.registration.block", reg, "blocked", now, reason=reason)
            return self._registration_view(connection, self._registration_row(connection, registration_id))

    def unblock(self, registration_id: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("birding.write")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            reg = self._registration_row(connection, registration_id)
            if reg["status"] != "blocked":
                raise ConflictError("只有暂停资格（blocked）的报名可以恢复")
            self._history(connection, reg, "registered", None, reason or "恢复资格", principal.display_name, now)
            connection.execute(
                "UPDATE birding_registrations SET status='registered',updated_at=? WHERE id=?",
                (now, registration_id),
            )
            self._audit_registration(connection, principal, "birding.registration.unblock", reg, "registered", now, reason=reason)
            # 恢复后若有名额空缺，按其原始排队位置参与晋升。
            self._fill_free_seats(connection, reg["event_id"], principal.display_name, now)
            return self._registration_view(connection, self._registration_row(connection, registration_id))

    # -------------------------------------------------------------------- sweep

    def sweep_expired(self, *, actor: str = "sweep-worker") -> dict[str, Any]:
        """回收所有过期未确认的名额，并立即按候补顺序晋升（只升 registered）。"""
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        expired: list[int] = []
        promoted: list[int] = []
        with transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT r.* FROM birding_registrations r JOIN birding_events e ON e.id=r.event_id "
                "WHERE r.status='offered' AND r.offer_expires_at IS NOT NULL AND r.offer_expires_at<=? "
                "AND e.status='open' ORDER BY r.event_id, r.offer_expires_at, r.id",
                (now,),
            ).fetchall()
            # 每个活动释放的座位号按升序，依次给排队最早的合格候补。
            freed_by_event: dict[int, list[int]] = {}
            for reg in rows:
                freed_by_event.setdefault(reg["event_id"], []).append(reg["seat_no"])
                self._history(connection, reg, "expired", reg["seat_no"], "确认超时，名额自动回收", actor, now)
                connection.execute(
                    "UPDATE birding_registrations SET status='expired',seat_no=NULL,updated_at=? WHERE id=?",
                    (now, reg["id"]),
                )
                self._void_pending_notifications(connection, int(reg["id"]), "确认超时")
                self._audit_registration(connection, None, "birding.registration.expire", reg, "expired", now,
                                         actor=actor, after_seat=None)
                expired.append(int(reg["id"]))
            for event_id, seats in freed_by_event.items():
                for seat in sorted(s for s in seats if s is not None):
                    promoted_id = self._promote_one(connection, event_id, seat, actor, now, now_dt=now_dt)
                    if promoted_id is not None:
                        promoted.append(promoted_id)
            self._audit(connection, AuditContext(None, actor),
                        action="birding.sweep.expired", resource_type="birding_event",
                        metadata={"expired": expired, "promoted": promoted})
        return {"expired": expired, "promoted": promoted}

    # -------------------------------------------------------------- notifications

    def dispatch_due(self, *, limit: int = 50) -> dict[str, Any]:
        """发送所有到期通知；失败只记录错误并退避重试，绝不改变报名/占位状态。"""
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        sent: list[int] = []
        failed: list[int] = []
        with transaction(immediate=True) as connection:
            pending = connection.execute(
                "SELECT * FROM birding_notifications WHERE status IN ('pending','failed') "
                "AND next_attempt_at<=? AND attempts<max_attempts ORDER BY id LIMIT ?",
                (now, max(1, min(limit, 500))),
            ).fetchall()
            for note in pending:
                payload = self._notification_payload(connection, note)
                try:
                    self.sender(payload)
                except Exception as exc:  # noqa: BLE001 - 外呼失败必须可重试
                    backoff = RETRY_BACKOFF_SECONDS[min(note["attempts"], len(RETRY_BACKOFF_SECONDS) - 1)]
                    connection.execute(
                        "UPDATE birding_notifications SET status='failed',attempts=attempts+1,"
                        "last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                        (str(exc)[:500], to_storage(now_dt + timedelta(seconds=backoff)), now, note["id"]),
                    )
                    failed.append(int(note["id"]))
                    self._audit(connection, AuditContext(None, "notification-worker"),
                                action="birding.notification.fail", resource_type="birding_notification",
                                resource_id=note["id"], outcome="failure",
                                metadata={"error": str(exc)[:500], "attempts": note["attempts"] + 1})
                else:
                    connection.execute(
                        "UPDATE birding_notifications SET status='sent',attempts=attempts+1,"
                        "sent_at=?,last_error='',updated_at=? WHERE id=?",
                        (now, now, note["id"]),
                    )
                    sent.append(int(note["id"]))
                    self._audit(connection, AuditContext(None, "notification-worker"),
                                action="birding.notification.sent", resource_type="birding_notification",
                                resource_id=note["id"])
        return {"sent": sent, "failed": failed}

    def report_delivery(self, notification_id: int, outcome: str, error: str) -> dict[str, Any]:
        """供外呼网关回报结果（模拟通道），只影响通知自身，与名额无关。"""
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        with transaction(immediate=True) as connection:
            note = self._notification_row(connection, notification_id)
            if outcome == "sent":
                connection.execute(
                    "UPDATE birding_notifications SET status='sent',sent_at=?,last_error='',updated_at=? WHERE id=?",
                    (now, now, notification_id),
                )
            else:
                backoff = RETRY_BACKOFF_SECONDS[min(note["attempts"], len(RETRY_BACKOFF_SECONDS) - 1)]
                connection.execute(
                    "UPDATE birding_notifications SET status='failed',attempts=attempts+1,"
                    "last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                    (error[:500], to_storage(now_dt + timedelta(seconds=backoff)), now, notification_id),
                )
            return dict(self._notification_row(connection, notification_id))

    def retry_notification(self, notification_id: int, principal: Principal | None) -> dict[str, Any]:
        """人工重试：清零退避立即到期；不新建通知、不重复占位。"""
        if principal is not None:
            principal.require("birding.write")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            note = self._notification_row(connection, notification_id)
            if note["channel"] == "offer":
                reg = self._registration_row(connection, note["registration_id"])
                if reg["status"] not in ("offered", "confirmed"):
                    raise ConflictError("名额已释放，转正通知不可重试")
            connection.execute(
                "UPDATE birding_notifications SET status='pending',attempts=0,last_error='',"
                "next_attempt_at=?,updated_at=? WHERE id=?",
                (now, now, notification_id),
            )
            actor = principal.display_name if principal else "notification-worker"
            self._audit(connection, AuditContext(principal.user_id if principal else None, actor),
                        action="birding.notification.retry", resource_type="birding_notification",
                        resource_id=notification_id, metadata={"previous_status": note["status"]})
            return dict(self._notification_row(connection, notification_id))

    # ------------------------------------------------------------------ queries

    def registration(self, registration_id: int, principal: Principal | None, applicant: str | None) -> dict[str, Any]:
        row = self._registration_row(self.connection, registration_id)
        self._require_owner_or_staff(row, principal, applicant)
        return self._registration_view(self.connection, row)

    def history(self, registration_id: int, principal: Principal | None, applicant: str | None) -> list[dict[str, Any]]:
        row = self._registration_row(self.connection, registration_id)
        self._require_owner_or_staff(row, principal, applicant)
        rows = self.connection.execute(
            "SELECT * FROM birding_status_history WHERE registration_id=? ORDER BY id ASC",
            (registration_id,),
        ).fetchall()
        return [dict(item) for item in rows]

    # ------------------------------------------------------------------ helpers

    def _admit_or_waitlist(
        self, connection: sqlite3.Connection, event: sqlite3.Row, reg: sqlite3.Row,
        principal: Principal | None, actor_id: int | None, now: str, now_dt,
    ) -> None:
        occupied = self._occupied(connection, event["id"])
        if occupied < event["capacity"]:
            seat = self._next_free_seat(connection, event["id"], event["capacity"])
            expires = to_storage(now_dt + timedelta(minutes=event["confirm_timeout_minutes"]))
            connection.execute(
                "UPDATE birding_registrations SET status='offered',seat_no=?,offered_at=?,"
                "offer_expires_at=?,updated_at=? WHERE id=?",
                (seat, now, expires, now, reg["id"]),
            )
            updated = self._registration_row(connection, reg["id"])
            self._history(connection, updated, "offered", seat, "获得名额，等待确认",
                          principal.display_name if principal else f"applicant:{reg['applicant']}", now,
                          from_status="registered")
            self._audit_registration(connection, principal, "birding.registration.offer", reg, "offered", now,
                                     actor_id=actor_id, after_seat=seat)
            self._create_offer_notification(connection, updated, event, now)
            return
        waitlisted = self._waitlisted(connection, event["id"])
        if event["waitlist_capacity"] == 0:
            raise ConflictError("活动名额已满且未开放候补")
        if waitlisted > event["waitlist_capacity"]:
            raise ConflictError("活动名额与候补队列均已满")
        self._audit_registration(connection, principal, "birding.registration.waitlist", reg, "registered", now,
                                 actor_id=actor_id)

    def _promote_one(
        self, connection: sqlite3.Connection, event_id: int, seat_no: int, actor: str, now: str, *, now_dt,
    ) -> int | None:
        """把最早仍合格（registered）的候补转正；blocked 不晋升、位置保留。"""
        candidate = connection.execute(
            "SELECT * FROM birding_registrations WHERE event_id=? AND status='registered' ORDER BY queued_at ASC,id ASC LIMIT 1",
            (event_id,),
        ).fetchone()
        if candidate is None:
            return None
        event = self._event_row(connection, event_id)
        expires = to_storage(now_dt + timedelta(minutes=event["confirm_timeout_minutes"]))
        connection.execute(
            "UPDATE birding_registrations SET status='offered',seat_no=?,offered_at=?,"
            "offer_expires_at=?,updated_at=? WHERE id=?",
            (seat_no, now, expires, now, candidate["id"]),
        )
        updated = self._registration_row(connection, candidate["id"])
        self._history(connection, updated, "offered", seat_no, "候补转正，等待确认", actor, now,
                      from_status="registered")
        self._audit_registration(connection, None, "birding.registration.promote", candidate, "offered", now,
                                 actor=actor, after_seat=seat_no)
        self._create_offer_notification(connection, updated, event, now)
        return int(candidate["id"])

    def _fill_free_seats(self, connection: sqlite3.Connection, event_id: int, actor: str, now: str) -> None:
        event = self._event_row(connection, event_id)
        now_dt = self.clock.now()
        while self._occupied(connection, event_id) < event["capacity"]:
            seat = self._next_free_seat(connection, event_id, event["capacity"])
            if self._promote_one(connection, event_id, seat, actor, now, now_dt=now_dt) is None:
                break

    @staticmethod
    def _occupied(connection: sqlite3.Connection, event_id: int) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) FROM birding_registrations WHERE event_id=? AND status IN ('offered','confirmed')",
            (event_id,),
        ).fetchone()[0])

    @staticmethod
    def _waitlisted(connection: sqlite3.Connection, event_id: int) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) FROM birding_registrations WHERE event_id=? AND status IN ('registered','blocked')",
            (event_id,),
        ).fetchone()[0])

    @staticmethod
    def _next_free_seat(connection: sqlite3.Connection, event_id: int, capacity: int) -> int:
        used = {row[0] for row in connection.execute(
            "SELECT seat_no FROM birding_registrations WHERE event_id=? AND seat_no IS NOT NULL", (event_id,)
        ).fetchall()}
        for number in range(1, capacity + 1):
            if number not in used:
                return number
        raise ConflictError("活动名额已满")

    def _create_offer_notification(self, connection: sqlite3.Connection, reg: sqlite3.Row, event: sqlite3.Row, now: str) -> None:
        # 重新报名时旧通知已被清除，故每个报名单的 offer 通道在生命周期内唯一。
        connection.execute(
            "INSERT INTO birding_notifications(event_id,registration_id,channel,status,attempts,max_attempts,"
            "next_attempt_at,idempotency_key,created_at,updated_at) VALUES(?,?,'offer','pending',0,5,?,?,?,?)",
            (event["id"], reg["id"], now, f"offer:event{event['id']}:reg{reg['id']}", now, now),
        )

    @staticmethod
    def _void_pending_notifications(connection: sqlite3.Connection, registration_id: int, reason: str) -> None:
        # 名额已释放：未发送的 offer 通知不再重试，避免向无名额者发送。
        connection.execute(
            "UPDATE birding_notifications SET status='failed',attempts=max_attempts,"
            "last_error=?,updated_at=datetime('now') WHERE registration_id=? AND status IN ('pending','failed')",
            (f"通知作废：{reason}", registration_id),
        )

    @staticmethod
    def _reset_notifications(connection: sqlite3.Connection, registration_id: int) -> None:
        connection.execute("DELETE FROM birding_notifications WHERE registration_id=?", (registration_id,))

    @staticmethod
    def _history(
        connection: sqlite3.Connection, reg: sqlite3.Row, to_status: str, seat_no: int | None,
        reason: str, actor: str, now: str, *, from_status: str | None | object = _Unset,
    ) -> None:
        previous = reg["status"] if from_status is _Unset else from_status
        connection.execute(
            "INSERT INTO birding_status_history(event_id,registration_id,from_status,to_status,seat_no,"
            "reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (reg["event_id"], reg["id"], previous, to_status, seat_no, reason, actor, now),
        )

    def _audit(self, connection: sqlite3.Connection, context: AuditContext, **kwargs: Any) -> None:
        AuditService(connection, self.clock).record(context, **kwargs)

    def _audit_registration(
        self, connection: sqlite3.Connection, principal: Principal | None, action: str,
        before_row: sqlite3.Row, to_status: str, now: str, *,
        reason: str = "", after_seat: int | None | object = _Unset,
        actor: str | None = None, actor_id: int | None = None,
    ) -> None:
        if principal is not None:
            context = AuditContext(principal.user_id, principal.display_name)
        else:
            context = AuditContext(actor_id, actor or "system")
        seat_after = before_row["seat_no"] if after_seat is _Unset else after_seat
        self._audit(
            connection, context,
            action=action, resource_type="birding_registration", resource_id=before_row["id"],
            before={"status": before_row["status"], "seat_no": before_row["seat_no"],
                    "offer_expires_at": before_row["offer_expires_at"]},
            after={"status": to_status, "seat_no": seat_after},
            metadata={"event_id": before_row["event_id"], "applicant": before_row["applicant"], "reason": reason},
        )

    def _registration_view(self, connection: sqlite3.Connection, reg: sqlite3.Row, *, duplicate: bool = False) -> dict[str, Any]:
        view = dict(reg)
        view["duplicate"] = duplicate
        note = connection.execute(
            "SELECT id,status,attempts,last_error,next_attempt_at FROM birding_notifications "
            "WHERE registration_id=? AND channel='offer'",
            (reg["id"],),
        ).fetchone()
        view["offer_notification"] = dict(note) if note else None
        return view

    def _notification_payload(self, connection: sqlite3.Connection, note: sqlite3.Row) -> dict[str, Any]:
        reg = self._registration_row(connection, note["registration_id"])
        event = self._event_row(connection, note["event_id"])
        return {
            "notification_id": note["id"], "channel": note["channel"],
            "event": {"id": event["id"], "title": event["title"], "start_at": event["start_at"], "location": event["location"]},
            "applicant": reg["applicant"], "applicant_name": reg["applicant_name"], "phone": reg["phone"],
            "offer_expires_at": reg["offer_expires_at"],
        }

    @staticmethod
    def _event_row(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        event = connection.execute("SELECT * FROM birding_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("观鸟活动场次不存在")
        return event

    def _event(self, connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
        return dict(self._event_row(connection, event_id))

    @staticmethod
    def _registration_row(connection: sqlite3.Connection, registration_id: int) -> sqlite3.Row:
        reg = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (registration_id,)).fetchone()
        if reg is None:
            raise NotFoundError("报名记录不存在")
        return reg

    @staticmethod
    def _notification_row(connection: sqlite3.Connection, notification_id: int) -> sqlite3.Row:
        note = connection.execute("SELECT * FROM birding_notifications WHERE id=?", (notification_id,)).fetchone()
        if note is None:
            raise NotFoundError("通知不存在")
        return note

    @staticmethod
    def _actor(principal: Principal | None, applicant: str | None) -> tuple[str, int | None]:
        if principal is not None:
            return principal.display_name, principal.user_id
        if applicant:
            return f"applicant:{normalize_applicant(applicant)}", None
        return "anonymous", None

    @staticmethod
    def _require_owner_or_staff(
        reg: sqlite3.Row, principal: Principal | None, applicant: str | None,
        *, staff_permission: str = "birding.read",
    ) -> None:
        if principal is not None and principal.can(staff_permission):
            return
        if applicant and normalize_applicant(applicant).casefold() == reg["applicant"].casefold():
            return
        raise PermissionDeniedError("只能操作本人的报名记录")
