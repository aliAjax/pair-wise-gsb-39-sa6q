import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
import impacts
from errors import DomainError as ErrorsDomainError


class NotificationDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}
        # 演示数据自带三条订阅；每个用例从干净订阅状态开始。
        for sub in self.db.list_subscriptions(include_inactive=True):
            self.db.deactivate_subscription(sub["id"], "planner-01", "planner")

    def tearDown(self):
        self.tmp.cleanup()

    def _sub(self, name, origin, destination, start=450, end=540, accessible=False,
             actor="planner-01", role="planner"):
        return self.db.register_subscription(actor, role, {
            "passenger_name": name, "contact": f"{name}@example.com",
            "origin_stop_id": origin, "destination_stop_id": destination,
            "service_start_minute": start, "service_end_minute": end,
            "require_accessible": accessible,
        })

    def _draft(self, code="D-100"):
        d = self.db.create_disruption("planner-01", {
            "code": code, "name": "夜间施工",
            "starts_at": "2026-09-24T22:00:00+08:00",
            "ends_at": "2026-09-25T02:00:00+08:00",
        }, "planner")
        return d["id"], d["draft_version_id"]

    def test_subscription_validation(self):
        s1, s2 = self.stops["S1"], self.stops["S2"]
        with self.assertRaises(DomainError):
            self._sub("同站", s1, s1)
        with self.assertRaises(DomainError):
            self._sub("坏时间窗", s1, s2, start=600, end=500)
        with self.assertRaises(DomainError):
            self._sub("越权", s1, s2, role="viewer")
        with self.assertRaises(DomainError):
            self._sub("未知站", s1, 99999)
        sub = self._sub("正常", s1, s2)
        self.assertTrue(sub["active"])
        self.assertFalse(sub["require_accessible"])

    def test_broken_origin_walk_goes_to_notify_without_alternative(self):
        # 乘客要去 S5，方案把终点站 S5 关掉：方案下接不上，进入名单且无替代路线。
        self._sub("去机场", self.stops["S1"], self.stops["S5"])
        _, version = self._draft()
        self.db.add_change(version, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"]}, "planner")
        listing = self.db.evaluate_notifications(version, "planner-01", "planner")
        self.assertEqual(listing["status"], "pending")
        self.assertEqual(listing["affected_count"], 1)
        entry = listing["entries"][0]
        self.assertEqual(entry["reason"], "broken")
        self.assertIsNone(entry["plan_minutes"])
        self.assertIsNone(entry["alternative_route"])
        self.assertEqual(entry["baseline_minutes"], 23)

    def test_detour_under_and_over_fifteen_minutes(self):
        # 演示拓扑（S1 到 S5 基线 23）：关闭 S4 后走 L1+L2 合并段为 31，只多 8 分钟。
        self._sub("去机场", self.stops["S1"], self.stops["S5"])
        _, version = self._draft()
        self.db.add_change(version, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        listing = self.db.evaluate_notifications(version, "planner-01", "planner")
        self.assertEqual(listing["affected_count"], 0)

        # 定制拓扑：T1 到 T9 基线靠两条接驳线在枢纽 T5 换乘，共 20 分钟；
        # 另有一条绕开枢纽的站站停慢线 T1-T2-T9 共 50 分钟。
        # 关闭枢纽 T5 后两条接驳线各自断开，只剩慢线，多 30 分钟。
        topology = {
            "lines": [
                {"code": "L10", "name": "接驳西"}, {"code": "L11", "name": "接驳东"},
                {"code": "L12", "name": "站站停慢线"},
            ],
            "stops": [
                {"code": "T1", "name": "起点", "latitude": 31.0, "longitude": 121.0},
                {"code": "T2", "name": "慢线中段", "latitude": 31.01, "longitude": 121.01},
                {"code": "T5", "name": "换乘枢纽", "latitude": 31.02, "longitude": 121.02},
                {"code": "T9", "name": "终点", "latitude": 31.03, "longitude": 121.03},
            ],
            "line_stops": [
                {"line_code": "L10", "stop_code": "T1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "L10", "stop_code": "T5", "sequence": 1, "travel_minutes_from_previous": 10},
                {"line_code": "L11", "stop_code": "T5", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "L11", "stop_code": "T9", "sequence": 1, "travel_minutes_from_previous": 10},
                {"line_code": "L12", "stop_code": "T1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "L12", "stop_code": "T2", "sequence": 1, "travel_minutes_from_previous": 22},
                {"line_code": "L12", "stop_code": "T9", "sequence": 2, "travel_minutes_from_previous": 28},
            ],
            "trips": [],
        }
        self.assertTrue(self.db.import_base("planner-01", topology, "planner")["accepted"])
        ids = {row["code"]: row["id"] for row in self.db.list_stops()}
        self.assertEqual(self.db.route(ids["T1"], ids["T9"])["minutes"], 20)

        other = self.db.create_disruption("planner-01", {
            "code": "D-150", "name": "换乘枢纽封闭",
            "starts_at": "2026-09-27T22:00:00+08:00", "ends_at": "2026-09-28T02:00:00+08:00",
        }, "planner")["draft_version_id"]
        self._sub("快线乘客", ids["T1"], ids["T9"])
        self.db.add_change(other, "planner-01",
                           {"kind": "stop_closure", "stop_id": ids["T5"]}, "planner")
        listing = self.db.evaluate_notifications(other, "planner-01", "planner")
        self.assertEqual(listing["affected_count"], 1)
        entry = listing["entries"][0]
        self.assertEqual(entry["reason"], "detour_delay")
        self.assertEqual(entry["baseline_minutes"], 20)
        self.assertEqual(entry["plan_minutes"], 50)
        self.assertEqual(entry["extra_minutes"], 30)
        chain = [stop["code"] for stop in entry["alternative_route"]["stop_chain"]]
        self.assertEqual(chain, ["T1", "T2", "T9"])

    def test_accessibility_requirement_unsatisfied(self):
        self._sub("轮椅乘客", self.stops["S1"], self.stops["S5"], accessible=True)
        _, version = self._draft()
        # 终点 S5 无障碍设施停用：只能走无障碍路线的乘客方案下接不上，
        # 但普通路线仍然可达（23 分钟），因此归入 accessibility 并给非无障碍替代。
        self.db.add_change(version, "planner-01",
                           {"kind": "accessibility_change", "stop_id": self.stops["S5"],
                            "accessible": False}, "planner")
        listing = self.db.evaluate_notifications(version, "planner-01", "planner")
        self.assertEqual(listing["affected_count"], 1)
        entry = listing["entries"][0]
        self.assertEqual(entry["reason"], "accessibility")
        self.assertTrue(entry["require_accessible"])
        self.assertEqual(entry["alternative_route"]["minutes"], 23)
        # 普通乘客同一方案不受影响。
        self._sub("普通乘客", self.stops["S1"], self.stops["S5"])
        listing = self.db.evaluate_notifications(version, "planner-01", "planner")
        reasons = {e["passenger_name"]: e["reason"] for e in listing["entries"]}
        self.assertNotIn("普通乘客", reasons)
        self.assertEqual(reasons["轮椅乘客"], "accessibility")

    def test_publish_requires_confirmation_and_freezes_list(self):
        self._sub("去机场", self.stops["S1"], self.stops["S5"])
        _, version = self._draft()
        self.db.add_change(version, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"]}, "planner")
        self.db.evaluate_notifications(version, "planner-01", "planner")
        self.db.transition(version, "planner-01", "planner", "submit")
        self.db.transition(version, "reviewer-01", "reviewer", "approve")
        with self.assertRaises(DomainError):
            self.db.transition(version, "reviewer-01", "reviewer", "publish")
        # 确认后可发布
        self.db.confirm_notifications(version, "planner-01", "planner")
        published = self.db.transition(version, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")

        frozen = self.db.get_notifications(version)
        self.assertEqual(frozen["status"], "frozen")
        self.assertEqual(frozen["affected_count"], 1)
        frozen_passenger = frozen["entries"][0]["passenger_name"]

        # 冻结后：停用订阅、改基础数据（本系统只能新导入，这里停用订阅）都不改变名单
        sub_id = self.db.list_subscriptions()[0]["id"]
        self.db.deactivate_subscription(sub_id, "planner-01", "planner")
        frozen_again = self.db.get_notifications(version)
        self.assertEqual(frozen_again["status"], "frozen")
        self.assertEqual(frozen_again["entries"][0]["passenger_name"], frozen_passenger)
        self.assertEqual(frozen_again["entries"], frozen["entries"])

        # 已冻结名单不能再评估或确认
        with self.assertRaises(DomainError):
            self.db.evaluate_notifications(version, "planner-01", "planner")

    def test_new_version_recalculates_list(self):
        self._sub("去机场", self.stops["S1"], self.stops["S5"])
        disruption_id, v1 = self._draft()
        self.db.add_change(v1, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"]}, "planner")
        self.db.evaluate_notifications(v1, "planner-01", "planner")
        self.db.confirm_notifications(v1, "planner-01", "planner")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        self.db.transition(v1, "reviewer-01", "reviewer", "publish")

        v2 = self.db.create_version_copy(disruption_id, v1, "planner-02", "planner")["id"]
        # 新版本还没有名单
        self.assertEqual(self.db.get_notifications(v2)["status"], "missing")
        # 只有草稿版本可以重新评估
        self.db.evaluate_notifications(v2, "planner-02", "planner")
        self.assertEqual(self.db.get_notifications(v2)["affected_count"], 1)
        self.db.confirm_notifications(v2, "planner-02", "planner")
        # 没有确认名单前发布会失败；确认后（上面已确认）可发布
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.assertEqual(self.db.transition(v2, "reviewer-02", "reviewer", "publish")["status"], "published")
        # v1 的冻结名单不受新版本动作影响
        self.assertEqual(self.db.get_notifications(v1)["status"], "frozen")

    def test_service_window_overlap_picks_change_start(self):
        self._sub("晚归", self.stops["S1"], self.stops["S5"], start=1410, end=1460)
        _, version = self._draft("D-200")
        self.db.add_change(version, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"],
                            "effective_start_minute": 1400, "effective_end_minute": 1500}, "planner")
        listing = self.db.evaluate_notifications(version, "planner-01", "planner")
        self.assertEqual(listing["affected_count"], 1)
        self.assertEqual(listing["entries"][0]["at_minute"], 1400)

        # 时间窗完全不重叠的变更不影响该乘客
        other = self.db.create_disruption("planner-01", {
            "code": "D-201", "name": "白天施工",
            "starts_at": "2026-09-26T08:00:00+08:00", "ends_at": "2026-09-26T10:00:00+08:00",
        }, "planner")["draft_version_id"]
        self.db.add_change(other, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"],
                            "effective_start_minute": 600, "effective_end_minute": 900}, "planner")
        self.assertEqual(self.db.evaluate_notifications(other, "planner-01", "planner")["affected_count"], 0)

    def test_re_evaluate_resets_confirmation(self):
        self._sub("去机场", self.stops["S1"], self.stops["S5"])
        _, version = self._draft("D-300")
        self.db.add_change(version, "planner-01",
                           {"kind": "stop_closure", "stop_id": self.stops["S5"]}, "planner")
        self.db.evaluate_notifications(version, "planner-01", "planner")
        confirmed = self.db.confirm_notifications(version, "planner-01", "planner")
        self.assertEqual(confirmed["status"], "confirmed")
        # 草稿仍可重新评估，确认状态回到待确认
        again = self.db.evaluate_notifications(version, "planner-02", "planner")
        self.assertEqual(again["status"], "pending")
        self.assertIsNone(again["confirmed_by"])

    def test_empty_list_must_still_be_confirmed_before_publish(self):
        _, version = self._draft("D-400")
        self.db.evaluate_notifications(version, "planner-01", "planner")
        self.db.transition(version, "planner-01", "planner", "submit")
        self.db.transition(version, "reviewer-01", "reviewer", "approve")
        with self.assertRaises(DomainError):
            self.db.transition(version, "reviewer-01", "reviewer", "publish")
        self.db.confirm_notifications(version, "planner-01", "planner")
        self.assertEqual(self.db.transition(version, "reviewer-01", "reviewer", "publish")["status"], "published")


class ImpactLogicTest(unittest.TestCase):
    """纯逻辑层用假路由器单测，不依赖数据库。"""

    def _sub(self, require_accessible=False):
        return {"id": 1, "origin_stop_id": 1, "destination_stop_id": 5,
                "service_start_minute": 450, "service_end_minute": 540,
                "require_accessible": require_accessible,
                "passenger": {"name": "测试", "contact": ""}}

    @staticmethod
    def _route(minutes, status="ok"):
        return {"status": status, "minutes": minutes, "path": [1, 5],
                "legs": [{"from_stop_id": 1, "to_stop_id": 5, "minutes": minutes,
                          "line_id": 1, "kind": "route"}]}

    def test_under_threshold_not_affected(self):
        router = lambda *a: self._route(30)  # baseline 23 由外部传入，这里模拟 +5
        base = lambda *a: self._route(25)
        self.assertIsNone(impacts.evaluate_one(base, router, self._sub(), []))

    def test_threshold_boundary(self):
        base = lambda *a: self._route(23)
        router14 = lambda *a: self._route(37)  # 多 14 分钟：不进
        router15 = lambda *a: self._route(38)  # 多 15 分钟：进
        self.assertIsNone(impacts.evaluate_one(base, router14, self._sub(), []))
        hit = impacts.evaluate_one(base, router15, self._sub(), [])
        self.assertEqual(hit["reason"], "detour_delay")
        self.assertEqual(hit["extra_minutes"], 15)

    def test_broken_reason(self):
        base = lambda *a: self._route(23)
        broken = lambda *a: {"status": "unreachable", "minutes": None, "path": [], "legs": []}
        hit = impacts.evaluate_one(base, broken, self._sub(), [])
        self.assertEqual(hit["reason"], "broken")
        self.assertIsNone(hit["alternative_route"])

    def test_accessible_router_raises_falls_back(self):
        # 无障碍路径在方案下抛领域错误（如起点不再无障碍），普通路径仍可达
        base = lambda o, d, m, a: self._route(23)

        def plan(o, d, m, a):
            if a:
                raise ErrorsDomainError("起点不具备无障碍通行条件", 409)
            return self._route(25)

        hit = impacts.evaluate_one(base, plan, self._sub(True), [])
        self.assertEqual(hit["reason"], "accessibility")
        self.assertIsNotNone(hit["alternative_route"])

    def test_evaluation_minute_uses_overlapping_window(self):
        sub = self._sub()
        changes = [
            {"effective_start_minute": 600, "effective_end_minute": 900},       # 不重叠
            {"effective_start_minute": 500, "effective_end_minute": 520},       # 重叠
            {"effective_start_minute": None, "effective_end_minute": None},     # 全天
        ]
        self.assertEqual(impacts.evaluation_minutes(sub, changes), 500)
        self.assertEqual(impacts.evaluation_minutes(sub, changes[:1]), 450)


if __name__ == "__main__":
    unittest.main()
