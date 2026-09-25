import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
import notifications as ns
from impact import DETOUR_EXTRA_MINUTES_LIMIT


class NotificationDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def _disruption(self, code="D-N1"):
        return self.db.create_disruption(
            "planner-01",
            {"code": code, "name": "施工", "starts_at": "2026-09-25T00:00:00+08:00", "ends_at": "2026-09-26T00:00:00+08:00"},
            "planner",
        )

    def test_subscription_validation_and_permissions(self):
        with self.assertRaises(DomainError):
            self.db.subscriptions.create("viewer", {
                "passenger_name": "钱七", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"],
                "service_start_minute": 480, "service_end_minute": 600, "accessible_only": False,
            }, "viewer")
        with self.assertRaises(DomainError):
            self.db.subscriptions.create("planner-01", {
                "passenger_name": "钱七", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S1"],
                "service_start_minute": 480, "service_end_minute": 600, "accessible_only": False,
            }, "planner")
        with self.assertRaises(DomainError):
            self.db.subscriptions.create("planner-01", {
                "passenger_name": "钱七", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"],
                "service_start_minute": 900, "service_end_minute": 600, "accessible_only": False,
            }, "planner")
        created = self.db.subscriptions.create("planner-01", {
            "passenger_name": "钱七", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"],
            "service_start_minute": 480, "service_end_minute": 1500, "accessible_only": True,
        }, "planner")
        self.assertTrue(created["accessible_only"])
        updated = self.db.subscriptions.update(created["id"], "planner-01", {"accessible_only": False}, "planner")
        self.assertFalse(updated["accessible_only"])

    def test_route_broken_keeps_reason_without_alternative(self):
        disruption = self._disruption()
        v = disruption["draft_version_id"]
        # S4 封闭：李无障碍 S1->S4 原走法中断
        self.db.add_change(v, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        result = self.db.impacts.analyze_version(v, "planner-01", "planner")
        affected = {i["subscription"]["passenger_name"]: i for i in result["items"]}
        self.assertIn("李无障碍", affected)
        item = affected["李无障碍"]
        self.assertEqual(item["reason_code"], "route_broken")
        self.assertIsNone(item["plan_minutes"])
        self.assertIsNone(item["alternative"])
        self.assertEqual(item["baseline_minutes"], 17)
        # 张北站走 S1->S5 多 8 分钟，不超过 15 分钟，不应进名单
        self.assertNotIn("张北站", affected)

    def test_accessibility_lost_when_destination_becomes_inaccessible(self):
        disruption = self._disruption("D-N2")
        v = disruption["draft_version_id"]
        self.db.add_change(v, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        result = self.db.impacts.analyze_version(v, "planner-01", "planner")
        affected = {i["subscription"]["passenger_name"]: i for i in result["items"]}
        item = affected["李无障碍"]
        self.assertEqual(item["reason_code"], "accessibility_lost")
        self.assertIsNone(item["alternative"])
        self.assertIn("无障碍", item["reason"])
        # 普通乘客不受影响
        self.assertNotIn("张北站", affected)

    def test_detour_over_fifteen_minutes_with_alternative(self):
        # 自建单线网络 a-b-d，基线 20 分钟，绕行 a->d 36 分钟（多 16）
        custom = Database(Path(self.tmp.name) / "detour.db")
        custom.import_base("planner-01", {
            "lines": [{"code": "A", "name": "A线"}],
            "stops": [
                {"code": "a", "name": "甲站", "latitude": 1, "longitude": 1},
                {"code": "b", "name": "乙站", "latitude": 1, "longitude": 2},
                {"code": "d", "name": "丁站", "latitude": 1, "longitude": 3},
            ],
            "line_stops": [
                {"line_code": "A", "stop_code": "a", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "A", "stop_code": "b", "sequence": 1, "travel_minutes_from_previous": 10},
                {"line_code": "A", "stop_code": "d", "sequence": 2, "travel_minutes_from_previous": 10},
            ],
            "trips": [],
        }, "planner")
        stops = {r["code"]: r["id"] for r in custom.list_stops()}
        line_id = custom.list_lines()[0]["id"]
        custom.subscriptions.create("planner-01", {
            "passenger_name": "王五", "from_stop_id": stops["a"], "to_stop_id": stops["d"],
            "service_start_minute": 600, "service_end_minute": 900, "accessible_only": False,
        }, "planner")
        def disruption_with_detour(code, minutes):
            d = custom.create_disruption("planner-01", {
                "code": code, "name": "绕行", "starts_at": "2026-10-01T00:00:00+08:00", "ends_at": "2026-10-02T00:00:00+08:00",
            }, "planner")
            ver = d["draft_version_id"]
            custom.add_change(ver, "planner-01", {"kind": "detour", "line_id": line_id, "from_stop_id": stops["a"], "to_stop_id": stops["d"], "travel_minutes": minutes}, "planner")
            return ver
        # 边界：恰好多 15 分钟不通知
        v = disruption_with_detour("X1", 20 + DETOUR_EXTRA_MINUTES_LIMIT)
        self.assertEqual(custom.route(stops["a"], stops["d"], v, 700)["minutes"], 35)
        self.assertEqual(custom.impacts.analyze_version(v, "planner-01", "planner")["items"], [])
        # 多 16 分钟：进入名单并给出替代（方案绕行走法）
        v2 = disruption_with_detour("X2", 36)
        result = custom.impacts.analyze_version(v2, "planner-01", "planner")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["reason_code"], "detour_delay")
        self.assertEqual(item["extra_minutes"], 16)
        self.assertIsNotNone(item["alternative"])
        self.assertEqual(item["alternative"]["minutes"], 36)
        self.assertEqual([s["code"] for s in item["alternative"]["stops"]], ["a", "d"])
        self.assertEqual(item["alternative"]["legs"][0]["kind"], "detour")

    def test_publish_requires_confirmation_and_freezes_list(self):
        disruption = self._disruption("D-N3")
        v = disruption["draft_version_id"]
        self.db.add_change(v, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self.db.impacts.analyze_version(v, "planner-01", "planner")
        # 先确认，然后模拟确认结果被清空的异常情况：直接确认后再走审批，正常可以发布。
        ns.confirm(self.db, v, "planner-01", "planner")
        self.db.transition(v, "planner-01", "planner", "submit")
        self.db.transition(v, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        frozen = ns.list_for_version(self.db, v)
        self.assertTrue(all(i["status"] == "frozen" for i in frozen["items"]))
        self.assertIsNotNone(frozen["items"][0]["subscription_snapshot"])
        # 发布后订阅改名不影响冻结名单
        sid = frozen["items"][0]["subscription_id"]
        self.db.subscriptions.update(sid, "planner-01", {"passenger_name": "改名乘客"}, "planner")
        again = ns.list_for_version(self.db, v)
        self.assertEqual(again["items"][0]["subscription"]["passenger_name"], "李无障碍")
        with self.assertRaises(DomainError):
            self.db.subscriptions.delete(sid, "planner-01", "planner")
        with self.assertRaises(DomainError):
            ns.confirm(self.db, v, "planner-01", "planner")

    def test_publish_blocked_when_list_not_confirmed(self):
        # 独立库：未确认名单即使审批通过也不能发布
        blocked_db = Database(Path(self.tmp.name) / "gate.db")
        seed_demo(blocked_db)
        stops = {row["code"]: row["id"] for row in blocked_db.list_stops()}
        disruption = blocked_db.create_disruption("planner-01", {
            "code": "G1", "name": "施工", "starts_at": "2026-09-25T00:00:00+08:00", "ends_at": "2026-09-26T00:00:00+08:00",
        }, "planner")
        v = disruption["draft_version_id"]
        blocked_db.add_change(v, "planner-01", {"kind": "stop_closure", "stop_id": stops["S4"]}, "planner")
        blocked_db.impacts.analyze_version(v, "planner-01", "planner")
        blocked_db.transition(v, "planner-01", "planner", "submit")
        blocked_db.transition(v, "reviewer-01", "reviewer", "approve")
        with self.assertRaises(DomainError):
            blocked_db.transition(v, "reviewer-01", "reviewer", "publish")
        # 完全没分析过的版本同样不能发布
        plain = blocked_db.create_disruption("planner-01", {
            "code": "G2", "name": "无分析", "starts_at": "2026-09-26T00:00:00+08:00", "ends_at": "2026-09-27T00:00:00+08:00",
        }, "planner")
        vv = plain["draft_version_id"]
        blocked_db.transition(vv, "planner-01", "planner", "submit")
        blocked_db.transition(vv, "reviewer-01", "reviewer", "approve")
        with self.assertRaises(DomainError):
            blocked_db.transition(vv, "reviewer-01", "reviewer", "publish")

    def test_draft_change_and_new_version_recompute(self):
        disruption = self._disruption("D-N4")
        v = disruption["draft_version_id"]
        self.db.add_change(v, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self.db.impacts.analyze_version(v, "planner-01", "planner")
        ns.confirm(self.db, v, "planner-01", "planner")
        # 草稿再加变更：名单作废
        self.db.add_change(v, "planner-01", {"kind": "skip_stop", "stop_id": self.stops["S2"]}, "planner")
        self.assertEqual(ns.list_for_version(self.db, v)["status"], "not_analyzed")
        # 重新走完发布
        self.db.impacts.analyze_version(v, "planner-01", "planner")
        ns.confirm(self.db, v, "planner-01", "planner")
        self.db.transition(v, "planner-01", "planner", "submit")
        self.db.transition(v, "reviewer-01", "reviewer", "approve")
        self.db.transition(v, "reviewer-01", "reviewer", "publish")
        # 新版本没有继承名单，需要重新算；旧版本名单仍冻结
        v2 = self.db.create_version_copy(disruption["id"], v, "planner-02", "planner")["id"]
        self.assertEqual(ns.list_for_version(self.db, v2)["status"], "not_analyzed")
        self.db.impacts.analyze_version(v2, "planner-02", "planner")
        self.assertEqual(ns.list_for_version(self.db, v)["items"][0]["status"], "frozen")

    def test_subscription_locked_during_review_and_empty_list_confirm(self):
        # 已提交复核/已批准版本存在时，订阅修改被拦截
        disruption = self._disruption("D-N5")
        v = disruption["draft_version_id"]
        self.db.add_change(v, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self.db.impacts.analyze_version(v, "planner-01", "planner")
        ns.confirm(self.db, v, "planner-01", "planner")
        self.db.transition(v, "planner-01", "planner", "submit")
        with self.assertRaises(DomainError):
            self.db.subscriptions.create("planner-01", {
                "passenger_name": "临时新增", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"],
                "service_start_minute": 600, "service_end_minute": 900, "accessible_only": False,
            }, "planner")
        self.db.transition(v, "reviewer-01", "reviewer", "reject")
        # 退回后订阅可以修改；改动会清掉草稿版本的旧分析结论
        self.db.subscriptions.create("planner-01", {
            "passenger_name": "临时新增", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"],
            "service_start_minute": 600, "service_end_minute": 900, "accessible_only": False,
        }, "planner")
        self.assertEqual(ns.list_for_version(self.db, v)["status"], "not_analyzed")
        # 无影响版本：空名单也要显式确认才能发布
        no_impact = self._disruption("D-N6")
        vn = no_impact["draft_version_id"]
        self.db.impacts.analyze_version(vn, "planner-01", "planner")
        ns.confirm(self.db, vn, "planner-01", "planner")
        self.db.transition(vn, "planner-01", "planner", "submit")
        self.db.transition(vn, "reviewer-01", "reviewer", "approve")
        self.assertEqual(self.db.transition(vn, "reviewer-01", "reviewer", "publish")["status"], "published")


    def test_effective_window_intersects_service_window(self):
        # 李无障碍 07:00-10:00 乘车；施工只在深夜 1200-1380 分钟生效，二者不重叠 -> 不进名单
        disruption = self._disruption("D-W1")
        v = disruption["draft_version_id"]
        self.db.add_change(v, "planner-01", {
            "kind": "stop_closure", "stop_id": self.stops["S4"],
            "effective_start_minute": 1200, "effective_end_minute": 1380,
        }, "planner")
        result = self.db.impacts.analyze_version(v, "planner-01", "planner")
        affected = {i["subscription"]["passenger_name"] for i in result["items"]}
        self.assertNotIn("李无障碍", affected)
        # 把生效窗口调到与早高峰重叠 -> 应检测到中断
        disruption2 = self._disruption("D-W2")
        v2 = disruption2["draft_version_id"]
        self.db.add_change(v2, "planner-01", {
            "kind": "stop_closure", "stop_id": self.stops["S4"],
            "effective_start_minute": 480, "effective_end_minute": 540,
        }, "planner")
        result2 = self.db.impacts.analyze_version(v2, "planner-01", "planner")
        affected2 = {i["subscription"]["passenger_name"]: i for i in result2["items"]}
        self.assertIn("李无障碍", affected2)
        self.assertGreaterEqual(affected2["李无障碍"]["evaluated_at_minute"], 480)
        self.assertLessEqual(affected2["李无障碍"]["evaluated_at_minute"], 540)


if __name__ == "__main__":
    unittest.main()
