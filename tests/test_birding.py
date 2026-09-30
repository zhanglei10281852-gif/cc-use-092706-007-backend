from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.birding import router as birding_router
from app.core.clock import FrozenClock


@pytest.fixture()
def staff_headers(client) -> dict:
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def outsider_headers(client, staff_headers) -> dict:
    """拥有 auditor 角色但没有任何 birding 权限的账号。"""
    created = client.post(
        "/api/users",
        json={"username": "auditor1", "password": "Auditor!123", "display_name": "审计员", "role_codes": ["auditor"]},
        headers=staff_headers,
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": "auditor1", "password": "Auditor!123", "client_label": "tests"})
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def frozen_clock():
    clock = FrozenClock(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    birding_router.clock_override = clock
    try:
        yield clock
    finally:
        birding_router.clock_override = None


def make_event(client, headers, *, capacity=2, waitlist=3, timeout_minutes=30, clock=None, **overrides) -> int:
    base = clock.now() if clock else datetime.now(UTC)
    payload = {
        "title": "奥林匹克森林公园观鸟导赏",
        "location": "奥森南园",
        "leader": "飞羽志愿者",
        "start_at": (base + timedelta(days=7)).isoformat(),
        "capacity": capacity,
        "waitlist_capacity": waitlist,
        "confirm_timeout_minutes": timeout_minutes,
        "registration_opens_at": (base - timedelta(hours=1)).isoformat(),
        "registration_closes_at": (base + timedelta(days=1)).isoformat(),
    }
    payload.update(overrides)
    response = client.post("/api/birding/events", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def register(client, event_id, applicant, key, *, name=None, phone="13800000000"):
    return client.post(
        f"/api/birding/events/{event_id}/registrations",
        json={"applicant": applicant, "applicant_name": name or f"访客{applicant}", "phone": phone, "idempotency_key": key},
    )


# --------------------------------------------------------------------- 权限隔离

def test_permission_isolation(client, staff_headers, outsider_headers):
    event_id = make_event(client, staff_headers)

    # 无令牌不能创建活动 → 401
    forbidden = client.post(
        "/api/birding/events",
        json={
            "title": "x", "location": "x", "leader": "x",
            "start_at": "2026-10-20T09:00:00+00:00", "capacity": 1,
            "registration_opens_at": "2026-09-01T09:00:00+00:00",
            "registration_closes_at": "2026-10-19T09:00:00+00:00",
        },
    )
    # 未携带令牌 → 401
    assert forbidden.status_code == 401

    # 审计员账号无 birding 权限 → 403
    denied = client.post(
        f"/api/birding/events/{event_id}/close", json={"reason": "test"}, headers=outsider_headers
    )
    assert denied.status_code == 403
    assert client.get(f"/api/birding/events/{event_id}/roster", headers=outsider_headers).status_code == 403
    # 工作人员可以看名单
    assert client.get(f"/api/birding/events/{event_id}/roster", headers=staff_headers).status_code == 200

    # 公开报名不需要登录
    public = register(client, event_id, "public-1", "k-public-1")
    assert public.status_code == 201, public.text
    reg_id = public.json()["id"]

    # 报名人身份隔离：A 不能查看/操作 B 的报名
    assert client.get(f"/api/birding/registrations/{reg_id}?applicant=someone-else").status_code == 403
    assert client.post(f"/api/birding/registrations/{reg_id}/confirm?applicant=someone-else").status_code == 403
    # 本人可以操作
    assert client.post(f"/api/birding/registrations/{reg_id}/confirm?applicant=public-1").status_code == 200


# ----------------------------------------------------------------- 并发与幂等

def test_concurrent_registration_respects_capacity(client, staff_headers):
    capacity, waitlist = 3, 20
    event_id = make_event(client, staff_headers, capacity=capacity, waitlist=waitlist)

    def submit(index: int):
        return register(client, event_id, f"racer-{index:03d}", f"race-key-{index:03d}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(submit, range(18)))

    assert all(response.status_code == 201 for response in responses)
    summary = client.get(f"/api/birding/events/{event_id}").json()
    assert summary["status_counts"].get("offered", 0) == capacity
    assert summary["status_counts"].get("registered", 0) == 18 - capacity
    assert summary["occupied"] == capacity

    # 座位号 1..capacity 且不重复
    roster = client.get(f"/api/birding/events/{event_id}/roster", headers=staff_headers).json()
    seats = sorted(item["seat_no"] for item in roster["registrations"] if item["seat_no"] is not None)
    assert seats == list(range(1, capacity + 1))


def test_duplicate_requests_are_idempotent(client, staff_headers):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=2)

    payload = {"applicant": "dup-user", "applicant_name": "重复报名者", "idempotency_key": "idem-1"}
    first = client.post(f"/api/birding/events/{event_id}/registrations", json=payload)
    assert first.status_code == 201

    # 同一幂等键重放：返回首次结果，不产生第二行
    replay = client.post(f"/api/birding/events/{event_id}/registrations", json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    # 同一身份换幂等键（并发重试的典型形态）：幂等返回 200 + duplicate，仍只有一行
    other_key = client.post(
        f"/api/birding/events/{event_id}/registrations",
        json={"applicant": "DUP-USER", "applicant_name": "重复报名者", "idempotency_key": "idem-2"},
    )
    assert other_key.status_code == 200
    assert other_key.json()["duplicate"] is True
    assert other_key.json()["id"] == first.json()["id"]

    # 同键不同体 → 409
    conflict = client.post(
        f"/api/birding/events/{event_id}/registrations",
        json={"applicant": "dup-user", "applicant_name": "改名了", "idempotency_key": "idem-1"},
    )
    assert conflict.status_code == 409

    roster = client.get(f"/api/birding/events/{event_id}/roster", headers=staff_headers).json()
    assert len(roster["registrations"]) == 1


def test_waitlist_overflow_rejected(client, staff_headers):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=1)
    assert register(client, event_id, "a", "ka").status_code == 201
    assert register(client, event_id, "b", "kb").status_code == 201
    overflow = register(client, event_id, "c", "kc")
    assert overflow.status_code == 409
    assert "候补" in overflow.json()["error"]["message"]


# ----------------------------------------------------------- 超时释放与候补转正

def test_expiry_releases_seat_and_promotes_waitlist(client, staff_headers, frozen_clock):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=3, timeout_minutes=30, clock=frozen_clock)

    first = register(client, event_id, "holder", "k-holder").json()
    w1 = register(client, event_id, "wait-1", "k-w1").json()
    w2 = register(client, event_id, "wait-2", "k-w2").json()
    assert first["status"] == "offered"
    assert w1["status"] == w2["status"] == "registered"

    # 未到期限：sweep 不动作
    frozen_clock.advance(minutes=29)
    assert client.post("/api/birding/sweep/expired", headers=staff_headers).json() == {"expired": [], "promoted": []}

    # 超时后 sweep：holder 名额回收，最早候补 w1 转正
    frozen_clock.advance(minutes=2)
    swept = client.post("/api/birding/sweep/expired", headers=staff_headers).json()
    assert swept["expired"] == [first["id"]]
    assert swept["promoted"] == [w1["id"]]

    promoted = client.get(f"/api/birding/registrations/{w1['id']}?applicant=wait-1").json()
    assert promoted["status"] == "offered"
    assert promoted["seat_no"] == 1
    expired = client.get(f"/api/birding/registrations/{first['id']}?applicant=holder").json()
    assert expired["status"] == "expired" and expired["seat_no"] is None

    # 超过确认期限的确认请求必须被拒绝
    late = client.post(f"/api/birding/registrations/{first['id']}/confirm?applicant=holder")
    assert late.status_code == 409

    # w1 及时确认
    frozen_clock.advance(minutes=10)
    confirmed = client.post(f"/api/birding/registrations/{w1['id']}/confirm?applicant=wait-1")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    # 重复确认幂等
    assert client.post(f"/api/birding/registrations/{w1['id']}/confirm?applicant=wait-1").json()["duplicate"] is True

    # w2 始终未被动过
    assert client.get(f"/api/birding/registrations/{w2['id']}?applicant=wait-2").json()["status"] == "registered"


def test_cancel_offered_promotes_next_waitlister(client, staff_headers, frozen_clock):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=2, timeout_minutes=30, clock=frozen_clock)
    holder = register(client, event_id, "holder", "k-holder").json()
    waiter = register(client, event_id, "wait-1", "k-w1").json()

    cancelled = client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant=holder", json={"reason": "没空"})
    assert cancelled.status_code == 200
    promoted = client.get(f"/api/birding/registrations/{waiter['id']}?applicant=wait-1").json()
    assert promoted["status"] == "offered" and promoted["seat_no"] == 1

    # 重复取消幂等，且不会再次触发晋升（没有第二个候补，结果保持不变）
    again = client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant=holder", json={})
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    assert client.get(f"/api/birding/registrations/{waiter['id']}?applicant=wait-1").json()["status"] == "offered"


def test_blocked_waitlister_is_skipped_and_retains_position(client, staff_headers, frozen_clock):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=3, timeout_minutes=30, clock=frozen_clock)
    holder = register(client, event_id, "holder", "kh").json()
    blocked = register(client, event_id, "bp", "kbp").json()   # 最早候补，但资格存疑
    eligible = register(client, event_id, "ep", "kep").json()  # 正常候补

    # 志愿者暂停最早候补的资格
    blocked_op = client.post(f"/api/birding/registrations/{blocked['id']}/block", json={"reason": "重复身份待核"}, headers=staff_headers)
    assert blocked_op.status_code == 200 and blocked_op.json()["status"] == "blocked"

    # holder 取消：跳过 blocked，只提升仍合格的 eligible
    client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant=holder", json={})
    assert client.get(f"/api/birding/registrations/{blocked['id']}?applicant=bp").json()["status"] == "blocked"
    promoted = client.get(f"/api/birding/registrations/{eligible['id']}?applicant=ep").json()
    assert promoted["status"] == "offered" and promoted["seat_no"] == 1

    # 恢复资格：blocked 重新参与晋升，但当前无空缺，保持排队
    unblocked = client.post(f"/api/birding/registrations/{blocked['id']}/unblock", json={"reason": "核实通过"}, headers=staff_headers)
    assert unblocked.json()["status"] == "registered"


# --------------------------------------------------------------- 活动关闭流转

def test_close_event_rejects_outstanding_and_keeps_confirmed(client, staff_headers, frozen_clock):
    event_id = make_event(client, staff_headers, capacity=2, waitlist=2, timeout_minutes=30, clock=frozen_clock)
    confirmed_reg = register(client, event_id, "keeper", "kk").json()
    offered_reg = register(client, event_id, "undecided", "ku").json()
    wait_reg = register(client, event_id, "waiting", "kw").json()
    client.post(f"/api/birding/registrations/{confirmed_reg['id']}/confirm?applicant=keeper")

    closed = client.post(f"/api/birding/events/{event_id}/close", json={"reason": "天气原因"}, headers=staff_headers)
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "closed"
    assert body["status_counts"]["confirmed"] == 1
    assert body["status_counts"]["rejected"] == 2

    assert client.get(f"/api/birding/registrations/{offered_reg['id']}?applicant=undecided").json()["status"] == "rejected"
    assert client.get(f"/api/birding/registrations/{wait_reg['id']}?applicant=waiting").json()["status"] == "rejected"
    assert client.get(f"/api/birding/registrations/{confirmed_reg['id']}?applicant=keeper").json()["status"] == "confirmed"

    # 关闭后不能再报名、不能重复关闭
    assert register(client, event_id, "late", "klate").status_code == 409
    assert client.post(f"/api/birding/events/{event_id}/close", json={}, headers=staff_headers).status_code == 409


# ----------------------------------------------------------- 通知重试不重复占位

def test_notification_failure_retries_without_double_seat(client, staff_headers, frozen_clock):
    attempts = {"n": 0}

    def flaky_sender(_payload):
        attempts["n"] += 1
        raise RuntimeError("短信网关超时")

    birding_router.sender_override = flaky_sender
    try:
        event_id = make_event(client, staff_headers, capacity=1, waitlist=1, timeout_minutes=30, clock=frozen_clock)
        reg = register(client, event_id, "holder", "kholder").json()
        note_id = reg["offer_notification"]["id"]

        # 第一次派发失败：退避 30 秒
        first = client.post("/api/birding/notifications/dispatch", headers=staff_headers).json()
        assert first["failed"] == [note_id] and first["sent"] == []

        # 未到退避时间：不会重发
        frozen_clock.advance(seconds=20)
        assert client.post("/api/birding/notifications/dispatch", headers=staff_headers).json() == {"sent": [], "failed": []}

        # 报名单状态与名额完全不受通知失败影响
        view = client.get(f"/api/birding/registrations/{reg['id']}?applicant=holder").json()
        assert view["status"] == "offered" and view["seat_no"] == 1

        # 人工重试立即到期；网关恢复后送达，整个过程只有一条通知（不重复占位）
        client.post(f"/api/birding/notifications/{note_id}/retry", headers=staff_headers)
        birding_router.sender_override = lambda _payload: None
        result = client.post("/api/birding/notifications/dispatch", headers=staff_headers).json()
        assert result["sent"] == [note_id]

        roster = client.get(f"/api/birding/events/{event_id}/roster", headers=staff_headers).json()
        offer_notes = [n for n in roster["notifications"] if n["channel"] == "offer"]
        assert len(offer_notes) == 1
        assert offer_notes[0]["status"] == "sent"
        assert sum(1 for r in roster["registrations"] if r["seat_no"] is not None) == 1
    finally:
        birding_router.sender_override = None


def test_voided_offer_notification_is_not_retried(client, staff_headers, frozen_clock):
    birding_router.sender_override = lambda _payload: (_ for _ in ()).throw(RuntimeError("网关宕机"))
    try:
        event_id = make_event(client, staff_headers, capacity=1, waitlist=1, clock=frozen_clock)
        holder = register(client, event_id, "holder", "kh").json()
        note_id = holder["offer_notification"]["id"]
        client.post("/api/birding/notifications/dispatch", headers=staff_headers)

        # holder 取消，名额给候补；holder 的旧 offer 通知必须作废而不是继续重试
        register(client, event_id, "waiter", "kw")
        client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant=holder", json={})
        retry = client.post(f"/api/birding/notifications/{note_id}/retry", headers=staff_headers)
        assert retry.status_code == 409
        frozen_clock.advance(minutes=5)
        # 作废通知 attempts 已达上限，不会再被派发
        result = client.post("/api/birding/notifications/dispatch", headers=staff_headers).json()
        assert note_id not in result["sent"] and note_id not in result["failed"]
    finally:
        birding_router.sender_override = None


# ----------------------------------------------------------------- 审计可还原

def test_all_transitions_recorded_in_audit_and_history(client, staff_headers, frozen_clock):
    event_id = make_event(client, staff_headers, capacity=1, waitlist=2, timeout_minutes=30, clock=frozen_clock)
    holder = register(client, event_id, "holder", "kh").json()
    waiter = register(client, event_id, "waiter", "kw").json()
    client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant=holder", json={"reason": "出差"})

    # 报名单自带的状态轨迹：registered → offered → cancelled；waiter: registered → offered
    holder_history = client.get(f"/api/birding/registrations/{holder['id']}/history?applicant=holder").json()["items"]
    transitions = [(item["from_status"], item["to_status"]) for item in holder_history]
    assert transitions == [(None, "registered"), ("registered", "offered"), ("offered", "cancelled")]

    waiter_history = client.get(f"/api/birding/registrations/{waiter['id']}/history?applicant=waiter").json()["items"]
    assert (waiter_history[-1]["from_status"], waiter_history[-1]["to_status"]) == ("registered", "offered")

    # 全局审计事件可按资源类型检索，足以还原每个动作
    audit = client.get("/api/audit?resource_type=birding_registration&size=100", headers=staff_headers).json()
    actions = {item["action"] for item in audit["data"]}
    assert "birding.registration.offer" in actions
    assert "birding.registration.cancel" in actions
    assert "birding.registration.promote" in actions
    # 每条审计带 before/after 快照
    promote_events = [item for item in audit["data"] if item["action"] == "birding.registration.promote"]
    assert promote_events[0]["after_json"] is not None

    event_audit = client.get("/api/audit?resource_type=birding_event&size=100", headers=staff_headers).json()
    assert any(item["action"] == "birding.event.create" for item in event_audit["data"])


def test_registration_window_enforced(client, staff_headers, frozen_clock):
    # 报名尚未开放
    future = make_event(
        client, staff_headers,
        registration_opens_at=(frozen_clock.now() + timedelta(hours=2)).isoformat(),
        registration_closes_at=(frozen_clock.now() + timedelta(days=1)).isoformat(),
        start_at=(frozen_clock.now() + timedelta(days=2)).isoformat(),
    )
    assert register(client, future, "early", "ke").status_code == 409

    # 报名已结束
    past = make_event(
        client, staff_headers,
        registration_opens_at=(frozen_clock.now() - timedelta(days=2)).isoformat(),
        registration_closes_at=(frozen_clock.now() - timedelta(hours=1)).isoformat(),
        start_at=(frozen_clock.now() + timedelta(days=1)).isoformat(),
    )
    assert register(client, past, "late", "kl").status_code == 409
