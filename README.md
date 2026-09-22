# 残特奥赛事保障

协调三赛区（残运会 + 特奥会）的运动员个人支持：报到、医学分级、训练、比赛、
住宿、交通与跨城交接。系统是保障中心的调度内核——只登记赛事资格与功能支持
所需的最少信息，把场馆无障碍能力、酒店房型、车辆、器材、志愿者资质作为
**有期限的资源**按时间窗安排；出现分级变化、赛程延误或设备故障时只重排
受影响行程。

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 最小知情 | 登记白名单仅 `id/name/team/sport_class/support_needs/city`；含诊断、病历等字段直接拒绝，不接收、不回显、不落盘 |
| 有期限资源 | 车辆/房型/席位/辅具/志愿者均带赛区、能力目录与容量，按 `[start, end)` 半开区间占用；资源赛区须与行程赛区一致（跨城资源除外） |
| 禁止双重承诺 | 重叠窗内占用数超过容量即 `409 conflict`，同一辆无障碍车不会同时承诺给两支队伍 |
| 局部重排 | `classification_change` / `delay_legs` / `equipment_failure` 只触及指明行程的当前及未来占用，其他队伍不受影响 |
| 紧急征用 | 仅可覆盖**普通通勤资源**，无障碍资源受保护；必须带理由，系统自动通知原安排负责人（无负责人则通知赛区调度员），决定与影响全部入审计 |
| 跨城不断档 | 同一运动员相邻两城赛程之间必须存在首尾相接、能力匹配且**已落实占用**的交接行程；未计划或计划了未落实都判为断档 |
| 视图分离 | `GET /v1/public/schedule` 免鉴权但彻底脱敏；支持视图、断档清单、资源台账、审计回看须内部令牌 |
| 可靠运行 | 命令带 `request_id` 或 `Idempotency-Key` 时重复提交回放首次结果；全部变更追加到 `data/events.jsonl`，重启回放恢复；一条命令一个提交点，失败无副作用 |

## HTTP 接口

内部接口需请求头 `X-Internal-Token`（取自环境变量 `SUPPORT_INTERNAL_TOKEN`，
未配置时用仅适用于本地联调的默认值 `local-dev-internal`，生产必须显式配置）。

### 公众（无需令牌）

- `GET /health` — 服务身份与健康
- `GET /v1/public/schedule` — 公众赛程：时间、场馆、参赛名单与**体育分级**
  （赛事资格），不含任何功能支持需要、负责人或改派信息

### 命令（内部，`POST /v1/commands`）

命令为单个 JSON 对象，支持 `request_id` 字段或 `Idempotency-Key` 头实现幂等。

| `command` | 关键参数 | 说明 |
| --- | --- | --- |
| `register_athlete` | `athlete` | 最小信息登记；带诊断字段返回 `400` |
| `add_resource` | `resource` | 登记资源（类型、赛区、能力目录、容量） |
| `plan_leg` | `leg` | 安排个人行程（类型、时间窗、所需支持） |
| `assign` | `resource_id, leg_id, need, start, end, owner` | 按时间窗承诺资源 |
| `release` | `booking_id, reason` | 主动释放占用 |
| `equipment_failure` | `resource_id, at` | 故障：自动释放、同赛区同能力改派，无替代则需改派并通知 |
| `classification_change` | `athlete_id, sport_class, support_needs, at` | 分级变化：只改该运动员当前及未来相关占用 |
| `delay_legs` | `leg_ids[], delta`（分钟）, `at` | 赛程延误：只平移指定行程，冲突的占用单独改派 |
| 紧急版 `assign` | `priority:"emergency", reason, now` | 征用普通通勤资源；自动通知原负责人 |

时间格式支持整数分钟或 `D-HH:MM`（如 `1-08:30` = 第 1 天 08:30）。

### 查询（内部）

- `GET /v1/support/athletes/<id>` — 个人支持视图（行程状态 + 实际安排）
- `GET /v1/internal/handoff-gaps` — 跨城支持断档清单（按到达时限排序）
- `GET /v1/internal/audit?athlete_id=&leg_id=&include_schedule=true` —
  每次安排/改派/征用/通知及其实际影响的回看
- `GET /v1/internal/resources` — 资源台账（含全部占用状态）

## 架构

```
service.py      HTTP 路由、令牌鉴权、错误码（400/401/404/409）
runtime.py      命令分发、JSONL 追加日志、fsync、重启回放、幂等索引、锁与原子提交
scheduling.py   纯领域模型：登记、时间窗占用、容量、局部重排、征用、断档检测、脱敏与审计
```

所有状态变更都先在内存副本上执行，成功才整体提交（应用事件 + 追加日志）；
多事件命令（如故障改派同时释放资源、建立新占用、发出通知）写为同一条日志记录，
保证"命令执行中崩溃不留孤立中间态"。

## 运行与测试

```bash
python3 service.py --check           # 基础检查（领域模型与运行时可装载）
python3 service.py --port 8000       # 启动服务（默认读 data/events.jsonl）
SUPPORT_INTERNAL_TOKEN=xxx python3 service.py
npm test                             # 49 项：领域 27 + 运行时 9 + HTTP 10 + 契约 3
```

`fixtures/domain.json` 保存领域词表（登记白/黑名单、支持需要目录、行程/资源/
状态词汇与核心规则），供接口联调保持语义一致。
