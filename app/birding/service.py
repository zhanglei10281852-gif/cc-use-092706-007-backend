from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.birding.runtime import get_clock
from app.core.clock import Clock, to_storage
from app.core.errors import ConflictError, NotFoundError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS birding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    leader TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    start_at TEXT,
    end_at TEXT,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    waitlist_capacity INTEGER,
    confirm_timeout_minutes INTEGER NOT NULL CHECK(confirm_timeout_minutes > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('draft','open','closed')),
    created_by TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS birding_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES birding_events(id) ON DELETE RESTRICT,
    participant_id TEXT NOT NULL,
    participant_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('offered','confirmed','waitlisted','expired','cancelled','ineligible')),
    queue_seq INTEGER,
    offer_expires_at TEXT,
    offered_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    ineligible_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, participant_id)
);
CREATE INDEX IF NOT EXISTS idx_birding_reg_event_status ON birding_registrations(event_id, status, queue_seq);
CREATE TABLE IF NOT EXISTS birding_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES birding_events(id) ON DELETE RESTRICT,
    registration_id INTEGER NOT NULL REFERENCES birding_registrations(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    available_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_birding_notif_pending ON birding_notifications(status, available_at, id);
CREATE TABLE IF NOT EXISTS birding_delivery_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS birding_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    registration_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_birding_audit_event ON birding_audit(event_id, id);
CREATE INDEX IF NOT EXISTS idx_birding_audit_reg ON birding_audit(registration_id, id);
"""

ACTIVE_STATUSES = ("offered", "confirmed", "waitlisted")
SEAT_STATUSES = ("offered", "confirmed")
OFFER_NOTIFICATION_KINDS = ("offer", "promotion")
PENDING_NOTIFICATION_STATUSES = ("pending", "failed")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


class BirdingService:
    """公益观鸟导赏的场次、报名、确认期限与候补队列流转服务。

    关键不变量：

    * 所有写操作都在单个 ``BEGIN IMMEDIATE`` 事务中完成，配合
      ``birding_registrations(event_id, participant_id)`` 唯一约束，并发下
      同一身份的重复报名天然幂等，不会重复占位或重复入队。
    * 名额按「已确认 + 待确认」计数，待确认先占位，逾期或取消即时释放并
      严格按 ``queue_seq`` 提升候补；只有状态仍为 ``waitlisted``（即仍符合
      资格）的候补者会被提升。
    * 占位/提升与通知发送解耦：通知走发件箱，失败按退避重试，重试只更新
      同一条 notification，不会产生第二条占位记录。
    * 每一次状态变化都向 ``birding_audit`` 追加 before/after，可据此还原
      任意场次与报名的完整历史。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or get_clock()
        ensure_schema()

    # ------------------------------------------------------------------ helpers

    def _now(self) -> str:
        return to_storage(self.clock.now())

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        actor: str,
        event_id: int | None = None,
        registration_id: int | None = None,
        before: dict | None = None,
        after: dict | None = None,
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO birding_audit(event_id,registration_id,action,actor,before_json,after_json,metadata_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                event_id,
                registration_id,
                action,
                actor,
                json.dumps(before or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(after or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                created_at or self._now(),
            ),
        )

    def _enqueue_notification(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: int,
        registration: sqlite3.Row,
        kind: str,
        now: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO birding_notifications(event_id,registration_id,participant_id,channel,target,kind,status,available_at,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?, 'pending', ?, ?, ?)",
            (
                event_id,
                registration["id"],
                registration["participant_id"],
                "sms",
                registration["contact"],
                kind,
                now,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _cancel_pending_offer_notifications(self, connection: sqlite3.Connection, *, registration_id: int, now: str) -> None:
        placeholders = ",".join("?" for _ in OFFER_NOTIFICATION_KINDS)
        status_placeholders = ",".join("?" for _ in PENDING_NOTIFICATION_STATUSES)
        connection.execute(
            f"UPDATE birding_notifications SET status='cancelled', updated_at=? WHERE registration_id=? "
            f"AND kind IN ({placeholders}) AND status IN ({status_placeholders})",
            (now, registration_id, *OFFER_NOTIFICATION_KINDS, *PENDING_NOTIFICATION_STATUSES),
        )

    def _get_event(self, connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        event = connection.execute("SELECT * FROM birding_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("活动场次不存在")
        return event

    def _occupied(self, connection: sqlite3.Connection, event_id: int) -> int:
        """已占用名额数 = 已确认 + 待确认（待确认先占位，逾期/取消再释放）。"""
        placeholders = ",".join("?" for _ in SEAT_STATUSES)
        row = connection.execute(
            f"SELECT COUNT(*) FROM birding_registrations WHERE event_id=? AND status IN ({placeholders})",
            (event_id, *SEAT_STATUSES),
        ).fetchone()
        return int(row[0])

    def _next_queue_seq(self, connection: sqlite3.Connection, event_id: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM birding_registrations WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _public_registration(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data.pop("ineligible_reason", None)
        return data

    # ------------------------------------------------------------------ events

    def create_event(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self._now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT id FROM birding_events WHERE code=?", (payload["code"],)).fetchone()
            if existing is not None:
                raise ConflictError("场次编码已存在")
            cursor = connection.execute(
                "INSERT INTO birding_events(code,title,leader,location,start_at,end_at,capacity,waitlist_capacity,"
                "confirm_timeout_minutes,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)",
                (
                    payload["code"], payload["title"], payload["leader"], payload["location"],
                    payload["start_at"], payload["end_at"], payload["capacity"], payload["waitlist_capacity"],
                    payload["confirm_timeout_minutes"], actor, now, now,
                ),
            )
            event_id = int(cursor.lastrowid)
            event = self._get_event(connection, event_id)
            self._audit(connection, action="event.create", actor=actor, event_id=event_id, after=dict(event))
            return dict(event)

    def get_event(self, event_id: int) -> dict[str, Any]:
        event = self.connection.execute("SELECT * FROM birding_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("活动场次不存在")
        result = dict(event)
        result["occupied"] = self._occupied(self.connection, event_id)
        result["available"] = int(event["capacity"]) - result["occupied"]
        return result

    def list_events(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self.connection.execute(
                "SELECT * FROM birding_events WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM birding_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["occupied"] = self._occupied(self.connection, row["id"])
            item["available"] = int(row["capacity"]) - item["occupied"]
            events.append(item)
        return events

    def close_event(self, event_id: int, actor: str) -> dict[str, Any]:
        """关闭活动：冻结报名。不再接受新报名或候补转正；已发出的确认期限保持不变。"""
        now = self._now()
        with transaction(immediate=True) as connection:
            event = self._get_event(connection, event_id)
            before = dict(event)
            if event["status"] == "closed":
                raise ConflictError("活动场次已关闭")
            connection.execute(
                "UPDATE birding_events SET status='closed', closed_at=?, updated_at=? WHERE id=?",
                (now, now, event_id),
            )
            after = dict(self._get_event(connection, event_id))
            self._audit(
                connection, action="event.close", actor=actor, event_id=event_id, before=before, after=after,
            )
            return after

    # ------------------------------------------------------------------ signup

    def signup(self, event_id: int, payload: dict[str, Any], actor: str) -> tuple[dict[str, Any], bool]:
        """报名或重新报名。返回 (报名记录, 是否新建/复活)。

        同一身份对仍在进行中的报名重复请求：直接回放原记录（幂等）；
        对已取消/已逾期的记录：视为一次新的报名，在原行上复活并重新审计。
        """
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            event = self._get_event(connection, event_id)
            if event["status"] != "open":
                raise ConflictError("活动场次未开放报名")
            existing = connection.execute(
                "SELECT * FROM birding_registrations WHERE event_id=? AND participant_id=?",
                (event_id, payload["participant_id"]),
            ).fetchone()
            if existing is not None and existing["status"] in ACTIVE_STATUSES:
                return self._public_registration(existing), False
            if existing is not None and existing["status"] == "ineligible":
                raise ConflictError("该身份当前不符合报名资格")

            occupied = self._occupied(connection, event_id)
            if occupied < int(event["capacity"]):
                expires = to_storage(now_value + timedelta(minutes=int(event["confirm_timeout_minutes"])))
                if existing is None:
                    cursor = connection.execute(
                        "INSERT INTO birding_registrations(event_id,participant_id,participant_name,contact,status,"
                        "offer_expires_at,offered_at,created_by,created_at,updated_at)"
                        " VALUES(?,?,?,?, 'offered', ?, ?, ?, ?, ?)",
                        (
                            event_id, payload["participant_id"], payload["participant_name"], payload["contact"],
                            expires, now, actor, now, now,
                        ),
                    )
                    reg_id = int(cursor.lastrowid)
                else:
                    # 复活已取消/已逾期的记录，得到新的占位名额与确认期限。
                    reg_id = int(existing["id"])
                    connection.execute(
                        "UPDATE birding_registrations SET participant_name=?,contact=?,status='offered',queue_seq=NULL,"
                        "offer_expires_at=?,offered_at=?,confirmed_at=NULL,cancelled_at=NULL,ineligible_reason='',"
                        "created_by=?,updated_at=? WHERE id=?",
                        (
                            payload["participant_name"], payload["contact"], expires, now, actor, now, reg_id,
                        ),
                    )
                reg = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg_id,)).fetchone()
                self._enqueue_notification(connection, event_id=event_id, registration=reg, kind="offer", now=now)
                self._audit(
                    connection, action="signup.offer", actor=actor, event_id=event_id,
                    registration_id=reg_id,
                    before=self._public_registration(existing) if existing is not None else None,
                    after=self._public_registration(reg),
                    metadata={"revived": existing is not None},
                )
                return self._public_registration(reg), True

            # 名额已满 -> 候补队列（FIFO）。
            waiting_row = connection.execute(
                "SELECT COUNT(*) FROM birding_registrations WHERE event_id=? AND status='waitlisted'",
                (event_id,),
            ).fetchone()
            if event["waitlist_capacity"] is not None and int(waiting_row[0]) >= int(event["waitlist_capacity"]):
                raise ConflictError("名额与候补队列均已满")
            queue_seq = self._next_queue_seq(connection, event_id)
            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO birding_registrations(event_id,participant_id,participant_name,contact,status,"
                    "queue_seq,created_by,created_at,updated_at) VALUES(?,?,?,?, 'waitlisted', ?, ?, ?, ?)",
                    (
                        event_id, payload["participant_id"], payload["participant_name"], payload["contact"],
                        queue_seq, actor, now, now,
                    ),
                )
                reg_id = int(cursor.lastrowid)
            else:
                reg_id = int(existing["id"])
                connection.execute(
                    "UPDATE birding_registrations SET participant_name=?,contact=?,status='waitlisted',queue_seq=?,"
                    "offer_expires_at=NULL,offered_at=NULL,confirmed_at=NULL,cancelled_at=NULL,ineligible_reason='',"
                    "created_by=?,updated_at=? WHERE id=?",
                    (payload["participant_name"], payload["contact"], queue_seq, actor, now, reg_id),
                )
            reg = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg_id,)).fetchone()
            self._audit(
                connection, action="signup.waitlist", actor=actor, event_id=event_id,
                registration_id=reg_id,
                before=self._public_registration(existing) if existing is not None else None,
                after=self._public_registration(reg),
                metadata={"revived": existing is not None, "queue_seq": queue_seq},
            )
            return self._public_registration(reg), True

    def confirm(self, event_id: int, participant_id: str, actor: str) -> dict[str, Any]:
        now = self._now()
        with transaction(immediate=True) as connection:
            reg = self._require_registration(connection, event_id, participant_id)
            before = dict(reg)
            if reg["status"] == "confirmed":
                return self._public_registration(reg)  # 重复确认幂等
            if reg["status"] != "offered":
                raise ConflictError(f"当前状态 {reg['status']} 不能确认")
            if reg["offer_expires_at"] is not None and reg["offer_expires_at"] < now:
                raise ConflictError("确认期限已过，请等待名额回收后重新报名")
            connection.execute(
                "UPDATE birding_registrations SET status='confirmed', confirmed_at=?, updated_at=? WHERE id=?",
                (now, now, reg["id"]),
            )
            after = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
            self._audit(
                connection, action="registration.confirm", actor=actor, event_id=event_id,
                registration_id=reg["id"], before=self._public_registration(before),
                after=self._public_registration(after),
            )
            return self._public_registration(after)

    def cancel(self, event_id: int, participant_id: str, actor: str) -> dict[str, Any]:
        """报名人取消：释放名额，并按候补顺序只提升仍符合资格的人。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            event = self._get_event(connection, event_id)
            reg = self._require_registration(connection, event_id, participant_id)
            before = dict(reg)
            if reg["status"] == "cancelled":
                return self._public_registration(reg)  # 重复取消幂等
            if reg["status"] not in ACTIVE_STATUSES:
                raise ConflictError(f"当前状态 {reg['status']} 不能取消")
            connection.execute(
                "UPDATE birding_registrations SET status='cancelled', cancelled_at=COALESCE(cancelled_at,?), updated_at=? WHERE id=?",
                (now, now, reg["id"]),
            )
            self._cancel_pending_offer_notifications(connection, registration_id=reg["id"], now=now)
            after = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
            self._audit(
                connection, action="registration.cancel", actor=actor, event_id=event_id,
                registration_id=reg["id"], before=self._public_registration(before),
                after=self._public_registration(after),
            )
            promoted = self._promote_waitlist(connection, event, now_value, actor)
            result = self._public_registration(after)
            result["promoted"] = [pid for pid, _ in promoted]
            return result

    # ------------------------------------------------------------- expiry / promotion

    def expire_due_offers(self, event_id: int | None = None, actor: str = "system") -> dict[str, Any]:
        """释放所有已逾期待确认名额（超时释放），并即时按序提升候补（候补转正）。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        expired: list[str] = []
        promoted: list[str] = []
        with transaction(immediate=True) as connection:
            if event_id is not None:
                event_ids = [int(self._get_event(connection, event_id)["id"])]
            else:
                event_ids = [int(row[0]) for row in connection.execute("SELECT id FROM birding_events").fetchall()]
            for eid in event_ids:
                event = self._get_event(connection, eid)
                due = connection.execute(
                    "SELECT * FROM birding_registrations WHERE event_id=? AND status='offered'"
                    " AND offer_expires_at IS NOT NULL AND offer_expires_at <= ? ORDER BY id",
                    (eid, now),
                ).fetchall()
                for reg in due:
                    self._expire_registration(connection, event, reg, now, actor, reason="confirm_timeout")
                    expired.append(reg["participant_id"])
                    # 每释放一个名额立即提升一位，保证严格按原始候补顺序。
                    promoted_in_event = self._promote_waitlist(connection, event, now_value, actor, slots=1)
                    promoted.extend(pid for pid, _ in promoted_in_event)
        return {"expired": expired, "promoted": promoted}

    def _expire_registration(
        self,
        connection: sqlite3.Connection,
        event: sqlite3.Row,
        reg: sqlite3.Row,
        now: str,
        actor: str,
        *,
        reason: str,
    ) -> None:
        before = dict(reg)
        connection.execute(
            "UPDATE birding_registrations SET status='expired', updated_at=? WHERE id=? AND status='offered'",
            (now, reg["id"]),
        )
        self._cancel_pending_offer_notifications(connection, registration_id=reg["id"], now=now)
        after = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
        self._audit(
            connection, action="offer.expire", actor=actor, event_id=event["id"],
            registration_id=reg["id"], before=self._public_registration(before),
            after=self._public_registration(after), metadata={"reason": reason},
        )

    def _promote_waitlist(
        self,
        connection: sqlite3.Connection,
        event: sqlite3.Row,
        now_value,
        actor: str,
        *,
        reason: str = "slot_available",
        slots: int | None = None,
    ) -> list[tuple[str, int]]:
        """按 queue_seq 顺序把候补者转为 offered，直到名额填满或候补耗尽。

        只有状态仍为 ``waitlisted`` 的报名者会被提升——被取消或被取消资格
        （disqualify 转为 ``ineligible``）的人不在候选集合中，因此「取消后
        只提升仍符合资格的人」。每次提升生成一条独立的 promotion 通知。
        """
        if event["status"] != "open":
            return []
        promoted: list[tuple[str, int]] = []
        waitlist = connection.execute(
            "SELECT * FROM birding_registrations WHERE event_id=? AND status='waitlisted' ORDER BY queue_seq",
            (event["id"],),
        ).fetchall()
        for reg in waitlist:
            if slots is not None and len(promoted) >= slots:
                break
            if self._occupied(connection, event["id"]) >= int(event["capacity"]):
                break
            now = to_storage(now_value)
            expires = to_storage(now_value + timedelta(minutes=int(event["confirm_timeout_minutes"])))
            connection.execute(
                "UPDATE birding_registrations SET status='offered', offer_expires_at=?,"
                " offered_at=COALESCE(offered_at,?), updated_at=? WHERE id=? AND status='waitlisted'",
                (expires, now, now, reg["id"]),
            )
            updated = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
            self._enqueue_notification(connection, event_id=event["id"], registration=updated, kind="promotion", now=now)
            self._audit(
                connection, action="waitlist.promote", actor=actor, event_id=event["id"],
                registration_id=reg["id"], before=self._public_registration(reg),
                after=self._public_registration(updated), metadata={"reason": reason},
            )
            promoted.append((reg["participant_id"], int(reg["id"])))
        return promoted

    # --------------------------------------------------------------- eligibility

    def disqualify(self, event_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """标记报名人不符合资格：占用的名额作废并触发候补提升；候补则出局不参与提升。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            event = self._get_event(connection, event_id)
            reg = self._require_registration(connection, event_id, payload["participant_id"])
            before = dict(reg)
            if reg["status"] in ("offered", "confirmed", "waitlisted"):
                connection.execute(
                    "UPDATE birding_registrations SET status='ineligible', ineligible_reason=?,"
                    " offer_expires_at=NULL, updated_at=? WHERE id=?",
                    (payload["reason"], now, reg["id"]),
                )
                self._cancel_pending_offer_notifications(connection, registration_id=reg["id"], now=now)
            after = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
            self._audit(
                connection, action="registration.disqualify", actor=actor, event_id=event_id,
                registration_id=reg["id"], before=self._public_registration(before),
                after={"id": after["id"], "status": after["status"]}, metadata={"reason": payload["reason"]},
            )
            promoted: list[tuple[str, int]] = []
            if before["status"] in SEAT_STATUSES:
                promoted = self._promote_waitlist(connection, event, now_value, actor, reason="disqualified")
            result = self._public_registration(after)
            result["promoted"] = [pid for pid, _ in promoted]
            return result

    def requalify(self, event_id: int, participant_id: str, actor: str) -> dict[str, Any]:
        """恢复资格：被取消资格者回到候补队尾（场次关闭时仅恢复标记，不重新排队）。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            event = self._get_event(connection, event_id)
            reg = self._require_registration(connection, event_id, participant_id)
            before = dict(reg)
            if reg["status"] != "ineligible":
                raise ConflictError("仅不符合资格状态的报名可以恢复")
            new_status = "waitlisted" if event["status"] == "open" else "cancelled"
            queue_seq = self._next_queue_seq(connection, event_id) if new_status == "waitlisted" else None
            connection.execute(
                "UPDATE birding_registrations SET status=?, queue_seq=?, offer_expires_at=NULL,"
                " ineligible_reason='', updated_at=? WHERE id=?",
                (new_status, queue_seq, now, reg["id"]),
            )
            after = connection.execute("SELECT * FROM birding_registrations WHERE id=?", (reg["id"],)).fetchone()
            self._audit(
                connection, action="registration.requalify", actor=actor, event_id=event_id,
                registration_id=reg["id"], before=self._public_registration(before),
                after=self._public_registration(after),
            )
            promoted = self._promote_waitlist(connection, event, now_value, actor, reason="requalified")
            result = self._public_registration(after)
            result["promoted"] = [pid for pid, _ in promoted]
            return result

    # ----------------------------------------------------- delivery test support

    def block_delivery(self, contact: str, reason: str, actor: str = "operator") -> dict[str, Any]:
        """模拟某联系方式的通知渠道持续失败（仅影响通知投递，不影响报名资格）。"""
        now = self._now()
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO birding_delivery_blocks(contact,reason,created_at) VALUES(?,?,?) "
                "ON CONFLICT(contact) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at",
                (contact, reason, now),
            )
            self._audit(connection, action="delivery.block", actor=actor, metadata={"contact": contact, "reason": reason})
            return {"contact": contact, "blocked": True, "reason": reason}

    def unblock_delivery(self, contact: str, actor: str = "operator") -> dict[str, Any]:
        now = self._now()
        with transaction(immediate=True) as connection:
            connection.execute("DELETE FROM birding_delivery_blocks WHERE contact=?", (contact,))
            self._audit(connection, action="delivery.unblock", actor=actor, metadata={"contact": contact}, created_at=now)
            return {"contact": contact, "blocked": False}

    # ------------------------------------------------------------- notifications

    def _deliver(self, notification: sqlite3.Row) -> None:
        """实际发送通道。被 block 的联系方式持续失败，用于验收通知失败与重试。"""
        blocked = self.connection.execute(
            "SELECT reason FROM birding_delivery_blocks WHERE contact=?", (notification["target"],)
        ).fetchone()
        if blocked is not None:
            raise RuntimeError(f"渠道暂时不可用：{blocked['reason']}")

    def process_notifications(self, *, limit: int = 50, actor: str = "notifier") -> dict[str, Any]:
        """尝试投递待发通知。失败按指数退避重试，重试只更新同一条记录、绝不重复占位。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        sent: list[int] = []
        failed: list[int] = []
        with transaction(immediate=True) as connection:
            pending = connection.execute(
                "SELECT * FROM birding_notifications WHERE status IN ('pending','failed') AND available_at<=? ORDER BY id LIMIT ?",
                (now, max(1, min(limit, 500))),
            ).fetchall()
            for item in pending:
                attempts_after = int(item["attempts"]) + 1
                connection.execute(
                    "UPDATE birding_notifications SET status='sending', attempts=?, updated_at=? WHERE id=?"
                    " AND status IN ('pending','failed')",
                    (attempts_after, now, item["id"]),
                )
                try:
                    self._deliver(item)
                except Exception as exc:  # 通知失败：退避重试，占位状态不受影响
                    error = str(exc)[:200]
                    if attempts_after >= int(item["max_attempts"]):
                        connection.execute(
                            "UPDATE birding_notifications SET status='failed', last_error=?, updated_at=? WHERE id=?",
                            (error, now, item["id"]),
                        )
                    else:
                        available = to_storage(now_value + timedelta(seconds=min(2 ** int(item["attempts"]), 300)))
                        connection.execute(
                            "UPDATE birding_notifications SET status='failed', last_error=?, available_at=?, updated_at=? WHERE id=?",
                            (error, available, now, item["id"]),
                        )
                    failed.append(item["id"])
                    self._audit(
                        connection, action="notification.failed", actor=actor, event_id=item["event_id"],
                        registration_id=item["registration_id"],
                        metadata={"notification_id": item["id"], "kind": item["kind"], "error": error, "attempt": attempts_after},
                    )
                else:
                    connection.execute(
                        "UPDATE birding_notifications SET status='sent', sent_at=?, last_error='', updated_at=? WHERE id=?",
                        (now, now, item["id"]),
                    )
                    sent.append(item["id"])
                    self._audit(
                        connection, action="notification.sent", actor=actor, event_id=item["event_id"],
                        registration_id=item["registration_id"],
                        metadata={"notification_id": item["id"], "kind": item["kind"], "attempt": attempts_after},
                    )
        return {"sent": sent, "failed": failed}

    def list_notifications(self, event_id: int | None = None, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM birding_notifications WHERE 1=1"
        params: list[Any] = []
        if event_id is not None:
            sql += " AND event_id=?"
            params.append(event_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ queries

    def _require_registration(
        self, connection: sqlite3.Connection, event_id: int, participant_id: str
    ) -> sqlite3.Row:
        reg = connection.execute(
            "SELECT * FROM birding_registrations WHERE event_id=? AND participant_id=?",
            (event_id, participant_id.strip().upper()),
        ).fetchone()
        if reg is None:
            raise NotFoundError("报名记录不存在")
        return reg

    def get_registration(self, event_id: int, participant_id: str) -> dict[str, Any]:
        return self._public_registration(self._require_registration(self.connection, event_id, participant_id))

    def list_registrations(self, event_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
        self._get_event(self.connection, event_id)
        sql = "SELECT * FROM birding_registrations WHERE event_id=?"
        params: list[Any] = [event_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY COALESCE(queue_seq, 999999), id"
        return [self._public_registration(row) for row in self.connection.execute(sql, params).fetchall()]

    def waitlist_queue(self, event_id: int) -> list[dict[str, Any]]:
        self._get_event(self.connection, event_id)
        rows = self.connection.execute(
            "SELECT id,participant_id,participant_name,contact,status,queue_seq FROM birding_registrations "
            "WHERE event_id=? AND status IN ('waitlisted','ineligible') ORDER BY queue_seq, id",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def audit_trail(
        self, *, event_id: int | None = None, registration_id: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """按时间正序返回审计事件，所有状态变化均可据此完整还原。"""
        sql = "SELECT * FROM birding_audit WHERE 1=1"
        params: list[Any] = []
        if event_id is not None:
            sql += " AND event_id=?"
            params.append(event_id)
        if registration_id is not None:
            sql += " AND registration_id=?"
            params.append(registration_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        trail: list[dict[str, Any]] = []
        for row in self.connection.execute(sql, params).fetchall():
            item = dict(row)
            for key in ("before_json", "after_json", "metadata_json"):
                item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
            trail.append(item)
        return trail
