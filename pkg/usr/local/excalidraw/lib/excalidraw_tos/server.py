"""Excalidraw for TOS —— HTTP-over-Unix-Socket 后端。

TOS 7 §8.7.1（WebUI Internal Open / Unix Socket Mode）契约：
  - 可执行文件位于 /usr/local/<appid>/bin/<binary_name>
  - 后端监听 /var/api/<appid>.sock
  - /var/api 不存在时自动创建；启动前清理残留 socket
  - socket 权限需允许平台代理访问
  - 协议为 HTTP over Unix Socket
  - 需优雅处理 SIGTERM

平台通过 /v2/proxy/<app_id>/<api_name> 把前端请求代理到本服务。
静态页面由平台从 webui/ 目录提供，本服务只处理 /health 与 /api/*。
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import signal
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

APP_ID = os.environ.get("APP_ID", "excalidraw")
APP_ROOT = os.environ.get("APP_ROOT", f"/usr/local/{APP_ID}")
WEB_ROOT = os.environ.get("WEBUI_DIR", os.path.join(APP_ROOT, "webui"))
SOCKET_DIR = "/var/api"
SOCKET_PATH = os.path.join(SOCKET_DIR, f"{APP_ID}.sock")

# 可选的协作后端（excalidraw-room）。留空 = 纯本地模式。
COLLAB_URL = os.environ.get("COLLAB_URL", "").rstrip("/")

VERSION = os.environ.get("APP_VERSION", "dev")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def _log(level: str, message: str) -> None:
    """标准日志格式：[YYYY-MM-DD HH:MM:SS] [LEVEL] [component] message"""
    if _LEVELS.get(level, 20) < _LEVELS.get(LOG_LEVEL, 20):
        return
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{stamp}] [{level}] [{APP_ID}] {message}\n")
    sys.stderr.flush()


MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

STATIC_CACHE = "public, max-age=604800, immutable"


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _safe_join(root: str, url_path: str) -> str | None:
    """把 URL 路径安全地映射到 root 下的真实文件，阻止路径穿越。"""
    raw = url_path.split("?", 1)[0].split("#", 1)[0]
    raw = urllib.parse.unquote(raw)
    trailing_dir = raw.endswith("/")
    decoded = posixpath.normpath("/" + raw.lstrip("/"))

    if ".." in decoded.split("/"):
        return None

    if decoded == "/":
        decoded = "/index.html"
    elif trailing_dir:
        decoded = decoded.rstrip("/") + "/index.html"

    rel = decoded.lstrip("/")
    full = os.path.normpath(os.path.join(root, rel))
    root_abs = os.path.normpath(root)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        return None
    return full


class Handler(BaseHTTPRequestHandler):
    server_version = f"ExcalidrawTOS/{VERSION}"
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, ctype: str, cache: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error_page(self, code: int, message: str) -> None:
        html = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f"<title>Excalidraw · {code}</title>"
            "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;"
            "margin:0;background:#f8f9fa;color:#333}"
            ".box{text-align:center;max-width:560px;padding:32px}"
            "h1{font-size:56px;margin:0;color:#6965db}"
            "p{color:#555;line-height:1.7}</style></head><body>"
            f'<div class="box"><h1>{code}</h1><p>{message}</p></div></body></html>'
        ).encode("utf-8")
        self._send_bytes(code, html, "text/html; charset=utf-8", "no-store")

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = self.path

        if path in ("/health", "/healthz", "/ping"):
            return self._send_json(
                200,
                {
                    "status": "ok",
                    "app": APP_ID,
                    "version": VERSION,
                    "webui": os.path.isdir(WEB_ROOT),
                    "collab": bool(COLLAB_URL),
                },
            )

        if path.startswith("/api/"):
            return self._proxy_or_degrade(path)

        return self._serve_static()

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            return self._proxy_or_degrade(self.path)
        self._send_json(405, {"error": "method not allowed"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self) -> None:
        """兜底静态服务（正常由平台提供 webui/）。"""
        if not os.path.isdir(WEB_ROOT):
            return self._error_page(503, "WebUI 资源未找到，请重新安装本应用。")

        target = _safe_join(WEB_ROOT, self.path)
        if target is None:
            return self._error_page(403, "非法的资源路径。")

        if os.path.isdir(target):
            index = os.path.join(target, "index.html")
            if os.path.isfile(index):
                target = index
            else:
                return self._error_page(404, "资源不存在。")

        if not os.path.isfile(target):
            has_ext = os.path.splitext(target)[1] != ""
            index = os.path.join(WEB_ROOT, "index.html")
            if not has_ext and os.path.isfile(index):
                target = index
            else:
                return self._error_page(404, "资源不存在。")

        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error_page(500, "读取资源失败。")

        cache = "no-cache" if target.endswith(".html") else STATIC_CACHE
        self._send_bytes(200, body, _content_type(target), cache)

    def _proxy_or_degrade(self, path: str) -> None:
        if not COLLAB_URL:
            return self._send_json(
                503,
                {
                    "error": "collab disabled",
                    "hint": "未启用协作后端，前端以本地模式运行。",
                },
            )

        target = COLLAB_URL + path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(target, data=body, method=self.command)
        for header in ("Content-Type", "Accept"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                self._send_bytes(resp.status, payload, ctype, "no-store")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            ctype = exc.headers.get("Content-Type", "application/json")
            self._send_bytes(exc.code, payload, ctype, "no-store")
        except Exception:  # noqa: BLE001
            self._send_json(503, {"error": "collab backend unavailable"})

    def log_message(self, fmt: str, *args) -> None:
        _log("DEBUG", f"{self.address_string()} {fmt % args}")


_UnixBase = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class ThreadedUnixHTTPServer(socketserver.ThreadingMixIn, _UnixBase):
    daemon_threads = True

    def get_request(self):
        request, addr = super().get_request()
        return request, addr or ("unix", 0)

    def client_address(self):  # pragma: no cover - 仅用于日志
        return ("unix", 0)


def _prepare_socket() -> None:
    """§8.7.1: 自动创建 /var/api，并在 bind 前清理残留 socket。"""
    os.makedirs(SOCKET_DIR, exist_ok=True)
    if os.path.lexists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
            _log("INFO", f"removed stale socket {SOCKET_PATH}")
        except OSError as exc:
            raise SystemExit(
                f"{SOCKET_PATH} 存在但无法删除（{exc.strerror}）。"
                "该文件可能属于其他账户，请手动删除后重启服务。"
            ) from exc


def _install_signal_handlers() -> None:
    """§8.7.1: 优雅处理 SIGTERM。"""

    def _stop(signum, frame):  # noqa: ARG001
        _log("INFO", f"received signal {signum}, shutting down")
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


def main() -> int:
    _install_signal_handlers()

    if not os.path.isdir(WEB_ROOT):
        _log("WARN", f"webui 目录不存在: {WEB_ROOT}")

    _prepare_socket()
    server = ThreadedUnixHTTPServer(SOCKET_PATH, Handler)
    os.chmod(SOCKET_PATH, 0o660)
    _log("INFO", f"service started, listening on {SOCKET_PATH} (version {VERSION})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        _log("INFO", "service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
