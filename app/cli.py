from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_birding_demo() -> int:
    """通过 HTTP 接口演示观鸟活动的建场、并发报名、候补转正与活动关闭。"""
    from datetime import UTC, datetime, timedelta

    password = "Admin!23456"
    with TestClient(app) as client:
        client.post("/api/auth/bootstrap", json={"username": "admin", "password": password, "client_label": "cli"})
        login = client.post("/api/auth/login", json={"username": "admin", "password": password, "client_label": "cli"})
        if login.status_code != 200:
            print(json.dumps({"status": "skipped", "reason": "本地管理员口令与演示口令不一致"}, ensure_ascii=False))
            return 0
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        now = datetime.now(UTC)
        event = {
            "title": "CLI 观鸟导赏演示", "location": "野鸭湖", "leader": "飞羽志愿者",
            "start_at": (now + timedelta(days=7)).isoformat(),
            "capacity": 2, "waitlist_capacity": 5, "confirm_timeout_minutes": 30,
            "registration_opens_at": (now - timedelta(hours=1)).isoformat(),
            "registration_closes_at": (now + timedelta(days=1)).isoformat(),
        }
        created = client.post("/api/birding/events", json=event, headers=headers)
        if created.status_code != 201:
            print(created.text)
            return 1
        event_id = created.json()["id"]
        registered = []
        for index in range(4):
            response = client.post(
                f"/api/birding/events/{event_id}/registrations",
                json={"applicant": f"demo-{index}", "applicant_name": f"演示访客{index}",
                      "idempotency_key": f"demo-key-{index}"},
            )
            registered.append(response.json())
        # 第一位中签者取消，触发最早候补转正
        holder = next(item for item in registered if item["status"] == "offered")
        client.post(f"/api/birding/registrations/{holder['id']}/cancel?applicant={holder['applicant']}", json={})
        summary = client.get(f"/api/birding/events/{event_id}").json()
        result = {"event_id": event_id, "status_counts": summary["status_counts"], "occupied": summary["occupied"]}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if summary["occupied"] == 2 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("birding-demo", help="执行观鸟活动报名与候补流转演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "birding-demo": command_birding_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
