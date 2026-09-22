"""调度领域模型测试：覆盖保障中心章程中的关键不变量。"""

import unittest

from scheduling import (
    SupportStore, ValidationError, ConflictError, to_minutes,
)


def make_world():
    """构造常用三地两车队的基础数据。"""
    store = SupportStore()
    store.add_resource({"id": "van-a", "type": "无障碍车", "city": "甲市",
                        "capabilities": ["无障碍车"]})
    store.add_resource({"id": "van-b", "type": "无障碍车", "city": "甲市",
                        "capabilities": ["无障碍车"]})
    store.add_resource({"id": "van-c", "type": "无障碍车", "city": "乙市",
                        "capabilities": ["无障碍车"]})
    store.add_resource({"id": "bus-a", "type": "通勤车", "city": "甲市",
                        "capabilities": ["通勤车"]})
    store.add_resource({"id": "vol-sign", "type": "手语志愿者", "city": "乙市",
                        "capabilities": ["手语志愿"]})
    store.register_athlete({"id": "p1", "name": "甲队一号", "team": "甲队",
                            "sport_class": "T54", "support_needs": ["无障碍车"],
                            "city": "甲市"})
    store.register_athlete({"id": "p2", "name": "乙队二号", "team": "乙队",
                            "sport_class": "F56", "support_needs": ["无障碍车"],
                            "city": "甲市"})
    return store


def race_leg(store, leg_id, athlete_id, city="甲市", start="1-09:00", end="1-11:00",
             requirements=("无障碍车",), **extra):
    return store.plan_leg({
        "id": leg_id, "athlete_id": athlete_id, "purpose": "比赛",
        "event": "田径100米", "city": city, "venue": f"{city}体育场",
        "start": start, "end": end, "requirements": list(requirements), **extra,
    })


class MinimalDisclosureTest(unittest.TestCase):
    def test_rejects_diagnosis_fields(self):
        store = SupportStore()
        for bad_key in ("诊断", "diagnosis", "病历"):
            with self.assertRaises(ValidationError):
                store.register_athlete({"id": "x", "sport_class": "T54", bad_key: "详情"})

    def test_rejects_unknown_fields(self):
        store = SupportStore()
        with self.assertRaises(ValidationError):
            store.register_athlete({"id": "x", "id_number": "不应登记"})

    def test_rejects_unknown_support_need(self):
        store = SupportStore()
        with self.assertRaises(ValidationError):
            store.register_athlete({"id": "x", "support_needs": ["专人看护"]})

    def test_accepts_only_sport_class_and_functional_needs(self):
        store = SupportStore()
        athlete = store.register_athlete({
            "id": "x", "name": "甲", "team": "甲队", "sport_class": "S7",
            "support_needs": ["无障碍车", "手语志愿"], "city": "甲市"})
        self.assertEqual(set(athlete), {"id", "name", "team", "sport_class",
                                        "support_needs", "city"})


class BookingTest(unittest.TestCase):
    def test_same_accessible_vehicle_cannot_be_promised_twice(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-09:30", end="1-10:30")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30", owner="甲队联络员")
        with self.assertRaises(ConflictError):
            store.assign("van-a", "l2", "无障碍车", "1-09:00", "1-11:00")
        # 第二支队仍可使用另一辆无障碍车
        store.assign("van-b", "l2", "无障碍车", "1-09:00", "1-11:00")
        self.assertEqual(store.leg_state(store.legs["l2"]), "保障中")

    def test_touching_windows_do_not_conflict(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-10:00")
        race_leg(store, "l2", "p2", start="1-10:00", end="1-11:00")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-10:00")
        store.assign("van-a", "l2", "无障碍车", "1-10:00", "1-11:00")
        self.assertEqual(store.leg_state(store.legs["l2"]), "保障中")

    def test_capacity_allows_multiple_concurrent_bookings(self):
        store = SupportStore()
        store.add_resource({"id": "shuttle", "type": "通勤班车", "city": "甲市",
                            "capabilities": ["通勤车"], "capacity": 2})
        store.register_athlete({"id": "p1", "sport_class": "T54", "support_needs": []})
        store.register_athlete({"id": "p2", "sport_class": "T54", "support_needs": []})
        race_leg(store, "l1", "p1", requirements=("通勤车",))
        race_leg(store, "l2", "p2", requirements=("通勤车",))
        store.assign("shuttle", "l1", "通勤车", "1-09:00", "1-11:00")
        store.assign("shuttle", "l2", "通勤车", "1-09:00", "1-11:00")
        with self.assertRaises(ConflictError):
            race_leg(store, "l3", "p1", start="1-14:00", end="1-15:00",
                     requirements=("通勤车",))
            store.assign("shuttle", "l3", "通勤车", "1-10:00", "1-10:30")

    def test_assign_must_match_leg_requirement_resource_capability_and_city(self):
        store = make_world()
        race_leg(store, "l1", "p1", requirements=("无障碍车",))
        with self.assertRaises(ValidationError):
            store.assign("bus-a", "l1", "通勤车", "1-09:00", "1-10:00")
        # van-c 属乙市，不能承诺给甲市行程
        with self.assertRaises(ValidationError):
            store.assign("van-c", "l1", "无障碍车", "1-09:00", "1-10:00")


class EmergencyPreemptionTest(unittest.TestCase):
    def _setup(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-14:00", end="1-16:00",
                 requirements=("通勤车",))
        store.assign("bus-a", "l2", "通勤车", "1-13:30", "1-16:30", owner="乙队联络员")
        return store

    def test_emergency_preempts_commuter_resource_with_reason(self):
        store = self._setup()
        original = next(b for b in store.bookings.values() if b["leg_id"] == "l2")
        outcome = store.assign("bus-a", "l1", "通勤车", "1-14:00", "1-15:00",
                               priority="emergency", reason="送医急救", now=100)
        self.assertEqual(outcome["displaced"], [original["id"]])
        self.assertEqual(store.bookings[original["id"]]["status"], "displaced")
        # 原安排负责人收到通知，且通知中含理由与资源
        note = store.notifications[-1]
        self.assertEqual(note["to"], "乙队联络员")
        self.assertEqual(note["context"]["reason"], "送医急救")
        self.assertEqual(note["context"]["resource_id"], "bus-a")

    def test_emergency_requires_reason(self):
        store = self._setup()
        with self.assertRaises(ValidationError):
            store.assign("bus-a", "l1", "通勤车", "1-14:00", "1-15:00",
                         priority="emergency", now=100)

    def test_emergency_cannot_preempt_accessible_resource(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-09:30", end="1-10:30")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-11:00", owner="甲队联络员")
        with self.assertRaises(ConflictError):
            store.assign("van-a", "l2", "无障碍车", "1-09:30", "1-10:30",
                         priority="emergency", reason="急救")

    def test_notification_falls_back_to_duty_dispatcher_without_owner(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-14:00", end="1-16:00",
                 requirements=("通勤车",))
        store.assign("bus-a", "l2", "通勤车", "1-13:30", "1-16:30")  # 无 owner
        store.assign("bus-a", "l1", "通勤车", "1-14:00", "1-15:00",
                     priority="emergency", reason="送医", now=1)
        self.assertEqual(store.notifications[-1]["to"], "赛区调度员")


class LocalizedReplanTest(unittest.TestCase):
    def test_equipment_failure_reassigns_when_alternative_exists(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30")
        result = store.equipment_failure("van-a", at="1-08:00")
        self.assertEqual(result["reassigned"], ["l1"])
        self.assertEqual(result["needs_replan"], [])
        active = [b for b in store.bookings.values() if b["leg_id"] == "l1"
                  and b["status"] == "active"]
        self.assertEqual(active[0]["resource_id"], "van-b")
        self.assertEqual(store.resources["van-a"]["status"], "failed")

    def test_equipment_failure_marks_stranded_when_no_alternative(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-09:30", end="1-10:30")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30")
        store.assign("van-b", "l2", "无障碍车", "1-09:00", "1-11:00")
        result = store.equipment_failure("van-a", at="1-08:00")
        self.assertEqual(result["needs_replan"], ["l1"])
        self.assertEqual(store.leg_state(store.legs["l1"]), "需改派")
        # 另一支队的安排不动
        self.assertTrue(all(b["resource_id"] == "van-b"
                            for b in store.bookings.values()
                            if b["leg_id"] == "l2" and b["status"] == "active"))

    def test_failure_replan_never_double_books_one_alternative(self):
        store = make_world()
        # van-a 上两支不同时段队伍，唯一替代 van-b 全程空闲但容量为 1：
        # 两条都能改派（窗口互不重叠）；再构造一条重叠队伍时必须有人 stranded。
        race_leg(store, "l1", "p1", start="1-09:00", end="1-10:00")
        race_leg(store, "l2", "p2", start="1-10:00", end="1-11:00")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-10:00")
        store.assign("van-a", "l2", "无障碍车", "1-10:00", "1-11:00")
        result = store.equipment_failure("van-a", at="1-08:00")
        self.assertEqual(sorted(result["reassigned"]), ["l1", "l2"])
        resources = {b["resource_id"] for b in store.bookings.values()
                     if b["status"] == "active"}
        self.assertEqual(resources, {"van-b"})

    def test_past_bookings_are_not_touched_by_failure(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-10:00")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-10:00")
        result = store.equipment_failure("van-a", at="1-12:00")
        self.assertEqual(result["released"], [])

    def test_delay_only_moves_named_legs_and_their_bookings(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-09:30", end="1-10:30")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30", owner="甲队联络员")
        store.assign("van-b", "l2", "无障碍车", "1-09:00", "1-11:00", owner="乙队联络员")
        result = store.delay_legs(["l2"], delta=60, at="1-08:00")
        self.assertEqual(result["delayed_legs"], ["l2"])
        b1 = [b for b in store.bookings.values() if b["leg_id"] == "l1"][0]
        b2 = [b for b in store.bookings.values() if b["leg_id"] == "l2"][0]
        self.assertEqual((b1["start"], b1["end"]),
                         (to_minutes("1-08:30"), to_minutes("1-11:30")))
        self.assertEqual((b2["start"], b2["end"]),
                         (to_minutes("1-10:00"), to_minutes("1-12:00")))
        self.assertEqual(store.legs["l1"]["start"], to_minutes("1-09:00"))

    def test_delay_reroutes_only_clashing_booking(self):
        store = make_world()
        store.add_resource({"id": "van-d", "type": "无障碍车", "city": "甲市",
                            "capabilities": ["无障碍车"]})
        store.register_athlete({"id": "p3", "sport_class": "T54",
                                "support_needs": ["无障碍车"], "city": "甲市"})
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-11:00", end="1-12:00")
        race_leg(store, "l3", "p3", start="1-09:30", end="1-10:30")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-11:00")
        store.assign("van-b", "l2", "无障碍车", "1-11:00", "1-12:00")
        store.assign("van-b", "l3", "无障碍车", "1-09:30", "1-10:30")
        # l2 提前 60 分钟后，其 van-b 占用（10:00-11:00）与 l3（09:30-10:30）冲突
        # → 仅改派 l2 到空闲的 van-d；l1、l3 不动
        result = store.delay_legs(["l2"], delta=-60, at="1-08:00")
        self.assertEqual(result["rerouted"], ["l2"])
        active_l2 = [b for b in store.bookings.values()
                     if b["leg_id"] == "l2" and b["status"] == "active"]
        self.assertEqual(active_l2[0]["resource_id"], "van-d")
        self.assertEqual([b for b in store.bookings.values() if b["leg_id"] == "l1"
                          and b["status"] == "active"][0]["resource_id"], "van-a")
        self.assertEqual([b for b in store.bookings.values() if b["leg_id"] == "l3"
                          and b["status"] == "active"][0]["resource_id"], "van-b")

    def test_classification_change_only_affects_future_legs_of_one_athlete(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-10:00")
        race_leg(store, "l2", "p1", start="2-09:00", end="2-10:00")
        race_leg(store, "l3", "p2", start="2-09:00", end="2-10:00")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-10:00")
        store.assign("van-a", "l2", "无障碍车", "2-09:00", "2-10:00")
        store.assign("van-b", "l3", "无障碍车", "2-09:00", "2-10:00")
        result = store.classification_change(
            "p1", "T53", [], at="1-18:00")
        self.assertIn("l2", result["affected_legs"])
        self.assertNotIn("l1", result["affected_legs"])  # 已结束
        self.assertTrue(all(b["leg_id"] != "l2" or b["status"] != "active"
                            for b in store.bookings.values()))
        # p2 完全不受影响
        self.assertTrue(any(b["leg_id"] == "l3" and b["status"] == "active"
                            for b in store.bookings.values()))
        self.assertEqual(store.athletes["p1"]["sport_class"], "T53")


    def test_classification_change_adds_need_and_auto_provisions_future_legs(self):
        store = make_world()
        # p1 原只需无障碍车；分级后新增手语志愿，未来比赛行程应同步补要求并自动配志愿者
        race_leg(store, "l1", "p1", start="1-09:00", end="1-10:00")
        store.assign("van-a", "l1", "无障碍车", "1-09:00", "1-10:00")
        race_leg(store, "l2", "p1", city="乙市", start="2-09:00", end="2-10:00")
        store.assign("van-c", "l2", "无障碍车", "2-09:00", "2-10:00")
        result = store.classification_change(
            "p1", "T53", ["无障碍车", "手语志愿"], at="1-18:00")
        self.assertIn("l2", result["added"])
        self.assertNotIn("l1", result["added"])  # 已结束行程不补
        self.assertIn("手语志愿", store.legs["l2"]["requirements"])
        l2_view = store.athlete_support_view("p1")["itinerary"]
        l2 = next(item for item in l2_view if item["leg_id"] == "l2")
        needs = {a["need"] for a in l2["assignments"]}
        self.assertEqual(needs, {"无障碍车", "手语志愿"})
        self.assertEqual(l2["state"], "保障中")


class CrossCityHandoffTest(unittest.TestCase):
    def _athlete_across_cities(self, store, *, with_bridge=True, covered=True):
        store.register_athlete({"id": "p3", "name": "丙队三号", "team": "丙队",
                                "sport_class": "S7", "support_needs": ["无障碍车"]})
        store.add_resource({"id": "van-x", "type": "跨城无障碍车",
                            "capabilities": ["无障碍车"]})
        store.plan_leg({"id": "city-a-leg", "athlete_id": "p3", "purpose": "比赛",
                        "city": "甲市", "venue": "甲市游泳馆",
                        "start": "2-09:00", "end": "2-11:00", "requirements": []})
        store.plan_leg({"id": "city-b-leg", "athlete_id": "p3", "purpose": "比赛",
                        "city": "乙市", "venue": "乙市体育馆",
                        "start": "2-15:00", "end": "2-17:00", "requirements": []})
        if with_bridge:
            store.plan_leg({"id": "bridge", "athlete_id": "p3",
                            "purpose": "跨城交接", "from_city": "甲市", "to_city": "乙市",
                            "start": "2-11:30", "end": "2-14:30",
                            "requirements": ["无障碍车"] if covered else []})
            if covered:
                store.assign("van-x", "bridge", "无障碍车",
                             "2-11:30", "2-14:30", owner="跨城接驳组")

    def test_gap_detected_without_bridge(self):
        store = make_world()
        self._athlete_across_cities(store, with_bridge=False)
        gaps = store.handoff_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["from_city"], "甲市")
        self.assertEqual(gaps[0]["to_city"], "乙市")
        self.assertEqual(gaps[0]["before_leg"], "city-b-leg")

    def test_planned_but_uncovered_bridge_is_still_a_gap(self):
        store = make_world()
        # 桥接行程存在但无障碍车未落实 → 仍是断档
        store.register_athlete({"id": "p3", "sport_class": "S7",
                                "support_needs": ["无障碍车"]})
        store.add_resource({"id": "van-x", "type": "跨城无障碍车",
                            "capabilities": ["无障碍车"]})
        store.plan_leg({"id": "a", "athlete_id": "p3", "purpose": "比赛",
                        "city": "甲市", "start": "2-09:00", "end": "2-11:00",
                        "requirements": []})
        store.plan_leg({"id": "b", "athlete_id": "p3", "purpose": "比赛",
                        "city": "乙市", "start": "2-15:00", "end": "2-17:00",
                        "requirements": []})
        store.plan_leg({"id": "bridge", "athlete_id": "p3",
                        "purpose": "跨城交接", "from_city": "甲市", "to_city": "乙市",
                        "start": "2-11:30", "end": "2-14:30",
                        "requirements": ["无障碍车"]})
        self.assertEqual(len(store.handoff_gaps()), 1)
        store.assign("van-x", "bridge", "无障碍车", "2-11:30", "2-14:30")
        self.assertEqual(store.handoff_gaps(), [])

    def test_covered_bridge_closes_gap(self):
        store = make_world()
        self._athlete_across_cities(store)
        self.assertEqual(store.handoff_gaps(), [])

    def test_same_city_legs_have_no_gap(self):
        store = make_world()
        race_leg(store, "l1", "p1", city="甲市", start="2-09:00", end="2-10:00")
        race_leg(store, "l2", "p1", city="甲市", start="2-11:00", end="2-12:00")
        self.assertEqual(store.handoff_gaps(), [])


class ViewTest(unittest.TestCase):
    def test_public_schedule_is_sanitized(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30", owner="甲队联络员")
        # 训练行程不应出现在公众赛程
        store.plan_leg({"id": "train", "athlete_id": "p1", "purpose": "训练",
                        "city": "甲市", "start": "1-07:00", "end": "1-08:00",
                        "requirements": ["无障碍车"]})
        view = store.public_schedule_view()
        self.assertEqual(len(view), 1)
        entry = view[0]["entries"][0]
        self.assertEqual(entry["sport_class"], "T54")  # 赛事资格可公开
        for forbidden in ("support_needs", "无障碍", "联络员", "owner"):
            self.assertNotIn(forbidden, str(view))

    def test_support_view_carries_assignments_and_state(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        view = store.athlete_support_view("p1")
        self.assertEqual(view["itinerary"][0]["state"], "待安排")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30")
        view = store.athlete_support_view("p1")
        self.assertEqual(view["itinerary"][0]["state"], "保障中")
        self.assertEqual(view["itinerary"][0]["assignments"][0]["resource_id"], "van-a")


class AuditTrailTest(unittest.TestCase):
    def test_trail_records_each_change_and_impact(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        race_leg(store, "l2", "p2", start="1-14:00", end="1-16:00",
                 requirements=("通勤车",))
        store.assign("bus-a", "l2", "通勤车", "1-13:30", "1-16:30", owner="乙队联络员")
        store.assign("bus-a", "l1", "通勤车", "1-14:00", "1-15:00",
                     priority="emergency", reason="送医急救", now=500)
        # 征用方视角：booked 事件带理由与被抢占安排清单
        trail_p1 = store.audit_trail(athlete_id="p1")
        booked = next(item for item in trail_p1 if item["kind"] == "booked")
        self.assertEqual(booked["impact"]["preemption_reason"], "送医急救")
        self.assertTrue(booked["impact"]["preempted_bookings"])
        # 被征用方视角：通知原负责人的记录挂在 p2 名下
        trail_p2 = store.audit_trail(athlete_id="p2")
        notified = next(item for item in trail_p2 if item["kind"] == "notified")
        self.assertEqual(notified["impact"]["to"], "乙队联络员")
        self.assertEqual(notified["impact"]["context"]["reason"], "送医急救")
        # 按行程过滤
        self.assertTrue(all("l1" in item["leg_ids"] for item in
                            store.audit_trail(leg_id="l1")))

    def test_trail_records_failure_and_delay_with_actual_impact(self):
        store = make_world()
        race_leg(store, "l1", "p1", start="1-09:00", end="1-11:00")
        store.assign("van-a", "l1", "无障碍车", "1-08:30", "1-11:30")
        store.equipment_failure("van-a", at="1-08:00")
        trail = store.audit_trail(leg_id="l1")
        failure = next(item for item in trail if item["kind"] == "resource_failed")
        self.assertEqual(failure["impact"]["resource_id"], "van-a")
        self.assertTrue(failure["impact"]["released"])
        self.assertEqual(failure["impact"]["replacement_resources"], ["van-b"])


if __name__ == "__main__":
    unittest.main()
