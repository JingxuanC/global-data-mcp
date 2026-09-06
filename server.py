#!/usr/bin/env python3
"""Global Data MCP Server — 美股 + 宏观舆情工具集的独立 MCP 服务。

用法:
    python3 server.py --port 50058

端点:
    GET  /health        健康检查
    GET  /tools         工具列表（JSON schema）
    POST /mcp           MCP JSON-RPC（initialize / tools/list / tools/call）
    GET  /quota         当前 license key 的额度余量（鉴权模式）

鉴权与额度（mcp_gateway.py）：
    环境变量 MCP_LICENSE_FILE 指向 license JSON 时强制鉴权
    （请求头 X-License-Key）；未配置 = 开放模式（本地/内网）。
    所有工具均为秒级 HTTP 数据获取，同步调用，不走异步队列。

环境变量：
    YAHOO_PROXY     yfinance 专用代理（如 socks5://127.0.0.1:1097），
                    仅作用于美股请求，不污染全局 socket
    FRED_API_KEY    FRED 宏观数据 API key（get_macro_indicators 需要）
    DATA_CACHE_DIR  工具结果缓存目录（默认 .data_cache/）
    DATA_CACHE_TTL  缓存秒数（默认 300）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools import HANDLERS, TOOLS  # noqa: F401 — 副作用：注册全部工具

from mcp_gateway import LicenseStore, QuotaExceeded

logger = logging.getLogger("global-data-mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

SERVER_NAME = "global-data-mcp"
VERSION = "1.0.0"


class GlobalDataHandler(BaseHTTPRequestHandler):
    license_store: LicenseStore | None = None

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tool_schemas(self):
        return [t.to_dict() for t in TOOLS.values()]

    def _license_key(self) -> str:
        return self.headers.get("X-License-Key", "")

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": VERSION, "mode": "global-data-mcp",
                             "tools": len(TOOLS),
                             "auth": bool(self.license_store and self.license_store.enabled)})
        elif self.path == "/tools":
            self._send(200, {"tools": self._tool_schemas()})
        elif self.path == "/quota":
            if not (self.license_store and self.license_store.enabled):
                self._send(200, {"mode": "open"})
                return
            ok, info = self.license_store.check(self._license_key())
            if not ok:
                self._send(401, {"error": info})
                return
            self._send(200, self.license_store.quota_of(self._license_key()))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"invalid JSON: {e}"})
            return
        if self.path == "/mcp":
            self._handle_mcp(data)
        else:
            self._send(404, {"error": "not found"})

    def _handle_mcp(self, data):
        mid = data.get("id")
        method = data.get("method", "")
        params = data.get("params") or {}

        if method == "initialize":
            import uuid as _uuid
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", str(_uuid.uuid4()))
            self.end_headers()
            self.wfile.write(json.dumps({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                },
            }).encode())
            return

        if method == "notifications/initialized":
            self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {}})
            return

        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": mid,
                             "result": {"tools": self._tool_schemas()}})
            return

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            # 鉴权：license 模式强制校验 key（initialize/tools/list 保持开放便于发现）
            store = self.license_store
            key = self._license_key()
            if store and store.enabled:
                ok, info = store.check(key)
                if not ok:
                    self._send(200, {"jsonrpc": "2.0", "id": mid,
                                     "error": {"code": -32001, "message": info}})
                    return
            if tool_name not in HANDLERS:
                self._send(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}})
                return
            # 额度：先扣再跑
            if store and store.enabled:
                try:
                    store.consume(key, heavy=False)
                except QuotaExceeded as e:
                    self._send(200, {"jsonrpc": "2.0", "id": mid,
                                     "error": {"code": -32029, "message": str(e)}})
                    return
            try:
                result = HANDLERS[tool_name](**tool_args)
                self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(result)}], "isError": False}})
            except Exception as e:  # noqa: BLE001
                logger.error("tool call error %s: %s", tool_name, e)
                self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}})
            return

        self._send(200, {"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main():
    ap = argparse.ArgumentParser(description="美股 + 宏观舆情 MCP 服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=50058, help="监听端口（默认 50058）")
    ap.add_argument("--license-file", default=os.environ.get("MCP_LICENSE_FILE", ""),
                    help="license key JSON 路径（env MCP_LICENSE_FILE）；不配置=开放模式")
    args = ap.parse_args()

    GlobalDataHandler.license_store = LicenseStore(args.license_file, domain="global-data")

    server = ThreadingHTTPServer((args.host, args.port), GlobalDataHandler)
    logger.info("global-data MCP listening on %s:%d (tools=%d, auth=%s)",
                args.host, args.port, len(TOOLS),
                "on" if GlobalDataHandler.license_store.enabled else "open")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
