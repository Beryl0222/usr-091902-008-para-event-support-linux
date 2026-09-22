"""HTTP 接口契约测试：鉴权、公众脱敏、命令码与幂等头。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler, DEV_TOKEN


def post_json(base, path, payload, token=DEV_TOKEN, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    head = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        head["X-Internal-Token"] = token
    return Request(f"{base}{path}", data=body, headers=head, method="POST")


class HttpApiTest(unittest.TestCase):
    server = None
    thread = None
    base = None

    @classmethod
    def setUpClass(cls):
        # 服务进程只起一次；每个用例开始时重建内存运行时，重置全局状态。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.build_runtime(":memory:")

    def _request(self, method, path, token=DEV_TOKEN, payload=None, headers=None):
        url = f"{self.base}{path}"
        head = headers or {}
        if token is not None:
            head = {**head, "X-Internal-Token": token}
        data = json.dumps(payload).encode() if payload is not None else None
        return Request(url, data=data, headers=head, method=method)

    def _command(self, command, token=DEV_TOKEN, headers=None):
        return urlopen(post_json(self.base, "/v1/commands", command,
                                 token=token, headers=headers), timeout=3)

    def _seed(self):
        self._command({"command": "add_resource", "resource": {
            "id": "van-a", "type": "无障碍车", "city": "甲市",
            "capabilities": ["无障碍车"]}})
        self._command({"command": "add_resource", "resource": {
            "id": "bus-a", "type": "通勤车", "city": "甲市",
            "capabilities": ["通勤车"]}})
        self._command({"command": "register_athlete", "athlete": {
            "id": "p1", "name": "甲队一号", "team": "甲队", "sport_class": "T54",
            "support_needs": ["无障碍车"], "city": "甲市"}})
        self._command({"command": "register_athlete", "athlete": {
            "id": "p2", "name": "乙队二号", "team": "乙队", "sport_class": "F56",
            "support_needs": ["通勤车"], "city": "甲市"}})
        self._command({"command": "plan_leg", "leg": {
            "id": "l1", "athlete_id": "p1", "purpose": "比赛", "event": "田径100米",
            "city": "甲市", "venue": "甲市体育场", "start": "1-09:00", "end": "1-11:00",
            "requirements": ["无障碍车"]}})
        self._command({"command": "plan_leg", "leg": {
            "id": "l2", "athlete_id": "p2", "purpose": "训练",
            "city": "甲市", "venue": "训练馆", "start": "1-14:00", "end": "1-16:00",
            "requirements": ["通勤车"]}})

    def test_health_is_open(self):
        with urlopen(f"{self.base}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["service"], "para-event-support")

    def test_internal_routes_require_token(self):
        for path in ("/v1/internal/audit", "/v1/internal/resources",
                     "/v1/internal/handoff-gaps", "/v1/support/athletes/p1"):
            with self.assertRaises(HTTPError) as error:
                urlopen(self._request("GET", path, token=None), timeout=2)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

    def test_command_with_wrong_token_is_unauthorized(self):
        with self.assertRaises(HTTPError) as error:
            self._command({"command": "register_athlete",
                           "athlete": {"id": "x"}}, token="wrong")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_public_schedule_is_open_and_sanitized(self):
        self._seed()
        with urlopen(f"{self.base}/v1/public/schedule", timeout=2) as response:
            payload = json.load(response)
        body = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("support_needs", body)
        self.assertNotIn("无障碍", body)
        self.assertNotIn("联络员", body)
        # 体育分级作为赛事资格可公开
        self.assertEqual(payload["events"][0]["entries"][0]["sport_class"], "T54")
        # 训练行程不进公众赛程
        self.assertEqual(len(payload["events"]), 1)

    def test_double_booking_returns_409(self):
        self._seed()
        self._command({"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                       "need": "无障碍车", "start": "1-08:30", "end": "1-11:30",
                       "owner": "甲队联络员"})
        with self.assertRaises(HTTPError) as error:
            self._command({"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                           "need": "无障碍车", "start": "1-09:00", "end": "1-10:00"})
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(json.load(error.exception.fp)["error"], "conflict")

    def test_validation_error_returns_400(self):
        with self.assertRaises(HTTPError) as error:
            self._command({"command": "register_athlete",
                           "athlete": {"id": "x", "诊断": "病历"}})
        self.assertEqual(error.exception.code, 400)
        payload = json.load(error.exception.fp)
        self.assertEqual(payload["error"], "validation_error")

    def test_idempotency_key_header_dedupes(self):
        self._seed()
        command = {"command": "assign", "resource_id": "van-a", "leg_id": "l1",
                   "need": "无障碍车", "start": "1-08:30", "end": "1-11:30"}
        with self._command(command, headers={"Idempotency-Key": "net-1"}) as response:
            first = json.load(response)
        with self._command(command, headers={"Idempotency-Key": "net-1"}) as response:
            second = json.load(response)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"]["booking_id"],
                         second["result"]["booking_id"])

    def test_emergency_preemption_is_auditable(self):
        self._seed()
        self._command({"command": "assign", "resource_id": "bus-a", "leg_id": "l2",
                       "need": "通勤车", "start": "1-13:30", "end": "1-16:30",
                       "owner": "乙队联络员"})
        with self._command({"command": "assign", "resource_id": "bus-a",
                            "leg_id": "l1", "need": "通勤车",
                            "start": "1-14:00", "end": "1-15:00",
                            "priority": "emergency", "reason": "送医急救",
                            "now": 500}) as response:
            outcome = json.load(response)
        self.assertTrue(outcome["result"]["displaced"])
        with urlopen(self._request("GET", "/v1/internal/audit?athlete_id=p1")) as r:
            trail = json.load(r)["trail"]
        booked = next(t for t in trail if t["kind"] == "booked")
        self.assertEqual(booked["impact"]["preemption_reason"], "送医急救")
        with urlopen(self._request("GET", "/v1/support/athletes/p2")) as r:
            p2 = json.load(r)
        self.assertEqual(p2["itinerary"][0]["state"], "需改派")

    def test_malformed_json_is_400(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(post_json(self.base, "/v1/commands", None, raw=b"{not json"),
                    timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_unknown_route_is_404(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base}/nope", timeout=2)
        self.assertEqual(error.exception.code, 404)

    def test_default_runtime_persists_to_disk_and_survives_restart(self):
        # 回归：服务接线曾把默认日志路径误传成 None，导致命令成功但从不落盘。
        import os
        import tempfile
        import runtime
        from runtime import Runtime

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = os.path.join(tmp.name, "events.jsonl")
        original = runtime.EVENTS_PATH
        runtime.EVENTS_PATH = log_path
        try:
            service.build_runtime()  # 不传参：必须走磁盘默认路径
            with self._command({"command": "register_athlete", "athlete": {
                    "id": "disk1", "sport_class": "T54",
                    "support_needs": []}}) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertTrue(os.path.exists(log_path))

            # 全新运行时回放同一日志即可看到该登记
            recovered = Runtime(log_path)
            self.assertIn("disk1", recovered.store.athletes)
        finally:
            runtime.EVENTS_PATH = original
            service.build_runtime(":memory:")


if __name__ == "__main__":
    unittest.main()
