"""运行时：命令处理、追加事件日志、重启回放恢复、消息幂等。

保障中心在重复消息和服务重启中运行，因此：

* 每条外部命令可带 ``request_id`` 或 ``idempotency_key``；重复提交时直接返回
  首次结果，不产生第二次安排（“不把同一辆车同时承诺给两支队伍”的第一道防线）；
* 所有状态变更以事件形式追加写入 JSONL 日志，fsync 后才应答成功；
  重启时按序回放即可恢复全部状态；
* 一条命令对应一个提交点：命令在内存副本上执行，全部成功才整体提交并落盘，
  日志永不出现孤立的中间态。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time

from scheduling import SupportStore, ValidationError, SchedulingError

DATA_DIR = os.environ.get("SUPPORT_DATA_DIR", "data")
EVENTS_PATH = os.path.join(DATA_DIR, "events.jsonl")


class Runtime:
    """包裹领域存储，处理持久化、幂等与命令分发。"""

    def __init__(self, events_path=EVENTS_PATH):
        self.events_path = events_path
        if events_path is not None:
            os.makedirs(os.path.dirname(os.path.abspath(events_path)), exist_ok=True)
        self.lock = threading.RLock()
        self.store = SupportStore()
        self._idem = {}       # idempotency key -> (ok, result)
        self._replies = {}    # request_id -> (ok, result)，供消息重投回放
        self.restored_events = 0
        self._replay()

    # -- 持久化 --------------------------------------------------------------

    def _replay(self):
        if not self.events_path or not os.path.exists(self.events_path):
            return
        with open(self.events_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                envelope = json.loads(line)
                events = envelope.get("events") or (
                    [envelope["event"]] if envelope.get("event") else [])
                for event in events:
                    self.store.apply(event)
                if envelope.get("command_id"):
                    self._replies[envelope["command_id"]] = (
                        envelope["ok"], envelope["result"])
                if envelope.get("idempotency_key"):
                    self._idem[envelope["idempotency_key"]] = (
                        envelope["ok"], envelope["result"])
                self.restored_events += 1

    def _append(self, events, command_id=None, idempotency_key=None, result=None):
        if self.events_path is None:
            return
        envelope = {
            "ts": int(time.time()),
            "command_id": command_id,
            "idempotency_key": idempotency_key,
            "ok": True,
            "result": result,
            "event_count": len(events),
            "event": events[0] if len(events) == 1 else None,
            "events": events if len(events) != 1 else None,
        }
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- 命令执行（一个命令一个提交点）--------------------------------------

    def execute(self, command):
        """执行一条命令并原子提交，返回结果字典。

        重复的 ``request_id`` / ``idempotency_key`` 直接回放首次结果，
        不重复执行任何领域动作。
        """
        if not isinstance(command, dict):
            raise ValidationError("命令必须是 JSON 对象")
        cmd_id = command.get("request_id")
        key = command.get("idempotency_key")
        with self.lock:
            if cmd_id and cmd_id in self._replies:
                ok, result = self._replies[cmd_id]
                return {"replayed": True, "ok": ok, "result": result}
            if key and key in self._idem:
                ok, result = self._idem[key]
                return {"replayed": True, "ok": ok, "result": result}

            # 在副本上执行：成功才提交，失败则主状态完全不受影响。
            committed = self.store
            before = len(committed.events)
            sandbox = copy.deepcopy(self.store)
            self.store = sandbox
            try:
                result = self._dispatch(command)
            finally:
                self.store = committed
            new_events = sandbox.events[before:]

            for event in new_events:
                self.store.apply(event)
            self._append(new_events, command_id=cmd_id, idempotency_key=key, result=result)
            if cmd_id:
                self._replies[cmd_id] = (True, result)
            if key:
                self._idem[key] = (True, result)
            return {"replayed": False, "ok": True, "result": result}

    def _dispatch(self, command):
        kind = command.get("command")
        store = self.store
        if kind == "register_athlete":
            return {"athlete": store.register_athlete(command.get("athlete", command))}
        if kind == "add_resource":
            return {"resource": store.add_resource(command.get("resource", command))}
        if kind == "plan_leg":
            return {"leg": store.plan_leg(command.get("leg", command))}
        if kind == "assign":
            outcome = store.assign(
                command["resource_id"], command["leg_id"], command["need"],
                command["start"], command["end"], owner=command.get("owner"),
                priority=command.get("priority", "normal"),
                reason=command.get("reason"), now=command.get("now", 0))
            return {"booking_id": outcome["booking_id"],
                    "displaced": outcome["displaced"], "state": outcome["state"]}
        if kind == "release":
            state = store.release(command["booking_id"], command.get("reason", ""))
            return {"leg_state": state}
        if kind == "equipment_failure":
            return store.equipment_failure(
                command["resource_id"], command["at"], command.get("reason", "设备故障"))
        if kind == "classification_change":
            return store.classification_change(
                command["athlete_id"], command["sport_class"],
                command["support_needs"], command["at"],
                command.get("reason", "医学分级变化"))
        if kind == "delay_legs":
            return store.delay_legs(
                command["leg_ids"], command["delta"], command["at"],
                command.get("reason", "赛程延误"))
        raise ValidationError(f"未知命令: {kind}")

    # -- 查询（只读，不产生事件）--------------------------------------------

    def query(self, name, **params):
        with self.lock:
            if name == "athlete_support":
                return self.store.athlete_support_view(params["athlete_id"])
            if name == "public_schedule":
                return {"events": self.store.public_schedule_view()}
            if name == "handoff_gaps":
                return {"gaps": self.store.handoff_gaps()}
            if name == "audit":
                return {"trail": self.store.audit_trail(
                    athlete_id=params.get("athlete_id"),
                    leg_id=params.get("leg_id"),
                    include_schedule=params.get("include_schedule", False))}
            if name == "resources":
                return {"resources": [
                    {**r, "bookings": [
                        {"booking_id": b["id"], "leg_id": b["leg_id"], "need": b["need"],
                         "start": b["start"], "end": b["end"], "status": b["status"]}
                        for b in self.store.bookings.values()
                        if b["resource_id"] == r["id"]]}
                    for r in sorted(self.store.resources.values(), key=lambda r: r["id"])]}
            raise ValidationError(f"未知查询: {name}")
