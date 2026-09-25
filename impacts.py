"""乘客影响判断（影响通知台第二层）。

纯逻辑模块：给定一个"路径计算器"、订阅资料和方案变更，判断每个常用行程
在方案下是否受影响，受影响时给出可走替代路线；实在接不上则保留原因。

三种影响情形：
- broken：基线路线在方案里断掉（方案下不可达）；
- detour_delay：方案仍可达，但绕行多出 DETOUR_EXTRA_MINUTES 分钟及以上；
- accessibility：乘客只能走无障碍路线，方案下无障碍条件不满足
  （无障碍路线不可达；若连普通路线也接不上，则归入 broken）。

本模块不读写数据库、不知道 HTTP，也不知道 app.Database，方便单独测试。
"""
from __future__ import annotations

from typing import Any, Callable

from errors import DomainError

DETOUR_EXTRA_MINUTES = 15

# 影响原因 -> 面向调度员/乘客的中文说明
REASON_LABELS = {
    "broken": "原走法在方案中断掉，且找不到可通行路线",
    "detour_delay": f"绕行后多出 {DETOUR_EXTRA_MINUTES} 分钟及以上",
    "accessibility": "无障碍条件不满足，只能改走非无障碍路线",
}

Router = Callable[[int, int, int, bool], dict[str, Any]]


def windows_overlap(start_a: int, end_a: int, start_b: int | None, end_b: int | None) -> bool:
    """服务时间窗与变更生效窗是否重叠（端点相接也算重叠）。"""
    if start_b is None or end_b is None:
        return True
    return start_a <= end_b and start_b <= end_a


def evaluation_minutes(subscription: dict[str, Any], changes: list[dict[str, Any]]) -> int:
    """选择比对所用的服务日时刻。

    取与乘客常用服务时间窗重叠的生效变更中最早的开始时刻；没有时间窗的
    变更（全天生效）或没有重叠时，在乘客开始出行的时刻比对。
    """
    starts: list[int] = []
    window_start = int(subscription["service_start_minute"])
    window_end = int(subscription["service_end_minute"])
    for change in changes:
        c_start, c_end = change.get("effective_start_minute"), change.get("effective_end_minute")
        if c_start is None or c_end is None:
            continue
        if windows_overlap(window_start, window_end, int(c_start), int(c_end)):
            starts.append(int(c_start))
    return min(starts) if starts else window_start


def reachable(route: dict[str, Any]) -> bool:
    return route.get("status") == "ok" and route.get("minutes") is not None


def safe_route(router: Router, origin: int, destination: int,
               at_minute: int, require_accessible: bool) -> dict[str, Any] | None:
    """调用路径计算器，无障碍起终点本身不合法时视为不可达而非抛错。"""
    try:
        return router(origin, destination, at_minute, require_accessible)
    except DomainError:
        return None


def evaluate_one(baseline_router: Router, plan_router: Router,
                 subscription: dict[str, Any],
                 changes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """比对一条订阅的基线与方案路线；不受影响返回 None。"""
    origin = int(subscription["origin_stop_id"])
    destination = int(subscription["destination_stop_id"])
    require_accessible = bool(subscription["require_accessible"])
    at_minute = evaluation_minutes(subscription, changes)

    baseline = safe_route(baseline_router, origin, destination, at_minute, require_accessible)
    if baseline is None or not reachable(baseline):
        # 无障碍订阅的基线可能因起终点本身不具备无障碍条件而查不到，
        # 退化到普通路线确认它是不是一张有效行程。
        normal_baseline = safe_route(baseline_router, origin, destination, at_minute, False)
        if normal_baseline is None or not reachable(normal_baseline):
            return None  # 基线就不存在，不属于本方案造成的影响
        baseline = normal_baseline

    plan = safe_route(plan_router, origin, destination, at_minute, require_accessible)

    # 1) 原走法断掉：方案下连普通路线都接不上
    normal_plan = plan if plan is not None and reachable(plan) else safe_route(
        plan_router, origin, destination, at_minute, False)
    if normal_plan is None or not reachable(normal_plan):
        return {
            "reason": "broken",
            "reason_detail": REASON_LABELS["broken"],
            "at_minute": at_minute,
            "baseline_minutes": baseline["minutes"],
            "plan_minutes": None,
            "extra_minutes": None,
            "alternative_route": None,
        }

    # 2) 只能走无障碍路线，但方案下无障碍条件不满足
    if require_accessible and (plan is None or not reachable(plan)):
        return {
            "reason": "accessibility",
            "reason_detail": REASON_LABELS["accessibility"],
            "at_minute": at_minute,
            "baseline_minutes": baseline["minutes"],
            "plan_minutes": normal_plan["minutes"],
            "extra_minutes": max(0, int(normal_plan["minutes"]) - int(baseline["minutes"])),
            "alternative_route": normal_plan,
        }

    # 3) 走法还在，但绕行多出 15 分钟及以上
    extra = int(plan["minutes"]) - int(baseline["minutes"])
    if extra >= DETOUR_EXTRA_MINUTES:
        return {
            "reason": "detour_delay",
            "reason_detail": REASON_LABELS["detour_delay"],
            "at_minute": at_minute,
            "baseline_minutes": baseline["minutes"],
            "plan_minutes": plan["minutes"],
            "extra_minutes": extra,
            "alternative_route": plan,
        }
    return None


def build_notification_entries(baseline_router: Router, plan_router: Router,
                               subscriptions: list[dict[str, Any]],
                               changes: list[dict[str, Any]],
                               stops: dict[int, dict[str, Any]],
                               lines: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """评估所有生效订阅，返回待通知名单条目（未受影响的订阅不进名单）。"""
    entries: list[dict[str, Any]] = []
    for subscription in subscriptions:
        impact = evaluate_one(baseline_router, plan_router, subscription, changes)
        if impact is None:
            continue
        entries.append({
            "subscription_id": int(subscription["id"]),
            "passenger_name": subscription["passenger"]["name"],
            "contact": subscription["passenger"].get("contact", ""),
            "origin_stop_id": int(subscription["origin_stop_id"]),
            "destination_stop_id": int(subscription["destination_stop_id"]),
            "service_start_minute": int(subscription["service_start_minute"]),
            "service_end_minute": int(subscription["service_end_minute"]),
            "require_accessible": bool(subscription["require_accessible"]),
            "reason": impact["reason"],
            "reason_detail": impact["reason_detail"],
            "at_minute": impact["at_minute"],
            "baseline_minutes": impact["baseline_minutes"],
            "plan_minutes": impact["plan_minutes"],
            "extra_minutes": impact["extra_minutes"],
            "alternative_route": (
                present_route(impact["alternative_route"], stops, lines)
                if impact["alternative_route"] is not None else None
            ),
        })
    return entries


def present_route(route: dict[str, Any], stops: dict[int, dict[str, Any]],
                  lines: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """把路径结果里的站点/线路 ID 翻译成可直接展示的资料。"""
    def stop_brief(stop_id: int) -> dict[str, Any]:
        stop = stops.get(int(stop_id), {"id": stop_id, "code": "?", "name": "?", "accessible": None})
        return {"stop_id": stop["id"], "code": stop.get("code"), "name": stop.get("name"),
                "accessible": stop.get("accessible")}

    chain = [stop_brief(stop_id) for stop_id in route.get("path", [])]
    legs = []
    for leg in route.get("legs", []):
        line = lines.get(int(leg["line_id"])) if leg.get("line_id") is not None else None
        legs.append({
            "from": stop_brief(leg["from_stop_id"]),
            "to": stop_brief(leg["to_stop_id"]),
            "minutes": leg["minutes"],
            "line": {"id": line["id"], "code": line.get("code"), "name": line.get("name")} if line else None,
            "kind": leg.get("kind"),
        })
    result = {
        "minutes": route.get("minutes"),
        "status": route.get("status", "ok"),
        "stop_chain": chain,
        "legs": legs,
    }
    if route.get("arrival"):
        result["arrival"] = route["arrival"]
    return result
