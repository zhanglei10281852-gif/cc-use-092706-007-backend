from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from app.birding import runtime
from app.birding.service import BirdingService
from app.core.clock import FrozenClock
from app.database import get_connection

PASSWORD = "Volunteer!2345"


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def reset_birding_clock():
    runtime.reset_clock()
    yield
    runtime.reset_clock()


def make_user(client, admin, username, roles):
    response = client.post(
        "/api/users",
        json={"username": username, "password": PASSWORD, "display_name": username, "role_codes": roles},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "client_label": "t"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def identities(client, admin):
    return {
        "organizer": make_user(client, admin, "org_a", ["birding_organizer"]),
        "organizer_b": make_user(client, admin, "org_b", ["birding_organizer"]),
        "volunteer": make_user(client, admin, "vol_one", ["birding_volunteer"]),
        "volunteer2": make_user(client, admin, "vol_two", ["birding_volunteer"]),
        "plain": make_user(client, admin, "plain_user", ["birding_volunteer"]),
    }


def create_event(client, headers, code="SPRING-01", *, capacity=2, timeout=30, waitlist_capacity=None):
    payload = {
        "code": code,
        "title": "周末湿地观鸟导赏",
        "leader": "飞羽",
        "location": "野鸭湖",
        "capacity": capacity,
        "confirm_timeout_minutes": timeout,
        "waitlist_capacity": waitlist_capacity,
    }
    response = client.post("/api/birding/events", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def signup(client, headers, event_id, pid, *, name=None, contact=None):
    return client.post(
        f"/api/birding/events/{event_id}/signups",
        json={
            "participant_id": pid,
            "participant_name": name or f"访客{pid}",
            "contact": contact or f"139{pid[-8:].zfill(8)}",
        },
        headers=headers,
    )


def action(client, headers, event_id, pid, verb):
    return client.post(f"/api/birding/events/{event_id}/signups/{pid}/{verb}", headers=headers)


# ------------------------------------------------------------- 活动与报名基础


def test_event_lifecycle_and_idempotent_signup(client, identities):
    event = create_event(client, identities["organizer"], capacity=1)
    assert event["status"] == "open"

    first = signup(client, identities["volunteer"], event["id"], "ID0000001")
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "offered"

    # 同一身份重复请求幂等：回放同一条记录，不重复占位，HTTP 200。
    second = signup(client, identities["volunteer2"], event["id"], "ID0000001")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["status"] == "offered"

    detail = client.get(f"/api/birding/events/{event['id']}", headers=identities["volunteer"]).json()
    assert detail["occupied"] == 1
    assert detail["available"] == 0

    notifications = client.get(
        f"/api/birding/notifications?event_id={event['id']}", headers=identities["organizer"]
    ).json()["items"]
    assert len(notifications) == 1 and notifications[0]["kind"] == "offer"

    # 满员后新身份进入候补。
    waiting = signup(client, identities["volunteer2"], event["id"], "ID0000002")
    assert waiting.status_code == 201
    assert waiting.json()["status"] == "waitlisted"
    assert waiting.json()["queue_seq"] == 1

    # 候补队列有容量上限时返回冲突。
    full_event = create_event(client, identities["organizer"], "FULL-01", capacity=1, waitlist_capacity=1)
    signup(client, identities["volunteer"], full_event["id"], "ID0000101")
    signup(client, identities["volunteer2"], full_event["id"], "ID0000102")
    overflow = signup(client, identities["plain"], full_event["id"], "ID0000103")
    assert overflow.status_code == 409


def test_confirmation_within_deadline_and_repeated_confirm_idempotent(client, identities):
    event = create_event(client, identities["organizer"], capacity=1)
    signup(client, identities["volunteer"], event["id"], "ID0000010")
    confirmed = action(client, identities["volunteer"], event["id"], "ID0000010", "confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    again = action(client, identities["volunteer"], event["id"], "ID0000010", "confirm")
    assert again.status_code == 200 and again.json()["status"] == "confirmed"


# ------------------------------------------------------------- 并发报名


def test_concurrent_signups_never_oversell_and_duplicate_identity_is_idempotent(client, identities):
    event = create_event(client, identities["organizer"], capacity=3, waitlist_capacity=100)
    event_id = event["id"]
    barrier = threading.Barrier(12)
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(pid):
        barrier.wait()
        try:
            # 每个线程使用独立连接（thread-local），真实争抢同一 SQLite 文件。
            reg, created = BirdingService().signup(
                event_id,
                {"participant_id": pid, "participant_name": f"P{pid}", "contact": f"139{pid}"},
                actor="vol_one",
            )
            with lock:
                results.append(reg)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"IDC{index:05d}",)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    statuses = [reg["status"] for reg in results]
    assert statuses.count("offered") == 3
    assert statuses.count("waitlisted") == 9
    detail = client.get(f"/api/birding/events/{event_id}", headers=identities["volunteer"]).json()
    assert detail["occupied"] == 3

    # 同一身份并发重复报名：只有一条记录、一条占位通知。
    dup_event = create_event(client, identities["organizer"], "DUP-01", capacity=5)
    barrier2 = threading.Barrier(10)
    dup_errors: list[Exception] = []

    def duplicate_worker():
        barrier2.wait()
        try:
            BirdingService().signup(
                dup_event["id"],
                {"participant_id": "SAMEID01", "participant_name": "同一人", "contact": "13800000001"},
                actor="vol_one",
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                dup_errors.append(exc)

    pool = [threading.Thread(target=duplicate_worker) for _ in range(10)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    assert not dup_errors, dup_errors

    connection = get_connection()
    reg_count = connection.execute(
        "SELECT COUNT(*) FROM birding_registrations WHERE event_id=? AND participant_id='SAMEID01'",
        (dup_event["id"],),
    ).fetchone()[0]
    notif_count = connection.execute(
        "SELECT COUNT(*) FROM birding_notifications WHERE event_id=? AND participant_id='SAMEID01'",
        (dup_event["id"],),
    ).fetchone()[0]
    assert reg_count == 1
    assert notif_count == 1


# ------------------------------------------------------------- 超时释放与候补转正


def test_timeout_releases_slot_and_promotes_waitlist_in_order(client, identities, admin):
    frozen = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    runtime.set_clock(frozen)
    event = create_event(client, identities["organizer"], capacity=1, timeout=30)
    signup(client, identities["volunteer"], event["id"], "ID0000020")  # offered
    signup(client, identities["volunteer2"], event["id"], "ID0000021")  # waitlisted seq1
    signup(client, identities["plain"], event["id"], "ID0000022")       # waitlisted seq2

    # 期限内确认正常。
    frozen.advance(minutes=31)
    late_confirm = action(client, identities["volunteer"], event["id"], "ID0000020", "confirm")
    assert late_confirm.status_code == 409

    run = client.post("/api/birding/expirations/run", headers=admin["headers"])
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["expired"] == ["ID0000020"]
    assert body["promoted"] == ["ID0000021"]  # 严格按队首提升

    promoted = action(client, identities["volunteer2"], event["id"], "ID0000021", "confirm")
    assert promoted.json()["status"] == "confirmed"

    # 队首确认后，第二名仍在候补，不会被越级提升。
    second = client.get(
        f"/api/birding/events/{event['id']}/registrations?status=waitlisted",
        headers=identities["organizer"],
    ).json()["items"]
    assert [item["participant_id"] for item in second] == ["ID0000022"]

    # 幂等：再次回收没有新动作。
    again = client.post("/api/birding/expirations/run", headers=admin["headers"]).json()
    assert again == {"expired": [], "promoted": []}


def test_cancel_promotes_only_still_eligible_waitlisters(client, identities):
    event = create_event(client, identities["organizer"], capacity=1)
    signup(client, identities["volunteer"], event["id"], "ID0000030")  # offered
    signup(client, identities["volunteer2"], event["id"], "ID0000031")  # waitlisted seq1
    signup(client, identities["plain"], event["id"], "ID0000032")       # waitlisted seq2

    # 队首候补被取消资格，释放名额时必须跳过。
    disq = client.post(
        f"/api/birding/events/{event['id']}/disqualifications",
        json={"participant_id": "ID0000031", "reason": "重复报名其他场次"},
        headers=identities["organizer"],
    )
    assert disq.status_code == 200
    assert disq.json()["status"] == "ineligible"

    cancel = action(client, identities["volunteer"], event["id"], "ID0000030", "cancel")
    assert cancel.status_code == 200
    assert cancel.json()["promoted"] == ["ID0000032"]

    promoted = client.get(
        f"/api/birding/events/{event['id']}/registrations?status=offered",
        headers=identities["organizer"],
    ).json()["items"]
    assert [item["participant_id"] for item in promoted] == ["ID0000032"]

    # 重复取消幂等。
    again = action(client, identities["volunteer"], event["id"], "ID0000030", "cancel")
    assert again.status_code == 200 and again.json()["status"] == "cancelled"

    # 恢复资格后回到候补队尾，场次有名额时可被提升。
    requalify = client.post(
        f"/api/birding/events/{event['id']}/signups/ID0000031/requalify",
        headers=identities["organizer"],
    )
    assert requalify.status_code == 200
    assert requalify.json()["status"] == "waitlisted"


def test_disqualified_holder_frees_seat_for_waitlist(client, identities):
    event = create_event(client, identities["organizer"], capacity=1)
    signup(client, identities["volunteer"], event["id"], "ID0000040")
    signup(client, identities["volunteer2"], event["id"], "ID0000041")
    disq = client.post(
        f"/api/birding/events/{event['id']}/disqualifications",
        json={"participant_id": "ID0000040", "reason": "身份信息核验未通过"},
        headers=identities["organizer"],
    )
    assert disq.json()["promoted"] == ["ID0000041"]
    detail = client.get(f"/api/birding/events/{event['id']}", headers=identities["organizer"]).json()
    assert detail["occupied"] == 1


# ------------------------------------------------------------- 通知失败重试


def test_notification_failure_retries_without_duplicate_seat(client, identities, admin):
    frozen = FrozenClock(datetime(2026, 9, 30, 9, 0, tzinfo=UTC))
    runtime.set_clock(frozen)
    event = create_event(client, identities["organizer"], capacity=1, timeout=30)
    signup(client, identities["volunteer"], event["id"], "ID0000050", contact="13900000050")  # offered
    signup(client, identities["volunteer2"], event["id"], "ID0000051", contact="13900000051")  # waitlisted

    # 候补者的短信渠道故障（仅影响投递，不影响占位资格）。
    block = client.post(
        "/api/birding/notifications/delivery-blocks",
        json={"contact": "13900000051", "reason": "运营商网关错误"},
        headers=admin["headers"],
    )
    assert block.status_code == 200

    cancel = action(client, identities["volunteer"], event["id"], "ID0000050", "cancel")
    assert cancel.json()["promoted"] == ["ID0000051"]  # 占位成功，与通知解耦

    notifications_before = client.get(
        f"/api/birding/notifications?event_id={event['id']}", headers=admin["headers"]
    ).json()["items"]
    pending_promotions = [n for n in notifications_before if n["kind"] == "promotion"]
    assert len(pending_promotions) == 1
    notification_id = pending_promotions[0]["id"]

    # 首轮：取消者的 offer 通知已作废，候补者的 promotion 投递失败进入退避重试。
    first_run = client.post("/api/birding/notifications/process", headers=admin["headers"]).json()
    assert first_run["failed"] == [notification_id]
    assert first_run["sent"] == []

    notifications = client.get(
        f"/api/birding/notifications?event_id={event['id']}", headers=admin["headers"]
    ).json()["items"]
    promotion = [n for n in notifications if n["id"] == notification_id][0]
    assert promotion["status"] == "failed"
    assert promotion["attempts"] == 1

    # 退避窗口内重试不会被拾取。
    immediate = client.post("/api/birding/notifications/process", headers=admin["headers"]).json()
    assert immediate == {"sent": [], "failed": []}

    # 超过退避时间仍未恢复：继续在同一条记录上累加尝试次数。
    frozen.advance(seconds=10)
    second_run = client.post("/api/birding/notifications/process", headers=admin["headers"]).json()
    assert second_run["failed"] == [notification_id]

    # 渠道恢复后重试成功，仍然是同一条通知；候补转正的占位始终只有一个。
    client.delete("/api/birding/notifications/delivery-blocks/13900000051", headers=admin["headers"])
    frozen.advance(minutes=10)
    third_run = client.post("/api/birding/notifications/process", headers=admin["headers"]).json()
    assert third_run["sent"] == [notification_id]

    notifications_after = client.get(
        f"/api/birding/notifications?event_id={event['id']}", headers=admin["headers"]
    ).json()["items"]
    promotion_after = [n for n in notifications_after if n["id"] == notification_id]
    assert len(promotion_after) == 1 and promotion_after[0]["status"] == "sent"

    registrations = client.get(
        f"/api/birding/events/{event['id']}/registrations", headers=identities["organizer"]
    ).json()["items"]
    target = [r for r in registrations if r["participant_id"] == "ID0000051"]
    assert len(target) == 1 and target[0]["status"] == "offered"


# ------------------------------------------------------------- 活动关闭


def test_event_close_freezes_signups_and_promotions(client, identities, admin):
    frozen = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    runtime.set_clock(frozen)
    event = create_event(client, identities["organizer"], capacity=1, timeout=30)
    signup(client, identities["volunteer"], event["id"], "ID0000060")
    signup(client, identities["volunteer2"], event["id"], "ID0000061")

    close = client.post(f"/api/birding/events/{event['id']}/close", headers=identities["organizer"])
    assert close.status_code == 200
    assert close.json()["status"] == "closed"

    # 关闭后不能再报名。
    late = signup(client, identities["plain"], event["id"], "ID0000062")
    assert late.status_code == 409
    # 重复关闭幂等冲突。
    assert client.post(f"/api/birding/events/{event['id']}/close", headers=identities["organizer"]).status_code == 409

    # 关闭后即便未确认名额逾期，也不再向候补递补。
    frozen.advance(minutes=60)
    run = client.post("/api/birding/expirations/run", headers=admin["headers"]).json()
    assert run["promoted"] == []
    waiter = client.get(
        f"/api/birding/events/{event['id']}/registrations?status=waitlisted",
        headers=identities["organizer"],
    ).json()["items"]
    assert [item["participant_id"] for item in waiter] == ["ID0000061"]


# ------------------------------------------------------------- 权限隔离


def test_permission_isolation_between_teams(client, identities, admin):
    event = create_event(client, identities["organizer"], code="TEAM-01", capacity=2)

    # 志愿者不能发布场次；未登录不能访问。
    forbidden = client.post(
        "/api/birding/events",
        json={"code": "X1", "title": "x", "capacity": 1, "confirm_timeout_minutes": 10},
        headers=identities["volunteer"],
    )
    assert forbidden.status_code == 403
    assert client.get("/api/birding/events").status_code == 401

    # 非属主组织方不能关闭、查看名单、取消资格或看审计。
    other = identities["organizer_b"]
    assert client.post(f"/api/birding/events/{event['id']}/close", headers=other).status_code == 403
    assert client.get(f"/api/birding/events/{event['id']}/registrations", headers=other).status_code == 403
    assert client.get(f"/api/birding/events/{event['id']}/waitlist", headers=other).status_code == 403
    assert client.get(f"/api/birding/events/{event['id']}/audit", headers=other).status_code == 403
    disq = client.post(
        f"/api/birding/events/{event['id']}/disqualifications",
        json={"participant_id": "ID0000099", "reason": "x"},
        headers=other,
    )
    assert disq.status_code == 403

    # 组织方不能执行全局运营操作（超时回收、通知投递）。
    assert client.post("/api/birding/expirations/run", headers=other).status_code == 403
    assert client.post("/api/birding/notifications/process", headers=other).status_code == 403
    assert client.get("/api/birding/notifications", headers=other).status_code == 403

    # 属主可以管理自己的场次；管理员可以跨团队管理。
    assert client.get(f"/api/birding/events/{event['id']}/audit", headers=identities["organizer"]).status_code == 200
    assert client.post("/api/birding/expirations/run", headers=admin["headers"]).status_code == 200


# ------------------------------------------------------------- 审计还原


def test_audit_trail_reconstructs_every_state_change(client, identities):
    event = create_event(client, identities["organizer"], capacity=1, timeout=30)
    eid = event["id"]
    signup(client, identities["volunteer"], eid, "ID0000070")
    signup(client, identities["volunteer2"], eid, "ID0000071")
    action(client, identities["volunteer"], eid, "ID0000070", "cancel")
    action(client, identities["volunteer2"], eid, "ID0000071", "confirm")

    trail = client.get(f"/api/birding/events/{eid}/audit", headers=identities["organizer"]).json()["items"]
    actions = [entry["action"] for entry in trail]
    assert actions == [
        "event.create",
        "signup.offer",
        "signup.waitlist",
        "registration.cancel",
        "waitlist.promote",
        "registration.confirm",
    ]
    promote = next(entry for entry in trail if entry["action"] == "waitlist.promote")
    assert promote["before"]["status"] == "waitlisted"
    assert promote["after"]["status"] == "offered"
    assert promote["registration_id"]

    # 每条状态变化都带 before/after，可按 id 顺序回放还原。
    state_changes = [e for e in trail if e["action"] != "event.create"]
    assert all("after" in entry and isinstance(entry["after"], dict) for entry in state_changes)
