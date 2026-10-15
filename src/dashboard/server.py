"""本地只读网页面板的服务端（Python 标准库，零第三方依赖）。

红线落在代码结构上：
  - **只实现 `do_GET`**，不实现任何写操作 handler（PUT / DELETE 之类一律没有）
  - 路由表里只有行情/告警的读取接口，不存在任何交易或推送动作
  - 默认只监听 127.0.0.1，不对外网暴露
  - 静态文件服务做路径穿越防护（拒绝 `../` 跑到 web 目录之外）
  - `inst_id` 走配置白名单校验后才向上游取数，避免被当成任意 URL 参数用

这一层不做任何计算，只把 `DashboardService` 的结果序列化成 JSON。
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import BARS, DEFAULT_BAR, DashboardService

log = logging.getLogger(__name__)

INDEX_FILE = "index.html"


class DashboardServer(ThreadingHTTPServer):
    """把 service 与 web 目录挂在 server 上，handler 通过 self.server 取。"""

    daemon_threads = True
    allow_reuse_address = False  # 端口被占用时明确报错，不静默换端口

    def __init__(self, address: tuple[str, int], service: DashboardService,
                 web_root: Path) -> None:
        self.service = service
        self.web_root = Path(web_root).resolve()
        super().__init__(address, DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "TradeDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # ---------- 路由（只有 GET） ----------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        try:
            self._route_get()
        except Exception as exc:  # 面板绝不能因为单个请求异常就整个退掉
            log.exception("面板处理请求失败：%s", self.path)
            self._error(500, f"服务端内部错误：{exc}")

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        service: DashboardService = self.server.service  # type: ignore[attr-defined]

        if path == "/api/health":
            self._json({
                "ok": True,
                "bars": list(BARS),
                "default_bar": DEFAULT_BAR,
                "refresh_seconds": service.ttl_seconds,
                "kline_max": service.kline_max,
            })
            return
        if path == "/api/symbols":
            self._json({"symbols": service.symbols()})
            return
        if path == "/api/kline":
            inst_id = query.get("inst_id", "")
            bar = query.get("bar", DEFAULT_BAR)
            limit = _opt_int(query.get("limit"))
            if not inst_id:
                self._error(400, "缺少 inst_id")
                return
            if bar not in BARS:
                self._error(400, f"不支持的周期：{bar}（可选 {'/'.join(BARS)}）")
                return
            self._json_or_404(service.kline(inst_id, bar, limit), f"{inst_id} 的 {bar} K线")
            return
        if path == "/api/snapshot":
            inst_id = query.get("inst_id", "")
            if not inst_id:
                self._error(400, "缺少 inst_id")
                return
            try:
                self._json(service.snapshot(inst_id))
            except ValueError as exc:
                self._error(400, str(exc))
            return
        if path == "/api/alerts":
            self._json({
                "alerts": service.alerts(
                    inst_id=query.get("inst_id") or None,
                    limit=_opt_int(query.get("limit")) or 200,
                    since_ms=_opt_int(query.get("since_ms")),
                    signal_type=query.get("signal_type") or None,
                )
            })
            return

        self._serve_static(path, query)

    def _json_or_404(self, payload: Any | None, what: str) -> None:
        if payload is None:
            self._error(404, f"{what}取数失败（数据源未提供，刷新可重试）")
            return
        self._json(payload)

    # ---------- 静态文件 ----------
    def _serve_static(self, path: str, query: dict[str, str]) -> None:
        web_root: Path = self.server.web_root  # type: ignore[attr-defined]
        relative = INDEX_FILE if path in ("", "/") else path.lstrip("/")
        target = (web_root / relative).resolve()

        # 路径穿越防护：解析后必须仍在 web 目录内
        if not target.is_relative_to(web_root):
            self._error(403, "拒绝访问 web 目录之外的路径")
            return
        if not target.is_file():
            if relative != INDEX_FILE:
                self._error(404, f"找不到文件：{relative}")
                return
            self._error(500, f"缺少前端文件 {INDEX_FILE}，请确认 web/ 目录完整")
            return

        content_type, _ = mimetypes.guess_type(target.name)
        if not content_type:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith(("json", "javascript")):
            content_type = f"{content_type}; charset=utf-8"

        body = target.read_bytes()
        if target.name == INDEX_FILE and query.get("__probe") == "1":
            # 给启动脚本一个「前端文件是否齐全」的探针，避免自己起不来还不报错
            self._json({"ok": True, "bytes": len(body)})
            return
        if target.name == "echarts.min.js" and len(body) < 100_000:
            self._error(500, "web/vendor/echarts.min.js 不完整（疑似下载被截断），请重新获取")
            return
        self._send(200, body, content_type)

    # ---------- 日志（别把每条静态请求都刷进控制台） ----------
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def _opt_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(float(value))
    except ValueError:
        return None
    return parsed if parsed > 0 else None
