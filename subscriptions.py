"""乘客订阅资料：起终点、常用服务时间窗口、是否只能走无障碍路线。

订阅资料是影响判断的输入；任何订阅写入都会让尚未发布版本的分析结果失效，
已发布版本的冻结通知不在此模块处理（见 notifications 模块）。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from domain import DomainError, utcnow
import notifications as notification_service

if TYPE_CHECKING:
    from app import Database

SUBSCRIPTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS passenger_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passenger_name TEXT NOT NULL,
    from_stop_id INTEGER NOT NULL REFERENCES stops(id),
    to_stop_id INTEGER NOT NULL REFERENCES stops(id),
    service_start_minute INTEGER NOT NULL
        CHECK(service_start_minute >= 0 AND service_start_minute < 2880),
    service_end_minute INTEGER NOT NULL
        CHECK(service_end_minute > service_start_minute AND service_end_minute <= 2880),
    accessible_only INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

MAX_SERVICE_MINUTE = 2880


class SubscriptionStore:
    def __init__(self, db: "Database") -> None:
        self.db = db

    def create(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("只有调度编辑可以登记乘客订阅", 403)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            values = self._validate(conn, payload)
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO passenger_subscriptions(passenger_name,from_stop_id,to_stop_id,
                       service_start_minute,service_end_minute,accessible_only,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (values["passenger_name"], values["from_stop_id"], values["to_stop_id"],
                 values["service_start_minute"], values["service_end_minute"], values["accessible_only"], actor, now, now),
            )
            subscription_id = int(cur.lastrowid)
            # 新订阅可能影响所有草稿版本，旧的分析结论一律作废。
            notification_service.prepare_for_subscription_change(conn)
            self.db._audit(conn, actor, "subscription.created", "subscription", subscription_id, values)
            return self._row(conn, subscription_id)

    def update(self, subscription_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("只有调度编辑可以修改乘客订阅", 403)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM passenger_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not current:
                raise DomainError("乘客订阅不存在", 404)
            merged = {
                "passenger_name": payload.get("passenger_name", current["passenger_name"]),
                "from_stop_id": payload.get("from_stop_id", current["from_stop_id"]),
                "to_stop_id": payload.get("to_stop_id", current["to_stop_id"]),
                "service_start_minute": payload.get("service_start_minute", current["service_start_minute"]),
                "service_end_minute": payload.get("service_end_minute", current["service_end_minute"]),
                "accessible_only": payload.get("accessible_only", bool(current["accessible_only"])),
            }
            values = self._validate(conn, merged)
            conn.execute(
                """UPDATE passenger_subscriptions SET passenger_name=?,from_stop_id=?,to_stop_id=?,
                       service_start_minute=?,service_end_minute=?,accessible_only=?,updated_at=? WHERE id=?""",
                (values["passenger_name"], values["from_stop_id"], values["to_stop_id"],
                 values["service_start_minute"], values["service_end_minute"], values["accessible_only"],
                 utcnow(), subscription_id),
            )
            notification_service.prepare_for_subscription_change(conn)
            self.db._audit(conn, actor, "subscription.updated", "subscription", subscription_id, values)
            return self._row(conn, subscription_id)

    def delete(self, subscription_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("只有调度编辑可以删除乘客订阅", 403)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM passenger_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not current:
                raise DomainError("乘客订阅不存在", 404)
            notification_service.prepare_for_subscription_change(conn)
            try:
                conn.execute("DELETE FROM passenger_subscriptions WHERE id=?", (subscription_id,))
            except sqlite3.IntegrityError as exc:
                # 已发布版本的冻结名单保留该乘客的快照，不能删除。
                raise DomainError("该订阅已被发布版本的冻结通知引用，不能删除", 409) from exc
            self.db._audit(conn, actor, "subscription.deleted", "subscription", subscription_id,
                           {"passenger_name": current["passenger_name"]})
            return {"deleted": subscription_id}

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [self._serialize(r) for r in conn.execute("SELECT * FROM passenger_subscriptions ORDER BY id")]

    def _validate(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("passenger_name", "")).strip()
        if not name:
            raise DomainError("乘客称呼不能为空")
        try:
            from_stop_id = int(payload.get("from_stop_id"))
            to_stop_id = int(payload.get("to_stop_id"))
            start = int(payload.get("service_start_minute"))
            end = int(payload.get("service_end_minute"))
        except (TypeError, ValueError) as exc:
            raise DomainError("起终点和服务时间分钟数必须是整数") from exc
        if from_stop_id == to_stop_id:
            raise DomainError("起点和终点不能相同")
        for stop_id in (from_stop_id, to_stop_id):
            if not conn.execute("SELECT 1 FROM stops WHERE id=?", (stop_id,)).fetchone():
                raise DomainError("起讫站点不存在", 404)
        if start < 0 or start >= MAX_SERVICE_MINUTE or end <= start or end > MAX_SERVICE_MINUTE:
            raise DomainError(f"常用服务时间窗口必须满足 0 <= 开始 < 结束 <= {MAX_SERVICE_MINUTE}")
        accessible_only = payload.get("accessible_only", False)
        if not isinstance(accessible_only, bool):
            raise DomainError("accessible_only 必须是布尔值")
        return {
            "passenger_name": name,
            "from_stop_id": from_stop_id,
            "to_stop_id": to_stop_id,
            "service_start_minute": start,
            "service_end_minute": end,
            "accessible_only": accessible_only,
        }

    def _row(self, conn: sqlite3.Connection, subscription_id: int) -> dict[str, Any]:
        return self._serialize(conn.execute("SELECT * FROM passenger_subscriptions WHERE id=?", (subscription_id,)).fetchone())

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "passenger_name": row["passenger_name"],
            "from_stop_id": row["from_stop_id"],
            "to_stop_id": row["to_stop_id"],
            "service_start_minute": row["service_start_minute"],
            "service_end_minute": row["service_end_minute"],
            "accessible_only": bool(row["accessible_only"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
