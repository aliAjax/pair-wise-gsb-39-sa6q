"""乘客影响判断：基线路线 vs 方案路线。

选定一个草稿版本后，对每位登记了常用行程的乘客，在其常用服务时间窗口内
（与变更生效窗口取交集）比较两种走法：

- route_broken：原来的走法在方案里断掉（不可达），尝试给一条可走的替代路线，
  实在接不上就保留原因，alternative 为空；
- detour_delay：仍然可达，但绕行多出超过 15 分钟（严格大于 15），给出方案走法作为替代；
- accessibility_lost：只能走无障碍路线的乘客，方案下无障碍条件不再满足，
  尝试给出一条仍满足无障碍条件的替代路线。

判断只对草稿版本进行；发布后的冻结名单见 notifications 模块。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from domain import DomainError
import notifications as notification_service

if TYPE_CHECKING:
    from app import Database

DETOUR_EXTRA_MINUTES_LIMIT = 15

ROUTE_BROKEN = "route_broken"
DETOUR_DELAY = "detour_delay"
ACCESSIBILITY_LOST = "accessibility_lost"

# 原因优先级：断网最严重，其次是无障碍条件，最后才是绕行耗时。
_REASON_PRIORITY = {ROUTE_BROKEN: 3, ACCESSIBILITY_LOST: 2, DETOUR_DELAY: 1}


class ImpactAnalyzer:
    def __init__(self, db: "Database") -> None:
        self.db = db

    def analyze_version(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        if role not in {"planner", "editor", "admin"}:
            raise DomainError("只有调度编辑可以分析乘客影响", 403)
        with self.db.connect() as conn:
            version = conn.execute("SELECT id,status FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("方案版本不存在", 404)
            if version["status"] != "draft":
                raise DomainError("只能对草稿版本分析乘客影响", 409)
            subscriptions = [dict(r) for r in conn.execute("SELECT * FROM passenger_subscriptions ORDER BY id")]
            change_windows = [
                (int(r["effective_start_minute"]), int(r["effective_end_minute"]))
                for r in conn.execute("SELECT effective_start_minute,effective_end_minute FROM changes WHERE version_id=?", (version_id,))
                if r["effective_start_minute"] is not None and r["effective_end_minute"] is not None
            ]
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for sub in subscriptions:
            outcome = self._evaluate(sub, version_id, change_windows)
            if outcome is None:
                continue
            payload, baseline_unreachable = outcome
            if baseline_unreachable:
                skipped.append(payload)
            else:
                results.append(payload)
        results.sort(key=lambda item: (-_REASON_PRIORITY[item["reason_code"]], item["subscription_id"]))
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = notification_service.save_analysis(conn, version_id, actor, results)
            self.db._audit(conn, actor, "impact.analyzed", "version", version_id,
                           {"notifications": len(results), "skipped": len(skipped)})
        response = notification_service.list_for_version(self.db, version_id)
        response["skipped"] = skipped
        response["run_id"] = saved["run_id"]
        return response

    def _candidate_minutes(self, sub: dict[str, Any], change_windows: list[tuple[int, int]]) -> list[int]:
        """在常用服务时间窗口内取若干代表性出发时刻。

        变更加了生效时间窗口时，只在变更窗口与乘客服务窗口的交集内取样；
        交集为空（变更时段不经过其常用时间）时返回空列表，该乘客不受影响。
        """
        start, end = int(sub["service_start_minute"]), int(sub["service_end_minute"])
        if change_windows:
            windows: list[tuple[int, int]] = []
            for w_start, w_end in change_windows:
                # 生效窗口含端点；乘客窗口结束分钟不含在内（最后可出发分钟为 end-1）。
                lo, hi = max(start, w_start), min(end, w_end + 1)
                if lo < hi:
                    windows.append((lo, hi))
            if not windows:
                return []
        else:
            windows = [(start, end)]
        candidates: set[int] = set()
        for w_start, w_end in windows:
            candidates.add(w_start)
            candidates.add(w_start + (w_end - 1 - w_start) // 2)
            candidates.add(w_end - 1)
        return sorted(candidates)

    def _evaluate(self, sub: dict[str, Any], version_id: int,
                  change_windows: list[tuple[int, int]]) -> tuple[dict[str, Any], bool] | None:
        """返回 (影响记录, 是否基线本就不通)；完全不受影响返回 None。"""
        minutes_to_check = self._candidate_minutes(sub, change_windows)
        if not minutes_to_check:
            return None
        worst: dict[str, Any] | None = None
        baseline_unreachable = False
        for minute in minutes_to_check:
            outcome, baseline_ok = self._evaluate_at(sub, version_id, minute)
            if not baseline_ok:
                baseline_unreachable = True
                continue
            if outcome is not None and (worst is None or self._worse(outcome, worst)):
                worst = outcome
        if worst is not None:
            return worst, False
        if baseline_unreachable:
            return {
                "skipped": True,
                "subscription_id": sub["id"],
                "passenger_name": sub["passenger_name"],
                "reason": "基线下该常用行程本就无法通行，不纳入方案影响判断",
            }, True
        return None

    def _worse(self, candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        rank_a, rank_b = _REASON_PRIORITY[candidate["reason_code"]], _REASON_PRIORITY[current["reason_code"]]
        if rank_a != rank_b:
            return rank_a > rank_b
        return (candidate.get("extra_minutes") or 0) > (current.get("extra_minutes") or 0)

    def _evaluate_at(self, sub: dict[str, Any], version_id: int, minute: int) -> tuple[dict[str, Any] | None, bool]:
        """返回 (影响记录或 None, 基线在该时刻是否可达)。"""
        from_stop, to_stop = int(sub["from_stop_id"]), int(sub["to_stop_id"])
        accessible_only = bool(sub["accessible_only"])
        try:
            baseline = self.db.route(from_stop, to_stop, None, minute, require_accessible=accessible_only)
        except DomainError:
            # 基线不满足无障碍前置条件（如起点本就不无障碍），不是方案造成的。
            return None, False
        if baseline["status"] != "ok":
            return None, False
        common = {
            "subscription_id": sub["id"],
            "passenger_name": sub["passenger_name"],
            "accessible_only": accessible_only,
            "service_window": {
                "start_minute": int(sub["service_start_minute"]),
                "end_minute": int(sub["service_end_minute"]),
            },
            "evaluated_at_minute": minute,
        }
        baseline_error = None
        try:
            plan = self.db.route(from_stop, to_stop, version_id, minute, require_accessible=accessible_only)
        except DomainError as exc:
            plan = {"status": "unreachable", "path": [], "legs": [], "minutes": None}
            baseline_error = str(exc)
        if plan["status"] != "ok":
            reason_code, reason = ROUTE_BROKEN, "原来的走法在方案中已中断"
            alternative = None
            if accessible_only:
                relaxed = self.db.route(from_stop, to_stop, version_id, minute, require_accessible=False)
                if relaxed["status"] == "ok":
                    # 普通路线还在，但方案下已没有无障碍走法；同一张无障碍图找不到替代，保留原因。
                    reason_code, reason = ACCESSIBILITY_LOST, "方案下原有路线不再满足无障碍通行条件，且暂无无障碍替代路线"
            baseline_route = self._describe_route(baseline, "baseline")
            return {
                **common, "reason_code": reason_code, "reason": reason,
                "baseline_minutes": baseline["minutes"], "plan_minutes": None,
                "extra_minutes": None, "alternative": alternative,
                "details": {"at_minute": minute, "baseline_route": baseline_route,
                            "plan_status": "unreachable", "plan_error": baseline_error},
            }, True
        extra = int(plan["minutes"]) - int(baseline["minutes"])
        if extra > DETOUR_EXTRA_MINUTES_LIMIT:
            baseline_route = self._describe_route(baseline, "baseline")
            alternative = self._describe_route(plan, "plan_route")
            return {
                **common, "reason_code": DETOUR_DELAY,
                "reason": f"方案绕行多耗时 {extra} 分钟，超过 {DETOUR_EXTRA_MINUTES_LIMIT} 分钟阈值",
                "baseline_minutes": baseline["minutes"], "plan_minutes": plan["minutes"],
                "extra_minutes": extra, "alternative": alternative,
                "details": {"at_minute": minute, "baseline_route": baseline_route, "plan_route": alternative},
            }, True
        # 仍可达且未明显变慢：本时刻不受影响。
        return None, True

    def _describe_route(self, route: dict[str, Any], label: str) -> dict[str, Any]:
        if route.get("status") != "ok":
            return {"kind": label, "status": route.get("status", "unreachable"), "path": [], "minutes": None}
        stops = self._stop_names(route["path"])
        return {
            "kind": label,
            "status": "ok",
            "path": route["path"],
            "stops": stops,
            "minutes": route["minutes"],
            "legs": route["legs"],
            "arrival": route.get("arrival"),
        }

    def _stop_names(self, path: list[int]) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM stops")}
        return [
            {"stop_id": stop_id, "code": rows[stop_id]["code"], "name": rows[stop_id]["name"],
             "accessible": bool(rows[stop_id]["accessible"])}
            for stop_id in path if stop_id in rows
        ]
