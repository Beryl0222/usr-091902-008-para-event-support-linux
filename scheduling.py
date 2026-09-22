"""残特奥赛事保障中心的调度领域模型。

设计原则（对应保障中心章程）：

* 最小知情：只登记赛事资格（体育分级）与功能支持需要，拒收任何诊断信息；
* 有期限资源：车辆、酒店房型、场馆无障碍位、器材、志愿者均按时间窗占用；
* 局部重排：分级变化、延误、故障只动受影响行程，其余承诺保持不变；
* 紧急征用：可优先占用普通通勤资源，但须记录理由并通知原安排负责人；
* 跨城连续：跨城交接必须有可核验的接续支持链，不允许断档；
* 视图分离：公众视图只含赛程资格信息，支持需要与改派影响仅内部可见。

本模块只负责纯领域逻辑，不做 IO；所有状态变更都以事件形式产出，
由 ``runtime`` 追加日志并在重启后回放。
"""

from __future__ import annotations

# ----------------------------------------------------------------------------
# 基础类型
# ----------------------------------------------------------------------------


class SchedulingError(Exception):
    """领域错误基类。"""

    code = "scheduling_error"


class ValidationError(SchedulingError):
    code = "validation_error"


class ConflictError(SchedulingError):
    """资源在该时间窗已被承诺，且本次安排不能覆盖既有承诺。"""

    code = "conflict"


# 注册时允许登记的全部字段——白名单本身即“最少信息”边界。
ATHLETE_FIELDS = {"id", "name", "team", "sport_class", "support_needs", "city"}
# 任何疑似诊断/病历的字段名都直接拒绝，不进入存储也不进入日志。
FORBIDDEN_KEYS = {"diagnosis", "诊断", "medical_history", "病历", "condition_detail", "病情"}

# 合法的功能支持需要代码（功能支持，不等于诊断）。
NEED_CODES = {"无障碍车", "通勤车", "无障碍房", "无障碍席位", "助行辅具", "手语志愿", "陪护志愿"}

PURPOSES = {"报到", "医学分级", "训练", "比赛", "住宿", "交通", "跨城交接"}

# 紧急征用只允许覆盖普通通勤资源，无障碍资源不在此列。
EMERGENCY_PREEMPTABLE = {"通勤车"}


def to_minutes(value):
    """把时间换算为整数分钟：接受 int、``HH:MM`` 或 ``D-HH:MM``。"""
    if isinstance(value, bool):
        raise ValidationError("时间格式不正确")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        day = 0
        if "-" in text:
            head, text = text.split("-", 1)
            day = int(head)
        hour, minute = text.split(":")
        return day * 1440 + int(hour) * 60 + int(minute)
    raise ValidationError("时间格式不正确")


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


# ----------------------------------------------------------------------------
# 核心存储：所有变更经事件应用，便于回放与审计
# ----------------------------------------------------------------------------


class SupportStore:
    def __init__(self):
        self.resources = {}          # rid -> resource
        self.athletes = {}           # aid -> athlete
        self.legs = {}               # leg_id -> leg
        self.bookings = {}           # booking_id -> booking
        self.notifications = []      # 通知原安排负责人的记录
        self.events = []             # 已应用事件（运行时据此持久化）
        # 单条命令内已选定、尚未随总结事件落地的暂定占用，
        # 防止同一次故障/延误把多支队伍改派到同一替代资源。
        self._tentative = []
        self._seq = 0

    # -- 事件应用 ------------------------------------------------------------

    def apply(self, event):
        kind = event["kind"]
        if kind == "resource_added":
            self.resources[event["resource"]["id"]] = dict(event["resource"], status="ready")
        elif kind == "athlete_registered":
            self.athletes[event["athlete"]["id"]] = dict(event["athlete"])
        elif kind == "leg_planned":
            self.legs[event["leg"]["id"]] = dict(event["leg"])
        elif kind == "booked":
            self.bookings[event["booking_id"]] = {
                "id": event["booking_id"],
                "resource_id": event["resource_id"],
                "leg_id": event["leg_id"],
                "need": event["need"],
                "start": event["start"],
                "end": event["end"],
                "priority": event.get("priority", "normal"),
                "owner": event.get("owner"),
                "reason": event.get("reason"),
                "status": "active",
            }
            for displaced_id in event.get("displaced", []):
                self.bookings[displaced_id]["status"] = "displaced"
        elif kind == "booking_released":
            self.bookings[event["booking_id"]]["status"] = "released"
            self.bookings[event["booking_id"]]["release_reason"] = event.get("reason")
        elif kind == "resource_failed":
            self.resources[event["resource_id"]]["status"] = "failed"
            for booking_id in event.get("released", []):
                self.bookings[booking_id]["status"] = "released"
            for booking in event.get("reassigned", []):
                self.bookings[booking["id"]] = dict(booking, status="active")
        elif kind == "classification_changed":
            athlete = self.athletes[event["athlete_id"]]
            athlete["sport_class"] = event["sport_class"]
            athlete["support_needs"] = list(event["support_needs"])
            for leg_id, requirements in event.get("leg_requirements", {}).items():
                if leg_id in self.legs:
                    self.legs[leg_id]["requirements"] = list(requirements)
            for booking_id in event.get("cancelled", []):
                self.bookings[booking_id]["status"] = "released"
            for booking in event.get("added", []):
                self.bookings[booking["id"]] = dict(booking, status="active")
        elif kind == "legs_delayed":
            moved = dict(zip(event["leg_ids"], event["new_windows"]))
            for leg_id, (start, end) in moved.items():
                self.legs[leg_id]["start"] = start
                self.legs[leg_id]["end"] = end
            for booking_id, (start, end) in event.get("shifted_bookings", {}).items():
                booking = self.bookings[booking_id]
                booking["start"], booking["end"] = start, end
            for booking_id in event.get("dropped", []):
                self.bookings[booking_id]["status"] = "released"
            for booking in event.get("rerouted", []):
                self.bookings[booking["id"]] = dict(booking, status="active")
        elif kind == "notified":
            self.notifications.append({
                "to": event["to"],
                "message": event["message"],
                "at": event["at"],
                "context": event.get("context", {}),
            })
        else:
            raise SchedulingError(f"未知事件类型: {kind}")
        self.events.append(event)
        self._seq = max(self._seq, int(event.get("seq", 0)))

    def _next_id(self, prefix):
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _emit(self, kind, **payload):
        event = {"seq": self._seq + 1, "kind": kind, **payload}
        self.apply(event)
        return event

    # -- 登记：最小信息边界 --------------------------------------------------

    def register_athlete(self, data):
        extra = set(data) - ATHLETE_FIELDS
        leaked = extra & FORBIDDEN_KEYS
        if leaked:
            # 诊断信息绝不接收：不保存、不回显、不写日志。
            raise ValidationError("保障中心不登记诊断信息，只需赛事资格与功能支持需要")
        if extra:
            raise ValidationError(f"超出最小登记范围的字段: {sorted(extra)}")
        athlete_id = data.get("id")
        if not athlete_id:
            raise ValidationError("缺少运动员 id")
        if athlete_id in self.athletes:
            raise ValidationError(f"运动员已登记: {athlete_id}")
        needs = data.get("support_needs", [])
        unknown = sorted(set(needs) - NEED_CODES)
        if unknown:
            raise ValidationError(f"未知的功能支持需要: {unknown}")
        athlete = {
            "id": athlete_id,
            "name": data.get("name", athlete_id),
            "team": data.get("team", "未注明代表队"),
            "sport_class": data.get("sport_class"),
            "support_needs": list(needs),
            "city": data.get("city"),
        }
        self._emit("athlete_registered", athlete=athlete)
        return athlete

    def add_resource(self, data):
        rid = data.get("id")
        if not rid:
            raise ValidationError("缺少资源 id")
        if rid in self.resources:
            raise ValidationError(f"资源已存在: {rid}")
        caps = data.get("capabilities", [])
        unknown = sorted(set(caps) - NEED_CODES)
        if unknown:
            raise ValidationError(f"资源能力不在支持需要目录内: {unknown}")
        resource = {
            "id": rid,
            "type": data.get("type", "未分类"),
            "city": data.get("city"),
            "capabilities": list(caps),
            "capacity": int(data.get("capacity", 1)),
        }
        self._emit("resource_added", resource=resource)
        return resource

    def plan_leg(self, data):
        leg_id = data.get("id")
        if not leg_id:
            raise ValidationError("缺少行程 id")
        if leg_id in self.legs:
            raise ValidationError(f"行程已存在: {leg_id}")
        athlete_id = data.get("athlete_id")
        if athlete_id not in self.athletes:
            raise ValidationError(f"运动员未登记: {athlete_id}")
        purpose = data.get("purpose")
        if purpose not in PURPOSES:
            raise ValidationError(f"未知行程类型: {purpose}")
        start, end = to_minutes(data["start"]), to_minutes(data["end"])
        if start >= end:
            raise ValidationError("行程结束时间必须晚于开始时间")
        reqs = data.get("requirements", [])
        unknown = sorted(set(reqs) - NEED_CODES)
        if unknown:
            raise ValidationError(f"行程要求了未知支持需要: {unknown}")
        leg = {
            "id": leg_id,
            "athlete_id": athlete_id,
            "event": data.get("event"),
            "purpose": purpose,
            "city": data.get("city"),
            "venue": data.get("venue"),
            "start": start,
            "end": end,
            "requirements": list(reqs),
            "from_city": data.get("from_city"),
            "to_city": data.get("to_city"),
        }
        self._emit("leg_planned", leg=leg)
        return leg

    # -- 查询 ----------------------------------------------------------------

    def _active_bookings(self, resource_id, start, end, exclude=()):
        result = []
        for booking in self.bookings.values():
            if booking["id"] in exclude or booking["resource_id"] != resource_id:
                continue
            if booking["status"] != "active":
                continue
            if _overlaps(start, end, booking["start"], booking["end"]):
                result.append(booking)
        return result

    def _resource_load_ok(self, resource, start, end, exclude=()):
        if resource.get("status") == "failed":
            return False
        active = self._active_bookings(resource["id"], start, end, exclude)
        tent = [
            t for t in self._tentative
            if t["resource_id"] == resource["id"]
            and t["id"] not in exclude
            and _overlaps(start, end, t["start"], t["end"])
        ]
        return len(active) + len(tent) < resource.get("capacity", 1)

    def _leg_active_bookings(self, leg_id):
        return [b for b in self.bookings.values()
                if b["leg_id"] == leg_id and b["status"] == "active"]

    def leg_state(self, leg):
        """返回行程的保障状态：保障中 / 需改派 / 待安排。"""
        if not leg["requirements"]:
            return "保障中"
        covered = {b["need"] for b in self._leg_active_bookings(leg["id"])}
        if set(leg["requirements"]) <= covered:
            return "保障中"
        if covered:
            return "需改派"
        bookings = list(self.bookings.values())
        ever = any(b["leg_id"] == leg["id"] for b in bookings)
        return "需改派" if ever else "待安排"

    def athlete_support_view(self, athlete_id):
        """内部视图：个人支持行程与实际资源安排。"""
        athlete = self.athletes.get(athlete_id)
        if not athlete:
            raise ValidationError(f"运动员未登记: {athlete_id}")
        legs = sorted(
            (leg for leg in self.legs.values() if leg["athlete_id"] == athlete_id),
            key=lambda leg: (leg["start"], leg["id"]),
        )
        return {
            "athlete": {"id": athlete["id"], "name": athlete["name"], "team": athlete["team"],
                        "sport_class": athlete["sport_class"], "support_needs": athlete["support_needs"]},
            "itinerary": [
                {
                    "leg_id": leg["id"], "purpose": leg["purpose"], "city": leg["city"],
                    "venue": leg["venue"], "start": leg["start"], "end": leg["end"],
                    "requirements": leg["requirements"],
                    "state": self.leg_state(leg),
                    "assignments": [
                        {"resource_id": b["resource_id"], "need": b["need"],
                         "start": b["start"], "end": b["end"], "priority": b["priority"],
                         "owner": b["owner"]}
                        for b in sorted(self._leg_active_bookings(leg["id"]), key=lambda b: b["start"])
                    ],
                }
                for leg in legs
            ],
        }

    def public_schedule_view(self):
        """公众视图：赛程与资格信息，不含任何功能支持需要。"""
        events = {}
        for leg in self.legs.values():
            if leg["purpose"] != "比赛":
                continue
            key = leg.get("event") or f"event@{leg['venue']}"
            item = events.setdefault(key, {
                "event": key, "city": leg["city"], "venue": leg["venue"],
                "start": leg["start"], "end": leg["end"], "entries": [],
            })
            item["start"] = min(item["start"], leg["start"])
            item["end"] = max(item["end"], leg["end"])
            athlete = self.athletes[leg["athlete_id"]]
            # 体育分级属赛事资格，公开发布；支持需要、联系人、备注一律不含。
            item["entries"].append({
                "athlete_id": athlete["id"], "name": athlete["name"],
                "team": athlete["team"], "sport_class": athlete["sport_class"],
            })
        return sorted(events.values(), key=lambda item: (item["start"], item["event"]))

    # -- 安排：时间窗承诺 ----------------------------------------------------

    def _find_alternative(self, need, city, start, end, exclude_resource=()):
        candidates = []
        for resource in self.resources.values():
            if resource["id"] in exclude_resource:
                continue
            if resource.get("status") == "failed" or need not in resource["capabilities"]:
                continue
            if city and resource.get("city") and resource["city"] != city:
                continue
            if self._resource_load_ok(resource, start, end):
                candidates.append(resource["id"])
        return sorted(candidates)[0] if candidates else None

    def _notify(self, to, message, at, context):
        self._emit("notified", to=to, message=message, at=at, context=context)

    def assign(self, resource_id, leg_id, need, start, end, owner=None,
               priority="normal", reason=None, now=0):
        resource = self.resources.get(resource_id)
        if not resource:
            raise ValidationError(f"资源不存在: {resource_id}")
        leg = self.legs.get(leg_id)
        if not leg:
            raise ValidationError(f"行程不存在: {leg_id}")
        if need not in leg["requirements"] and priority != "emergency":
            raise ValidationError(f"行程 {leg_id} 不需要 {need}")
        if need not in resource["capabilities"]:
            raise ValidationError(f"资源 {resource_id} 不具备 {need} 能力")
        if resource.get("city") and leg.get("city") and resource["city"] != leg["city"]:
            raise ValidationError(
                f"资源 {resource_id} 属 {resource['city']} 赛区，不能承诺给 {leg['city']} 赛区的行程")
        start, end = to_minutes(start), to_minutes(end)
        if start >= end:
            raise ValidationError("占用时间窗无效")

        # 容量未满载时可直接共用（如多座位通勤班车）；满载才构成承诺冲突。
        conflicts = sorted(self._active_bookings(resource_id, start, end),
                           key=lambda b: (b["start"], b["id"]))
        capacity = resource.get("capacity", 1)
        overflow = len(conflicts) - capacity + 1
        displaced = []
        if overflow > 0:
            if priority != "emergency":
                raise ConflictError(
                    f"资源 {resource_id} 在该时间窗已承诺给其他队伍，不能双重承诺")
            # 紧急情况只可覆盖普通通勤资源；无障碍资源受保护。
            if not (set(resource["capabilities"]) <= EMERGENCY_PREEMPTABLE):
                raise ConflictError("紧急征用仅限普通通勤资源，无障碍资源不得覆盖")
            if not reason:
                raise ValidationError("紧急征用必须说明理由")
            # 只征用腾出一个名额所必需的最少占用，其余同行安排不受影响。
            for booking in conflicts[:overflow]:
                displaced.append(booking["id"])
        conflicts = [b for b in conflicts if b["id"] in displaced]

        booking_id = self._next_id("b")
        event = self._emit(
            "booked", booking_id=booking_id, resource_id=resource_id, leg_id=leg_id,
            need=need, start=start, end=end, priority=priority, owner=owner,
            reason=reason, displaced=displaced,
        )
        # 覆盖决定须通知原安排负责人；找不到负责人时上报赛区调度员。
        for booking in conflicts:
            original_owner = booking.get("owner") or "赛区调度员"
            self._notify(
                original_owner,
                f"您负责的 {booking['need']} 安排（行程 {booking['leg_id']}）因紧急医疗被征用",
                at=now,
                context={"booking_id": booking["id"], "leg_id": booking["leg_id"],
                         "resource_id": resource_id,
                         "emergency_booking_id": booking_id, "reason": reason,
                         "window": [start, end]},
            )
        return {"event": event, "booking_id": booking_id, "displaced": displaced,
                "state": self.leg_state(leg)}

    def release(self, booking_id, reason=""):
        booking = self.bookings.get(booking_id)
        if not booking:
            raise ValidationError(f"安排不存在: {booking_id}")
        if booking["status"] != "active":
            raise ValidationError(f"安排已处于 {booking['status']} 状态")
        self._emit("booking_released", booking_id=booking_id, reason=reason)
        return self.leg_state(self.legs[booking["leg_id"]])

    # -- 局部重排 ------------------------------------------------------------

    def equipment_failure(self, resource_id, at, reason="设备故障"):
        resource = self.resources.get(resource_id)
        if not resource:
            raise ValidationError(f"资源不存在: {resource_id}")
        at = to_minutes(at)
        affected = [
            b for b in self._active_bookings(resource_id, at, 10**12)
            if b["end"] > at
        ]
        self._tentative = []
        released_ids, reassigned, stranded = [], [], []
        for booking in sorted(affected, key=lambda b: b["start"]):
            released_ids.append(booking["id"])
            leg = self.legs[booking["leg_id"]]
            alt_id = self._find_alternative(
                booking["need"], leg.get("city"), booking["start"], booking["end"],
                exclude_resource=(resource_id,))
            if alt_id:
                new_id = self._next_id("b")
                replacement = {
                    "id": new_id, "resource_id": alt_id, "leg_id": leg["id"],
                    "need": booking["need"], "start": booking["start"], "end": booking["end"],
                    "priority": "normal", "owner": booking.get("owner"),
                    "reason": f"由 {resource_id} 故障改派",
                }
                self._tentative.append(replacement)
                reassigned.append(replacement)
            else:
                stranded.append(leg["id"])
            self._notify(
                booking.get("owner") or "赛区调度员",
                f"资源 {resource_id} 发生故障，行程 {leg['id']} 的 {booking['need']} 已"
                + (f"改派至 {alt_id}" if alt_id else "暂无替代资源，需改派"),
                at=at,
                context={"resource_id": resource_id, "booking_id": booking["id"],
                         "leg_id": leg["id"], "replacement": alt_id, "reason": reason},
            )
        self._emit("resource_failed", resource_id=resource_id, at=at,
                   released=released_ids, reassigned=reassigned, reason=reason)
        return {"released": released_ids,
                "reassigned": [b["leg_id"] for b in reassigned],
                "needs_replan": sorted(set(stranded))}

    def classification_change(self, athlete_id, sport_class, support_needs, at,
                              reason="医学分级变化"):
        athlete = self.athletes.get(athlete_id)
        if not athlete:
            raise ValidationError(f"运动员未登记: {athlete_id}")
        unknown = sorted(set(support_needs) - NEED_CODES)
        if unknown:
            raise ValidationError(f"未知的功能支持需要: {unknown}")
        at = to_minutes(at)
        new_needs = list(support_needs)
        removed = set(athlete["support_needs"]) - set(new_needs)
        added = set(new_needs) - set(athlete["support_needs"])

        cancelled, added_bookings, affected = [], [], []
        requirement_changes = {}
        self._tentative = []
        # 仅触及尚未开始的行程；已经完成的安排保持原样。
        future_legs = [leg for leg in self.legs.values()
                       if leg["athlete_id"] == athlete_id and leg["start"] >= at]
        for leg in future_legs:
            # 行程的支持要求随分级变化对齐：仅移除本次被撤下的需要，补入新增需要，
            # 行程原有的其他要求保持不变。
            updated_reqs = [r for r in leg["requirements"] if r not in removed]
            for need in added:
                if need not in updated_reqs:
                    updated_reqs.append(need)
            if updated_reqs != leg["requirements"]:
                requirement_changes[leg["id"]] = updated_reqs
            for booking in self._leg_active_bookings(leg["id"]):
                if booking["need"] in removed and booking["need"] not in new_needs:
                    cancelled.append(booking["id"])
                    affected.append(leg["id"])
            # 新出现的支持需要：自动寻找同赛区同能力资源，找不到则该行程标记需改派。
            for need in added:
                already = any(b["need"] == need for b in self._leg_active_bookings(leg["id"]))
                if already or need not in updated_reqs:
                    continue
                alt_id = self._find_alternative(need, leg.get("city"), leg["start"], leg["end"])
                if alt_id:
                    new_booking = {
                        "id": self._next_id("b"), "resource_id": alt_id, "leg_id": leg["id"],
                        "need": need, "start": leg["start"], "end": leg["end"],
                        "priority": "normal", "owner": None,
                        "reason": f"分级变化（{sport_class}）后补配",
                    }
                    self._tentative.append(new_booking)
                    added_bookings.append(new_booking)
                affected.append(leg["id"])
        self._emit("classification_changed", athlete_id=athlete_id,
                   sport_class=sport_class, support_needs=new_needs,
                   cancelled=cancelled, added=added_bookings,
                   leg_requirements=requirement_changes,
                   reason=reason, at=at)
        return {"athlete_id": athlete_id, "cancelled": cancelled,
                "added": [b["leg_id"] for b in added_bookings],
                "affected_legs": sorted(set(affected)), "added_needs": sorted(added),
                "removed_needs": sorted(removed)}

    def delay_legs(self, leg_ids, delta, at, reason="赛程延误"):
        """只平移给定行程及其占用；其他队伍的安排一律不动。"""
        at = to_minutes(at)
        delta = int(delta)
        legs, new_windows = [], []
        seen = set()
        for leg_id in leg_ids:
            leg = self.legs.get(leg_id)
            if not leg:
                raise ValidationError(f"行程不存在: {leg_id}")
            if leg["end"] <= at:
                raise ValidationError(f"已结束行程不能延误改排: {leg_id}")
            if leg_id in seen:
                continue
            seen.add(leg_id)
            legs.append(leg)
            new_windows.append((leg["start"] + delta, leg["end"] + delta))

        shifted, dropped, rerouted = {}, [], []
        self._tentative = []
        for leg, (new_start, new_end) in zip(legs, new_windows):
            for booking in self._leg_active_bookings(leg["id"]):
                b_start, b_end = booking["start"] + delta, booking["end"] + delta
                rid = booking["resource_id"]
                clash = self._active_bookings(rid, b_start, b_end, exclude=(booking["id"],))
                if not clash and self.resources[rid].get("status") != "failed":
                    shifted[booking["id"]] = (b_start, b_end)
                    continue
                # 平移后冲突（只可能是第三方占用）：仅改派这一条，不波及其他行程。
                dropped.append(booking["id"])
                alt_id = self._find_alternative(
                    booking["need"], leg.get("city"), b_start, b_end,
                    exclude_resource=(rid,))
                if alt_id:
                    new_booking = {
                        "id": self._next_id("b"), "resource_id": alt_id,
                        "leg_id": leg["id"], "need": booking["need"],
                        "start": b_start, "end": b_end, "priority": "normal",
                        "owner": booking.get("owner"),
                        "reason": f"赛程延误后与 {rid} 冲突，改派",
                    }
                    self._tentative.append(new_booking)
                    rerouted.append(new_booking)
                    self._notify(
                        booking.get("owner") or "赛区调度员",
                        f"行程 {leg['id']} 延误后 {rid} 时间冲突，已改派至 {alt_id}",
                        at=at,
                        context={"leg_id": leg["id"], "resource_id": rid,
                                 "replacement": alt_id, "reason": reason},
                    )
        self._emit("legs_delayed", leg_ids=[leg["id"] for leg in legs],
                   new_windows=new_windows, shifted_bookings=shifted,
                   dropped=dropped, rerouted=rerouted, reason=reason, at=at)
        return {"delayed_legs": [leg["id"] for leg in legs], "delta": delta,
                "shifted_bookings": sorted(shifted), "rerouted": [b["leg_id"] for b in rerouted],
                "needs_replan": sorted({self.bookings[b]["leg_id"] for b in dropped
                                        if not any(r["leg_id"] == self.bookings[b]["leg_id"]
                                                   for r in rerouted)})}

    # -- 跨城交接 ------------------------------------------------------------

    def handoff_gaps(self):
        """检查相邻跨城行程之间是否存在个人支持断档。

        规则：同一运动员按时间排序的行程中，相邻两段所在城市不同，
        中间必须存在一座“跨城交接/交通”桥接行程，时间上首尾相接，
        且桥接行程声明的每一项支持需要都有有效占用；否则记一处断档。
        """
        gaps = []
        by_athlete = {}
        for leg in self.legs.values():
            by_athlete.setdefault(leg["athlete_id"], []).append(leg)
        for athlete_id, legs in by_athlete.items():
            ordered = sorted(legs, key=lambda leg: (leg["start"], leg["end"]))
            # 跨城交接/交通行程本身不设赛区城市，因此按“有城市的行程”配对，
            # 让桥接行程可以夹在两段之间被检查。
            located = [leg for leg in ordered if leg.get("city")]
            for prev, nxt in zip(located, located[1:]):
                if prev["city"] == nxt["city"]:
                    continue
                bridges = [
                    leg for leg in legs
                    if leg["purpose"] in ("跨城交接", "交通")
                    and leg["start"] >= prev["start"] and leg["end"] <= nxt["end"]
                    and {leg.get("from_city"), leg.get("to_city")} == {prev["city"], nxt["city"]}
                ]
                covered = None
                for bridge in bridges:
                    covered_needs = {b["need"] for b in self._leg_active_bookings(bridge["id"])}
                    if set(bridge["requirements"]) <= covered_needs:
                        covered = bridge
                        break
                if covered is None:
                    gaps.append({
                        "athlete_id": athlete_id,
                        "from_city": prev["city"], "to_city": nxt["city"],
                        "after_leg": prev["id"], "before_leg": nxt["id"],
                        "arrival_deadline": nxt["start"],
                        "reason": "跨城交接缺少已落实的接续支持",
                    })
        return sorted(gaps, key=lambda gap: (gap["arrival_deadline"], gap["athlete_id"]))

    # -- 审计：回看每次改派及其实际影响 --------------------------------------

    # 内部审计可见的事件类型；登记事件不在逐条审计重点之列，但同样保留在日志中。
    _CHANGE_KINDS = {
        "booked", "booking_released", "resource_failed",
        "classification_changed", "legs_delayed", "notified",
    }

    def audit_trail(self, athlete_id=None, leg_id=None, include_schedule=False):
        """内部回看：按时间顺序列出每次改派/征用及其实际影响。

        可选按运动员或行程过滤；``include_schedule`` 时一并附上常规占用，
        形成完整承诺历史。输出只含功能支持与安排事实，不含诊断。
        """
        trail = []
        for event in self.events:
            kind = event["kind"]
            if kind not in self._CHANGE_KINDS and not (
                    include_schedule and kind in ("leg_planned", "athlete_registered")):
                continue
            impact = self._event_impact(event)
            if athlete_id and impact["athlete_id"] != athlete_id:
                continue
            if leg_id and leg_id not in impact["leg_ids"]:
                continue
            trail.append({
                "seq": event["seq"], "kind": kind, "at": event.get("at"),
                "reason": event.get("reason"),
                "actor": event.get("owner"),
                "impact": impact["detail"],
                "athlete_id": impact["athlete_id"],
                "leg_ids": impact["leg_ids"],
            })
        return trail

    def _event_impact(self, event):
        kind = event["kind"]
        if kind == "booked":
            leg = self.legs.get(event["leg_id"], {})
            detail = {
                "booking_id": event["booking_id"], "resource_id": event["resource_id"],
                "need": event["need"], "window": [event["start"], event["end"]],
                "priority": event.get("priority", "normal"),
            }
            if event.get("displaced"):
                detail["preempted_bookings"] = event["displaced"]
                detail["preemption_reason"] = event.get("reason")
            return {"athlete_id": leg.get("athlete_id"),
                    "leg_ids": [event["leg_id"]], "detail": detail}
        if kind == "booking_released":
            leg_id = self.bookings[event["booking_id"]]["leg_id"]
            return {"athlete_id": self.legs[leg_id].get("athlete_id"), "leg_ids": [leg_id],
                    "detail": {"booking_id": event["booking_id"]}}
        if kind == "resource_failed":
            leg_ids = [self.bookings[b]["leg_id"] for b in event.get("released", [])]
            leg_ids += [b["leg_id"] for b in event.get("reassigned", [])]
            athlete_ids = {self.legs[l]["athlete_id"] for l in leg_ids}
            return {"athlete_id": next(iter(athlete_ids), None) if len(athlete_ids) == 1 else None,
                    "leg_ids": sorted(set(leg_ids)),
                    "detail": {"resource_id": event["resource_id"],
                               "released": event.get("released", []),
                               "reassigned": [b["id"] for b in event.get("reassigned", [])],
                               "replacement_resources":
                                   [b["resource_id"] for b in event.get("reassigned", [])]}}
        if kind == "classification_changed":
            leg_ids = [self.bookings[b]["leg_id"] for b in event.get("cancelled", [])]
            leg_ids += [b["leg_id"] for b in event.get("added", [])]
            leg_ids += list(event.get("leg_requirements", {}))
            return {"athlete_id": event["athlete_id"], "leg_ids": sorted(set(leg_ids)),
                    "detail": {"sport_class": event["sport_class"],
                               "cancelled": event.get("cancelled", []),
                               "added": [b["id"] for b in event.get("added", [])],
                               "requirement_changes": event.get("leg_requirements", {})}}
        if kind == "legs_delayed":
            leg_ids = list(event["leg_ids"])
            athlete_ids = {self.legs[l]["athlete_id"] for l in leg_ids}
            return {"athlete_id": next(iter(athlete_ids), None) if len(athlete_ids) == 1 else None,
                    "leg_ids": leg_ids,
                    "detail": {"new_windows": event["new_windows"],
                               "shifted_bookings": sorted(event.get("shifted_bookings", {})),
                               "rerouted": [b["id"] for b in event.get("rerouted", [])]}}
        if kind == "notified":
            ctx = event.get("context", {})
            leg_ids = [ctx["leg_id"]] if "leg_id" in ctx else []
            athlete_id = None
            if leg_ids:
                athlete_id = self.legs[leg_ids[0]]["athlete_id"]
            return {"athlete_id": athlete_id, "leg_ids": leg_ids,
                    "detail": {"to": event["to"], "message": event["message"],
                               "context": ctx}}
        return {"athlete_id": None, "leg_ids": [], "detail": {}}
