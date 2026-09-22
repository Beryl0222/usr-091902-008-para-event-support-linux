"""运行时测试：重复消息幂等、重启回放恢复、命令原子提交。"""

import json
import os
import tempfile
import threading
import unittest

from runtime import Runtime
from scheduling import ValidationError, ConflictError


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.jsonl")
        self.rt = Runtime(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, rt=None):
        rt = rt or self.rt
        rt.execute({"command": "add_resource", "resource": {
            "id": "van-a", "type": "无障碍车", "city": "甲市",
            "capabilities": ["无障碍车"]}})
        rt.execute({"command": "register_athlete", "athlete": {
            "id": "p1", "name": "甲", "team": "甲队", "sport_class": "T54",
            "support_needs": ["无障碍车"], "city": "甲市"}})
        rt.execute({"command": "plan_leg", "leg": {
            "id": "l1", "athlete_id": "p1", "purpose": "比赛", "event": "E",
            "city": "甲市", "venue": "V", "start": "1-09:00", "end": "1-11:00",
            "requirements": ["无障碍车"]}})

    def test_duplicate_request_id_is_replayed_without_double_booking(self):
        self._seed()
        command = {"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                   "need": "无障碍车", "start": "1-08:30", "end": "1-11:30",
                   "owner": "联络员甲", "request_id": "msg-001"}
        first = self.rt.execute(command)
        second = self.rt.execute(dict(command))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"]["booking_id"], second["result"]["booking_id"])
        self.assertEqual(len(self.rt.store.bookings), 1)

    def test_idempotency_key_dedupes(self):
        self._seed()
        payload = {"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                   "need": "无障碍车", "start": "1-08:30", "end": "1-11:30"}
        first = self.rt.execute({**payload, "idempotency_key": "k1"})
        second = self.rt.execute({**payload, "idempotency_key": "k1"})
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"]["booking_id"], second["result"]["booking_id"])

    def test_restart_replays_state_and_dedupe_indexes(self):
        self._seed()
        self.rt.execute({"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                         "need": "无障碍车", "start": "1-08:30", "end": "1-11:30",
                         "idempotency_key": "k1", "request_id": "r9"})
        lines = [ln for ln in open(self.path, encoding="utf-8") if ln.strip()]
        self.assertTrue(lines)

        rt2 = Runtime(self.path)
        self.assertEqual(rt2.query("athlete_support", athlete_id="p1")[
            "itinerary"][0]["state"], "保障中")
        # 旧幂等键在重启后仍然生效
        replay = rt2.execute({"command": "assign", "resource_id": "van-a",
                              "leg_id": "l1", "need": "无障碍车",
                              "start": "1-08:30", "end": "1-11:30",
                              "idempotency_key": "k1"})
        self.assertTrue(replay["replayed"])
        replay2 = rt2.execute({"command": "add_resource", "resource": {
            "id": "van-a", "type": "无障碍车", "city": "甲市",
            "capabilities": ["无障碍车"]}, "request_id": "r9"})
        self.assertTrue(replay2["replayed"])
        self.assertEqual(len(rt2.store.resources), 1)

    def test_multi_event_command_persists_atomically(self):
        self._seed()
        self.rt.execute({"command": "add_resource", "resource": {
            "id": "van-b", "type": "无障碍车", "city": "甲市",
            "capabilities": ["无障碍车"]}})
        # 故障命令会同时产生释放、改派、通知等多个事件，落在同一条日志记录里
        self.rt.execute({"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                         "need": "无障碍车", "start": "1-08:30", "end": "1-11:30"})
        result = self.rt.execute({"command": "equipment_failure",
                                  "resource_id": "van-a", "at": "1-08:00"})
        self.assertEqual(result["result"]["reassigned"], ["l1"])
        envelopes = [json.loads(ln) for ln in open(self.path, encoding="utf-8") if ln.strip()]
        last = envelopes[-1]
        self.assertGreater(last["event_count"], 1)
        # 重启后改派结果完整恢复
        rt2 = Runtime(self.path)
        active = [b for b in rt2.store.bookings.values() if b["status"] == "active"]
        self.assertEqual([b["resource_id"] for b in active], ["van-b"])
        self.assertEqual(rt2.store.resources["van-a"]["status"], "failed")
        self.assertTrue(rt2.store.notifications)

    def test_failed_command_leaves_no_state_or_log(self):
        self._seed()
        events_before = len(self.rt.store.events)
        with self.assertRaises(ValidationError):
            self.rt.execute({"command": "plan_leg", "leg": {
                "id": "bad", "athlete_id": "p1", "purpose": "比赛",
                "start": "1-10:00", "end": "1-09:00",
                "requirements": ["无障碍车"]}})  # 结束早于开始
        self.assertEqual(len(self.rt.store.events), events_before)
        self.assertNotIn("bad", self.rt.store.legs)
        # 磁盘上也没有半条记录
        with open(self.path, encoding="utf-8") as handle:
            self.assertNotIn("bad", handle.read())

    def test_failed_command_after_partial_domain_events_rolls_back(self):
        self._seed()
        # 未知命令会在分发阶段失败；此前若产生过事件也必须整体回滚
        events_before = len(self.rt.store.events)
        with self.assertRaises(ValidationError):
            self.rt.execute({"command": "no_such_command"})
        self.assertEqual(len(self.rt.store.events), events_before)

    def test_concurrent_duplicates_commit_once(self):
        self._seed()
        results = []

        def fire():
            results.append(self.rt.execute({
                "command": "assign", "resource_id": "van-a", "leg_id": "l1",
                "need": "无障碍车", "start": "1-08:30", "end": "1-11:30",
                "idempotency_key": "concurrent-k"}))

        threads = [threading.Thread(target=fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.rt.store.bookings), 1)
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 1)

    def test_concurrent_distinct_requests_respect_capacity(self):
        # 容量为 2 的班车面对 5 个并发安排，恰好成功 2 个，其余冲突
        self.rt.execute({"command": "add_resource", "resource": {
            "id": "shuttle", "type": "通勤班车", "city": "甲市",
            "capabilities": ["通勤车"], "capacity": 2}})
        self.rt.execute({"command": "register_athlete", "athlete": {
            "id": "p2", "sport_class": "T54", "support_needs": []}})
        legs = []
        for i in range(5):
            leg_id = f"l{i}"
            legs.append(leg_id)
            self.rt.execute({"command": "plan_leg", "leg": {
                "id": leg_id, "athlete_id": "p2", "purpose": "训练", "city": "甲市",
                "start": "1-09:00", "end": "1-11:00", "requirements": ["通勤车"]}})
        outcomes = []

        def fire(leg_id):
            try:
                self.rt.execute({"command": "assign", "resource_id": "shuttle",
                                 "leg_id": leg_id, "need": "通勤车",
                                 "start": "1-09:00", "end": "1-11:00"})
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=fire, args=(leg_id,)) for leg_id in legs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes).count("ok"), 2)
        self.assertEqual(sorted(outcomes).count("conflict"), 3)

    def test_diagnosis_never_reaches_disk(self):
        with self.assertRaises(ValidationError):
            self.rt.execute({"command": "register_athlete", "athlete": {
                "id": "px", "诊断": "敏感病历内容"}})
        # 失败命令不创建/追加日志文件
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as handle:
                self.assertNotIn("敏感病历内容", handle.read())


if __name__ == "__main__":
    unittest.main()
