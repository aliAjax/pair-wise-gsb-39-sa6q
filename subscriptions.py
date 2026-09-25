"""乘客订阅资料（影响通知台第一层）。

只负责乘客常用行程的登记、校验、查询与停用，不做任何影响判断：
- 起讫站点：origin_stop_id / destination_stop_id
- 常用服务时间：service_start_minute ~ service_end_minute（服务日零点起算）
- 是否只能走无障碍路线：require_accessible
"""
from __future__ import annotations

import sqlite3
from typing import Any

from errors import DomainError, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS passengers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passenger_id INTEGER NOT NULL REFERENCES passengers(id) ON DELETE CASCADE,
    origin_stop_id INTEGER NOT NULL REFERENCES stops(id),
    destination_stop_id INTEGER NOT NULL REFERENCES stops(id),
    service_start_minute INTEGER NOT NULL CHECK(service_start_minute >= 0 AND service_start_minute < 2880),
    service_end_minute INTEGER NOT NULL CHECK(service_end_minute > service_start_minute AND service_end_minute <= 2880),
    require_accessible INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deactivated_at TEXT,
    CHECK(origin_stop_id <> destination_stop_id)
);
"""

WRITER_ROLES = {"planner", "editor", "admin"}


class SubscriptionRepository:
    def __init__(self, db: Any):
        # db 是 app.Database，仅通过它建立连接与读取站点/路径
        self.db = db

    def create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    def register(self, conn: sqlite3.Connection, actor: str, role: str,
                 payload: dict[str, Any], route_fn: Any) -> dict[str, Any]:
        """登记一个乘客的常用行程订阅。

        route_fn(from_id, to_id, at_minute, require_accessible) 用于确认该
        乘客在基线下确实存在可走路线；基线就接不上的行程不允许登记。
        """
        if role not in WRITER_ROLES:
            raise DomainError("只有调度人员可以登记乘客订阅", 403)
        name = str(payload.get("passenger_name", "")).strip()
        if not name:
            raise DomainError("乘客姓名不能为空")
        contact = str(payload.get("contact", "")).strip()
        try:
            origin = int(payload.get("origin_stop_id"))
            destination = int(payload.get("destination_stop_id"))
            start = int(payload.get("service_start_minute"))
            end = int(payload.get("service_end_minute"))
        except (TypeError, ValueError):
            raise DomainError("起终点和服务时间必须是整数")
        if origin == destination:
            raise DomainError("起点和终点不能相同")
        if not 0 <= start < 2880 or not start < end <= 2880:
            raise DomainError("常用服务时间窗必须落在服务日 0~2880 分钟内，且开始早于结束")
        require_accessible = bool(payload.get("require_accessible", False))
        for stop_id in (origin, destination):
            if not conn.execute("SELECT 1 FROM stops WHERE id=?", (stop_id,)).fetchone():
                raise DomainError(f"站点不存在: {stop_id}", 404)
        if require_accessible:
            origin_row = conn.execute("SELECT accessible FROM stops WHERE id=?", (origin,)).fetchone()
            if not origin_row["accessible"]:
                raise DomainError("起点不具备无障碍通行条件，无法登记无障碍订阅", 409)
        baseline = route_fn(origin, destination, start, require_accessible)
        if baseline.get("status") == "unreachable" or baseline.get("minutes") is None:
            raise DomainError("该起终点在基线下没有可走路线，不能登记订阅", 409)

        cur = conn.execute("INSERT INTO passengers(name,contact,created_at) VALUES(?,?,?)",
                           (name, contact, utcnow()))
        passenger_id = int(cur.lastrowid)
        cur = conn.execute(
            """INSERT INTO subscriptions(passenger_id,origin_stop_id,destination_stop_id,
               service_start_minute,service_end_minute,require_accessible,active,created_by,created_at)
               VALUES(?,?,?,?,?,?,1,?,?)""",
            (passenger_id, origin, destination, start, end, int(require_accessible), actor, utcnow()),
        )
        subscription_id = int(cur.lastrowid)
        return self._detail(conn, subscription_id)

    def deactivate(self, conn: sqlite3.Connection, actor: str, role: str, subscription_id: int) -> dict[str, Any]:
        if role not in WRITER_ROLES:
            raise DomainError("只有调度人员可以停用订阅", 403)
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
        if not row:
            raise DomainError("订阅不存在", 404)
        if not row["active"]:
            return self._detail(conn, subscription_id)
        conn.execute("UPDATE subscriptions SET active=0,deactivated_at=? WHERE id=?",
                     (utcnow(), subscription_id))
        return self._detail(conn, subscription_id)

    def list_active(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [self._decorate(conn, dict(r))
                for r in conn.execute(
                    "SELECT * FROM subscriptions WHERE active=1 ORDER BY id")]

    def list_all(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [self._decorate(conn, dict(r))
                for r in conn.execute("SELECT * FROM subscriptions ORDER BY id")]

    def _detail(self, conn: sqlite3.Connection, subscription_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
        return self._decorate(conn, dict(row))

    def _decorate(self, conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
        passenger = conn.execute("SELECT * FROM passengers WHERE id=?",
                                 (item["passenger_id"],)).fetchone()
        item["passenger"] = {"id": passenger["id"], "name": passenger["name"], "contact": passenger["contact"]}
        item["require_accessible"] = bool(item["require_accessible"])
        item["active"] = bool(item["active"])
        item["service_window"] = {
            "start_minute": item["service_start_minute"],
            "end_minute": item["service_end_minute"],
            "start_clock": _clock(item["service_start_minute"]),
            "end_clock": _clock(item["service_end_minute"]),
        }
        return item


def _clock(minutes: int) -> str:
    day_offset, minute_of_day = divmod(minutes, 1440)
    text = f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
    return f"{text}(+{day_offset}日)" if day_offset else text
