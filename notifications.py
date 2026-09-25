"""乘客影响通知名单的保存、确认与发布冻结。

- 影响判断（impact 模块）的结果通过 save_analysis 落库为 impact_runs + impact_notifications；
- 调度员逐条或整体确认后版本才允许发布；
- 发布在同一事务内把名单（含乘客资料和替代路线快照）冻结；
- 已发布版本的通知只读，基础数据或订阅之后再改都不影响它；
- 订阅资料任何写入都会调用 reset_draft_runs，清掉草稿版本的过时结论，新版本重新算。
"""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from domain import DomainError, canonical, utcnow

if TYPE_CHECKING:
    from app import Database

NOTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS impact_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed')),
    total_count INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    analyzed_by TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    confirmed_at TEXT,
    frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS impact_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES impact_runs(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES passenger_subscriptions(id),
    reason_code TEXT NOT NULL CHECK(reason_code IN ('route_broken','detour_delay','accessibility_lost')),
    reason TEXT NOT NULL,
    baseline_minutes INTEGER,
    plan_minutes INTEGER,
    extra_minutes INTEGER,
    evaluated_at_minute INTEGER NOT NULL,
    alternative TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    subscription_snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','frozen')),
    confirmed_by TEXT,
    confirmed_at TEXT,
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_version ON impact_notifications(version_id);
"""

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_FROZEN = "frozen"
RUN_PENDING = "pending"
RUN_CONFIRMED = "confirmed"


def reset_draft_runs(conn: sqlite3.Connection) -> None:
    """订阅资料变化后，删除所有草稿版本的分析运行和待通知名单。"""
    draft_run_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT r.id FROM impact_runs r JOIN versions v ON v.id=r.version_id WHERE v.status = 'draft'"
        )
    ]
    if not draft_run_ids:
        return
    conn.executemany("DELETE FROM impact_notifications WHERE run_id=?", [(rid,) for rid in draft_run_ids])
    conn.executemany("DELETE FROM impact_runs WHERE id=?", [(rid,) for rid in draft_run_ids])


def reset_run_for_version(conn: sqlite3.Connection, version_id: int) -> None:
    """草稿方案内容变化后作废该版本的分析结论；已发布版本的冻结名单不受影响。"""
    version = conn.execute("SELECT status FROM versions WHERE id=?", (version_id,)).fetchone()
    if not version or version["status"] != "draft":
        return
    run = conn.execute("SELECT id FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
    if not run:
        return
    conn.execute("DELETE FROM impact_notifications WHERE run_id=?", (run["id"],))
    conn.execute("DELETE FROM impact_runs WHERE id=?", (run["id"],))


def prepare_for_subscription_change(conn: sqlite3.Connection) -> None:
    """订阅写入前调用：已提交复核/已批准但未发布的版本名单不允许被静默写脏。"""
    locked = conn.execute(
        """SELECT v.id FROM impact_runs r JOIN versions v ON v.id=r.version_id
           WHERE v.status IN ('review','approved')"""
    ).fetchall()
    if locked:
        raise DomainError(
            "存在已提交复核或已批准但尚未发布的方案版本，其通知名单会随订阅资料变化失效，请先将版本退回草稿",
            409,
        )
    reset_draft_runs(conn)


def save_analysis(conn: sqlite3.Connection, version_id: int, actor: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """替换某版本当前的分析结论（调用方保证版本是草稿，且已在事务内）。"""
    existing = conn.execute("SELECT id FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
    if existing:
        conn.execute("DELETE FROM impact_notifications WHERE run_id=?", (existing["id"],))
        conn.execute("DELETE FROM impact_runs WHERE id=?", (existing["id"],))
    now = utcnow()
    run = conn.execute(
        "INSERT INTO impact_runs(version_id,status,total_count,confirmed_count,analyzed_by,analyzed_at) VALUES(?,?,?,?,?,?)",
        (version_id, RUN_PENDING, len(items), 0, actor, now),
    )
    run_id = int(run.lastrowid)
    for item in items:
        conn.execute(
            """INSERT INTO impact_notifications(run_id,version_id,subscription_id,reason_code,reason,
                   baseline_minutes,plan_minutes,extra_minutes,evaluated_at_minute,alternative,details,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, version_id, item["subscription_id"], item["reason_code"], item["reason"],
             item.get("baseline_minutes"), item.get("plan_minutes"), item.get("extra_minutes"),
             item["evaluated_at_minute"],
             canonical(item["alternative"]) if item.get("alternative") is not None else None,
             canonical(item.get("details", {})), STATUS_PENDING, now),
        )
    return {"run_id": run_id, "total": len(items)}


def _load_notifications(conn: sqlite3.Connection, run: sqlite3.Row) -> list[dict[str, Any]]:
    items = []
    for row in conn.execute("SELECT * FROM impact_notifications WHERE run_id=? ORDER BY id", (run["id"],)):
        item = _serialize_notification(row)
        if item.get("subscription_snapshot"):
            item["subscription"] = item["subscription_snapshot"]
        else:
            sub = conn.execute("SELECT * FROM passenger_subscriptions WHERE id=?", (row["subscription_id"],)).fetchone()
            item["subscription"] = {
                "id": row["subscription_id"],
                "passenger_name": sub["passenger_name"],
                "from_stop_id": sub["from_stop_id"],
                "to_stop_id": sub["to_stop_id"],
                "service_start_minute": sub["service_start_minute"],
                "service_end_minute": sub["service_end_minute"],
                "accessible_only": bool(sub["accessible_only"]),
            } if sub else {"id": row["subscription_id"]}
        items.append(item)
    return items


def list_for_version(db: "Database", version_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        version = conn.execute("SELECT id,status FROM versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            raise DomainError("方案版本不存在", 404)
        run = conn.execute("SELECT * FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
        if not run:
            return {"version_id": version_id, "analyzed": False, "status": "not_analyzed",
                    "total_count": 0, "confirmed_count": 0, "items": []}
        result = _serialize_run(run)
        result["version_id"] = version_id
        result["analyzed"] = True
        result["items"] = _load_notifications(conn, run)
        return result


def get_notification(db: "Database", notification_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM impact_notifications WHERE id=?", (notification_id,)).fetchone()
        if not row:
            raise DomainError("通知记录不存在", 404)
        return _serialize_notification(row)


def confirm(db: "Database", version_id: int, actor: str, role: str,
            notification_id: int | None = None) -> dict[str, Any]:
    if role not in {"planner", "editor", "reviewer", "admin"}:
        raise DomainError("没有确认乘客影响名单的权限", 403)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("SELECT id,status FROM versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            raise DomainError("方案版本不存在", 404)
        run = conn.execute("SELECT * FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
        if not run:
            raise DomainError("该版本还没有分析过乘客影响", 409)
        if version["status"] == "published":
            raise DomainError("版本已发布，名单已冻结", 409)
        now = utcnow()
        if notification_id is not None:
            row = conn.execute(
                "SELECT * FROM impact_notifications WHERE run_id=? AND id=? AND status=?",
                (run["id"], notification_id, STATUS_PENDING),
            ).fetchone()
            if not row:
                raise DomainError("没有待确认的通知记录", 409)
            conn.execute("UPDATE impact_notifications SET status=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                         (STATUS_CONFIRMED, actor, now, row["id"]))
            count = 1
        else:
            # 整单确认：包括没有任何受影响乘客的空名单，调度员也要显式确认后才能发布。
            targets = conn.execute("SELECT id FROM impact_notifications WHERE run_id=? AND status=?",
                                   (run["id"], STATUS_PENDING)).fetchall()
            for target in targets:
                conn.execute("UPDATE impact_notifications SET status=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                             (STATUS_CONFIRMED, actor, now, target["id"]))
            count = len(targets)
        _refresh_run_totals(conn, run["id"])
        db._audit(conn, actor, "impact.confirmed", "version", version_id,
                  {"count": count, "notification_id": notification_id})
    # 事务提交后再用新连接读取，保证返回的是已提交状态。
    return list_for_version(db, version_id)


def assert_publishable(conn: sqlite3.Connection, version_id: int) -> None:
    """发布前置闸门：必须先分析并由调度员确认整份名单。"""
    run = conn.execute("SELECT * FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
    if not run:
        raise DomainError("发布前必须先分析乘客影响并确认通知名单", 409)
    if run["status"] != RUN_CONFIRMED:
        raise DomainError("乘客影响通知名单尚未全部确认，不能发布", 409)


def freeze_for_publish(conn: sqlite3.Connection, version_id: int) -> int:
    """发布事务内调用：固化乘客资料与替代路线快照，名单随版本冻结。返回冻结条数。"""
    run = conn.execute("SELECT * FROM impact_runs WHERE version_id=?", (version_id,)).fetchone()
    if not run or run["status"] != RUN_CONFIRMED:
        raise DomainError("乘客影响通知名单尚未全部确认，不能发布", 409)
    now = utcnow()
    rows = conn.execute("SELECT * FROM impact_notifications WHERE run_id=?", (run["id"],)).fetchall()
    for row in rows:
        sub = conn.execute("SELECT * FROM passenger_subscriptions WHERE id=?", (row["subscription_id"],)).fetchone()
        snapshot = None
        if sub:
            snapshot = canonical({
                "id": sub["id"],
                "passenger_name": sub["passenger_name"],
                "from_stop_id": sub["from_stop_id"],
                "to_stop_id": sub["to_stop_id"],
                "service_start_minute": sub["service_start_minute"],
                "service_end_minute": sub["service_end_minute"],
                "accessible_only": bool(sub["accessible_only"]),
            })
        conn.execute(
            "UPDATE impact_notifications SET status=?,subscription_snapshot=?,frozen_at=? WHERE id=?",
            (STATUS_FROZEN, snapshot, now, row["id"]),
        )
    conn.execute("UPDATE impact_runs SET status=?,frozen_at=? WHERE id=?", (RUN_CONFIRMED, now, run["id"]))
    return len(rows)


def _refresh_run_totals(conn: sqlite3.Connection, run_id: int) -> None:
    total = int(conn.execute("SELECT COUNT(*) c FROM impact_notifications WHERE run_id=?", (run_id,)).fetchone()["c"])
    confirmed = int(conn.execute(
        "SELECT COUNT(*) c FROM impact_notifications WHERE run_id=? AND status != ?", (run_id, STATUS_PENDING)
    ).fetchone()["c"])
    # 空名单（没有乘客受影响）在整单确认后同样进入 confirmed。
    status = RUN_CONFIRMED if confirmed == total else RUN_PENDING
    confirmed_at = utcnow() if status == RUN_CONFIRMED else None
    conn.execute("UPDATE impact_runs SET total_count=?,confirmed_count=?,status=?,confirmed_at=? WHERE id=?",
                 (total, confirmed, status, confirmed_at, run_id))


def _serialize_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["id"],
        "status": row["status"],
        "total_count": row["total_count"],
        "confirmed_count": row["confirmed_count"],
        "analyzed_by": row["analyzed_by"],
        "analyzed_at": row["analyzed_at"],
        "confirmed_at": row["confirmed_at"],
        "frozen_at": row["frozen_at"],
    }


def _serialize_notification(row: sqlite3.Row) -> dict[str, Any]:
    def loads(value: str | None) -> Any:
        return json.loads(value) if value else None

    return {
        "id": row["id"],
        "subscription_id": row["subscription_id"],
        "reason_code": row["reason_code"],
        "reason": row["reason"],
        "baseline_minutes": row["baseline_minutes"],
        "plan_minutes": row["plan_minutes"],
        "extra_minutes": row["extra_minutes"],
        "evaluated_at_minute": row["evaluated_at_minute"],
        "alternative": loads(row["alternative"]),
        "details": loads(row["details"]) or {},
        "status": row["status"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "frozen_at": row["frozen_at"],
        "subscription": None,
        "subscription_snapshot": loads(row["subscription_snapshot"]),
    }
