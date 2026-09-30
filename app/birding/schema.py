from __future__ import annotations

# 活动场次：登记开放/关闭状态、容量与候补升级的确认期限。
SCHEMA = """
CREATE TABLE IF NOT EXISTS birding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    leader TEXT NOT NULL,
    start_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    waitlist_capacity INTEGER NOT NULL CHECK(waitlist_capacity >= 0),
    confirm_timeout_minutes INTEGER NOT NULL CHECK(confirm_timeout_minutes > 0),
    registration_opens_at TEXT NOT NULL,
    registration_closes_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    closed_at TEXT,
    close_reason TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_by_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(registration_closes_at > registration_opens_at),
    CHECK(start_at > registration_closes_at)
);

CREATE TABLE IF NOT EXISTS birding_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES birding_events(id) ON DELETE RESTRICT,
    applicant TEXT NOT NULL COLLATE NOCASE,
    applicant_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    -- registered 候补入列（或待分配）；blocked 暂不符合资格（保留原始排队位置）；
    -- offered 候补转正/直接获得名额，待确认；confirmed 已确认占名额；
    -- expired 确认超时被回收；cancelled 报名人取消；rejected 活动关闭时清退。
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK(status IN ('registered','blocked','offered','confirmed','expired','cancelled','rejected')),
    -- 占名额序号（1..capacity），offered 预留、confirmed 持有；候补期为 NULL。
    seat_no INTEGER,
    queued_at TEXT NOT NULL,
    offered_at TEXT,
    offer_expires_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, applicant)
);

CREATE INDEX IF NOT EXISTS idx_birding_reg_event_status
    ON birding_registrations(event_id, status, queued_at, id);

CREATE TABLE IF NOT EXISTS birding_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES birding_events(id) ON DELETE CASCADE,
    registration_id INTEGER NOT NULL REFERENCES birding_registrations(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('offer','reminder','cancel')),
    -- pending 待发送；sent 已送达；failed 本次发送失败（可重试，与占位无关）。
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    next_attempt_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(registration_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_birding_notifications_due
    ON birding_notifications(status, next_attempt_at);

-- 状态机迁移轨迹：每行一次迁移，按 id 回放即可还原报名单完整历史。
CREATE TABLE IF NOT EXISTS birding_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES birding_events(id) ON DELETE CASCADE,
    registration_id INTEGER NOT NULL REFERENCES birding_registrations(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    seat_no INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_birding_history_reg ON birding_status_history(registration_id, id);
CREATE INDEX IF NOT EXISTS idx_birding_history_event ON birding_status_history(event_id, id);
"""
