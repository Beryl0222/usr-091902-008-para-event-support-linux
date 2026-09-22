"""残特奥赛事保障中心 HTTP 入口。

路由：

* ``GET  /health``                     运维巡检，无需鉴权
* ``GET  /v1/public/schedule``         公众赛程（脱敏，不含功能支持需要）
* ``POST /v1/commands``                内部命令（登记/安排/重排/征用）
* ``GET  /v1/support/athletes/<id>``   个人支持视图（内部）
* ``GET  /v1/internal/handoff-gaps``   跨城交接断档检查（内部）
* ``GET  /v1/internal/audit``          改派与征用审计回看（内部）
* ``GET  /v1/internal/resources``      资源占用台账（内部）

内部接口需携带 ``X-Internal-Token``，令牌取环境变量 ``SUPPORT_INTERNAL_TOKEN``，
未配置时使用仅限本地联调的默认值，生产必须显式配置。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from runtime import Runtime
from scheduling import ValidationError, ConflictError

SERVICE_ID = "para-event-support"
SERVICE_NAME = "残特奥赛事保障"
DEV_TOKEN = "local-dev-internal"

RUNTIME = None


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _internal_token():
    return os.environ.get("SUPPORT_INTERNAL_TOKEN", DEV_TOKEN)


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、公众脱敏视图与内部保障接口。"""

    server_version = "ParaEventSupport/1.0"

    # -- 基础工具 ------------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("X-Internal-Token") == _internal_token()

    def _require_auth(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized",
                                  "message": "内部接口需要 X-Internal-Token"})
            return False
        return True

    def log_message(self, *_args):
        return

    # -- GET -----------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") if parsed.path != "/" else parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path == "/v1/public/schedule":
                self._send_json(200, RUNTIME.query("public_schedule"))
                return
            if path.startswith("/v1/support/athletes/"):
                if not self._require_auth():
                    return
                athlete_id = path.rsplit("/", 1)[1]
                self._send_json(200, RUNTIME.query(
                    "athlete_support", athlete_id=athlete_id))
                return
            if path == "/v1/internal/handoff-gaps":
                if not self._require_auth():
                    return
                self._send_json(200, RUNTIME.query("handoff_gaps"))
                return
            if path == "/v1/internal/audit":
                if not self._require_auth():
                    return
                params = {k: query[k] for k in ("athlete_id", "leg_id") if k in query}
                params["include_schedule"] = query.get("include_schedule") == "true"
                self._send_json(200, RUNTIME.query("audit", **params))
                return
            if path == "/v1/internal/resources":
                if not self._require_auth():
                    return
                self._send_json(200, RUNTIME.query("resources"))
                return
            self._send_json(404, {"error": "not_found", "message": "未知路由"})
        except ValidationError as exc:
            self._send_json(400, {"error": exc.code, "message": str(exc)})

    # -- POST ----------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") if parsed.path != "/" else parsed.path
        if path != "/v1/commands":
            self._send_json(404, {"error": "not_found", "message": "未知路由"})
            return
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            command = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "validation_error",
                                  "message": "请求体必须是 UTF-8 JSON 对象"})
            return
        if self.headers.get("Idempotency-Key") and "idempotency_key" not in command:
            command["idempotency_key"] = self.headers["Idempotency-Key"]
        if self.headers.get("X-Request-Id") and "request_id" not in command:
            command["request_id"] = self.headers["X-Request-Id"]
        try:
            outcome = RUNTIME.execute(command)
        except ConflictError as exc:
            self._send_json(409, {"error": exc.code, "message": str(exc)})
            return
        except ValidationError as exc:
            self._send_json(400, {"error": exc.code, "message": str(exc)})
            return
        self._send_json(200, outcome)


def build_runtime(events_path=None):
    """构建运行时。

    ``None``：使用 runtime 模块默认日志路径（受 ``SUPPORT_DATA_DIR`` 影响）；
    ``":memory:"``：纯内存模式，供测试使用。
    """
    global RUNTIME
    from runtime import EVENTS_PATH
    RUNTIME = Runtime(None if events_path == ":memory:" else
                      events_path or EVENTS_PATH)
    return RUNTIME


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--events", default=None, help="事件日志路径（默认 data/events.jsonl）")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 不落盘的空跑检查：领域模型与运行时可正常装载。
        Runtime(events_path=None)
        print("基础检查通过")
        return
    build_runtime(args.events)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
