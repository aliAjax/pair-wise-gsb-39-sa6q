"""乘客影响通知名单的保存与冻结（影响通知台第三层）。

- evaluate：用 impacts 层算出的待通知条目覆盖该版本的当前名单（仅草稿可算）；
- confirm：调度员确认名单，确认后方案才允许发布；
- freeze：发布事务内把名单整体冻结成快照，之后基础数据或订阅再变也不动；
- get_for_version：未发布读实时名单，已发布读冻结快照。

新版本必须重新评估生成自己的名单；名单不随版本复制。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import impacts
from errors import DomainError, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','frozen')),
    created_by TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    frozen_at TEXT,
    generated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    frozen_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS notification_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES notification_lists(id) ON DELETE CASCADE,
    subscription_id INTEGER,
    passenger_name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    origin_stop_id INTEGER,
    destination_stop_id INTEGER,
    service_start_minute INTEGER,
    service_end_minute INTEGER,
    require_accessible INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL CHECK(reason IN ('broken','detour_delay','accessibility')),
    reason_detail TEXT NOT NULL,
    at_minute INTEGER,
    baseline_minutes INTEGER,
    plan_minutes INTEGER,
    extra_minutes INTEGER,
    alternative_route TEXT,
    created_at TEXT NOT NULL
);
"""

EDITOR_ROLES = {"planner", "editor", "admin"}


class NotificationRepository:
    def create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    # -- 生成待通知名单 -----------------------------------------------------

    def evaluate(self, db: Any, version_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in EDITOR_ROLES:
            raise DomainError("只有调度人员可以生成待通知名单", 403)
        with db.connect() as conn:
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            if version["status"] not in {"draft"}:
                raise DomainError("只有草稿版本可以重新评估影响名单", 409)
            changes = [dict(r) for r in conn.execute(
                "SELECT * FROM changes WHERE version_id=?", (version_id,))]
            subscriptions = db.subscriptions.list_active(conn)

        # 路径计算可能另开读连接，放在写事务之外，避免读写互锁。
        stops = {int(r["id"]): dict(r) for r in db._query_stops()}
        lines = {int(r["id"]): dict(r) for r in db._query_lines()}
        baseline_router = lambda o, d, m, a: db.route(o, d, None, m, a)
        plan_router = lambda o, d, m, a: db.route(o, d, version_id, m, a)
        entries = impacts.build_notification_entries(
            baseline_router, plan_router, subscriptions, changes, stops, lines)

        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            row = conn.execute("SELECT id,status FROM notification_lists WHERE version_id=?",
                               (version_id,)).fetchone()
            if row:
                if row["status"] == "frozen":
                    raise DomainError("名单已随版本冻结，不能重新评估", 409)
                list_id = int(row["id"])
                conn.execute(
                    """UPDATE notification_lists SET status='pending',created_by=?,confirmed_by=NULL,
                       confirmed_at=NULL,frozen_at=NULL,frozen_snapshot=NULL,generated_at=?,updated_at=?
                       WHERE id=?""",
                    (actor, now, now, list_id))
                conn.execute("DELETE FROM notification_entries WHERE list_id=?", (list_id,))
            else:
                cur = conn.execute(
                    """INSERT INTO notification_lists(version_id,status,created_by,generated_at,updated_at)
                       VALUES(?,'pending',?,?,?)""",
                    (version_id, actor, now, now))
                list_id = int(cur.lastrowid)
            for entry in entries:
                self._insert_entry(conn, list_id, entry, now)
            db._audit(conn, actor, "notification.evaluated", "version", version_id,
                      {"list_id": list_id, "affected": len(entries)})
            return self.get_for_version_with_conn(conn, version_id)

    # -- 调度员确认 ---------------------------------------------------------

    def confirm(self, db: Any, version_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in EDITOR_ROLES:
            raise DomainError("只有调度人员可以确认待通知名单", 403)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT status FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            if version["status"] == "published":
                raise DomainError("方案已发布，名单已冻结", 409)
            row = conn.execute("SELECT * FROM notification_lists WHERE version_id=?",
                               (version_id,)).fetchone()
            if not row:
                raise DomainError("请先生成待通知名单再确认", 409)
            if row["status"] == "frozen":
                raise DomainError("名单已冻结，不能重复确认", 409)
            now = utcnow()
            conn.execute(
                "UPDATE notification_lists SET status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
                (actor, now, now, int(row["id"])))
            affected = conn.execute(
                "SELECT COUNT(*) c FROM notification_entries WHERE list_id=?", (int(row["id"]),)).fetchone()["c"]
            db._audit(conn, actor, "notification.confirmed", "version", version_id,
                      {"list_id": int(row["id"]), "affected": int(affected)})
            return self.get_for_version_with_conn(conn, version_id)

    # -- 发布冻结（与发布在同一事务内） -------------------------------------

    def freeze_for_publish(self, db: Any, conn: sqlite3.Connection,
                           version: sqlite3.Row, actor: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM notification_lists WHERE version_id=?",
                           (version["id"],)).fetchone()
        if not row:
            raise DomainError("发布前必须先生成并确认乘客影响通知名单", 409)
        if row["status"] != "confirmed":
            raise DomainError("待通知名单未经调度员确认，不能发布方案", 409)
        entries = self._read_entries(conn, int(row["id"]))
        snapshot = {
            "version_id": int(version["id"]),
            "disruption_id": int(version["disruption_id"]),
            "version_no": int(version["version_no"]),
            "frozen_by": actor,
            "frozen_at": utcnow(),
            "status": "frozen",
            "affected_count": len(entries),
            "entries": entries,
        }
        conn.execute(
            "UPDATE notification_lists SET status='frozen',frozen_at=?,frozen_snapshot=?,updated_at=? WHERE id=?",
            (snapshot["frozen_at"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
             snapshot["frozen_at"], int(row["id"])))
        db._audit(conn, actor, "notification.frozen", "version", int(version["id"]),
                  {"list_id": int(row["id"]), "affected": len(entries)})
        return snapshot

    # -- 查询 ---------------------------------------------------------------

    def get_for_version(self, db: Any, version_id: int) -> dict[str, Any]:
        with db.connect() as conn:
            version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            return self.get_for_version_with_conn(conn, version_id)

    def get_for_version_with_conn(self, conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM notification_lists WHERE version_id=?",
                           (version_id,)).fetchone()
        if not row:
            return {"version_id": version_id, "status": "missing",
                    "message": "尚未为该版本生成待通知名单", "affected_count": 0, "entries": []}
        if row["status"] == "frozen" and row["frozen_snapshot"]:
            return json.loads(row["frozen_snapshot"])
        return {
            "version_id": version_id,
            "status": row["status"],
            "created_by": row["created_by"],
            "confirmed_by": row["confirmed_by"],
            "confirmed_at": row["confirmed_at"],
            "frozen_at": row["frozen_at"],
            "generated_at": row["generated_at"],
            "updated_at": row["updated_at"],
            "affected_count": conn.execute(
                "SELECT COUNT(*) c FROM notification_entries WHERE list_id=?", (int(row["id"]),)).fetchone()["c"],
            "entries": self._read_entries(conn, int(row["id"])),
        }

    # -- 内部辅助 -----------------------------------------------------------

    def _read_entries(self, conn: sqlite3.Connection, list_id: int) -> list[dict[str, Any]]:
        result = []
        for row in conn.execute(
                "SELECT * FROM notification_entries WHERE list_id=? ORDER BY id", (list_id,)):
            item = dict(row)
            item["require_accessible"] = bool(item["require_accessible"])
            item["alternative_route"] = json.loads(item["alternative_route"]) if item["alternative_route"] else None
            result.append(item)
        return result

    def _insert_entry(self, conn: sqlite3.Connection, list_id: int,
                      entry: dict[str, Any], now: str) -> None:
        conn.execute(
            """INSERT INTO notification_entries(list_id,subscription_id,passenger_name,contact,
               origin_stop_id,destination_stop_id,service_start_minute,service_end_minute,
               require_accessible,reason,reason_detail,at_minute,baseline_minutes,plan_minutes,
               extra_minutes,alternative_route,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (list_id, entry["subscription_id"], entry["passenger_name"], entry.get("contact", ""),
             entry["origin_stop_id"], entry["destination_stop_id"],
             entry["service_start_minute"], entry["service_end_minute"],
             int(entry["require_accessible"]), entry["reason"], entry["reason_detail"],
             entry["at_minute"], entry["baseline_minutes"], entry["plan_minutes"],
             entry["extra_minutes"],
             json.dumps(entry["alternative_route"], ensure_ascii=False, sort_keys=True)
             if entry["alternative_route"] is not None else None, now),
        )
