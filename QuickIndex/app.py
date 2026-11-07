# -*- coding: utf-8 -*-
"""QuickIndex · 本地 HTTP 服务 + Web 界面

仅监听 127.0.0.1，不对外暴露。
    python app.py                 # 启动并自动打开窗口
    python app.py --port 8800     # 指定端口
    python app.py --no-browser    # 只起服务
    python app.py --rebuild       # 启动后先做一次全量重建
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import APP_DIR, Indexer

WEB_DIR = os.path.join(APP_DIR, "web")
DEFAULT_PORT = 8765
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}

INDEXER: Indexer


class Handler(BaseHTTPRequestHandler):
    server_version = "QuickIndex/1.0"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------- 基础响应
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    # -------------------------------------------------- 路由
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/status":
                return self._json(self._status())
            if path == "/api/search":
                q = (query.get("q") or [""])[0]
                limit = int((query.get("limit") or ["200"])[0])
                sort = (query.get("sort") or ["relevance"])[0]
                offset = int((query.get("offset") or ["0"])[0])
                return self._json(INDEXER.search(q, limit=limit, sort=sort, offset=offset))
            if path == "/api/stats":
                return self._json(INDEXER.stats())
            if path == "/api/recent":
                return self._json({"items": INDEXER.recent()})
            if path == "/api/config":
                return self._json({"config": INDEXER.config, "data_dir": INDEXER.data_dir})
            if path == "/api/ping":
                return self._json({"ok": True, "app": "QuickIndex"})
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)}, 500)

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        data = self._read_json()
        try:
            if path == "/api/index":
                res = INDEXER.start_index(rebuild=bool(data.get("rebuild")),
                                          roots=data.get("roots") or None)
                return self._json(res)
            if path == "/api/stop":
                return self._json(INDEXER.stop_index())
            if path == "/api/open":
                target = str(data.get("path") or "")
                INDEXER.record_open(target)
                return self._json(INDEXER.open_path(target, reveal=bool(data.get("reveal"))))
            if path == "/api/config":
                cfg = INDEXER.save_config(data.get("config") or {})
                return self._json({"ok": True, "config": cfg})
            if path == "/api/recent/clear":
                INDEXER.clear_history()
                return self._json({"ok": True})
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
                return
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)}, 500)

    # -------------------------------------------------- 静态文件
    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        return self._send(200, body, ctype)

    # -------------------------------------------------- 状态
    def _status(self) -> dict:
        st = dict(INDEXER.status)
        st["roots"] = INDEXER.config.get("roots")
        st["auto_index_minutes"] = INDEXER.config.get("auto_index_minutes", 0)
        st["data_dir"] = INDEXER.data_dir
        st["uptime"] = round(time.time() - START_TS, 1)
        if st.get("started_at") and st["indexing"]:
            st["elapsed"] = round(time.time() - st["started_at"], 1)
        return st


START_TS = time.time()


# ---------------------------------------------------------------- 启动辅助

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as r:
            return b"QuickIndex" in r.read()
    except (urllib.error.URLError, OSError):
        return False


def find_port(start: int) -> int:
    for port in range(start, start + 20):
        if port_free(port):
            return port
    return start


def open_window(url: str) -> None:
    """优先用 Edge/Chrome 的应用窗口模式，像个原生桌面软件。"""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge") or "",
        shutil.which("chrome") or "",
    ]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1360,880",
                                  "--window-position=80,60"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def scheduler(auto_index_on_start: bool) -> None:
    if auto_index_on_start:
        time.sleep(0.4)
        if not INDEXER.status["indexing"]:
            INDEXER.start_index(rebuild=False)
    while True:
        minutes = int(INDEXER.config.get("auto_index_minutes") or 0)
        time.sleep(30)
        if minutes <= 0 or INDEXER.status["indexing"]:
            continue
        last = INDEXER.status.get("finished_at") or 0
        if time.time() - last >= minutes * 60:
            INDEXER.start_index(rebuild=False)


def main() -> None:
    global INDEXER
    args = sys.argv[1:]
    port = DEFAULT_PORT
    open_browser = True
    rebuild = False
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a == "--no-browser":
            open_browser = False
        elif a == "--rebuild":
            rebuild = True

    INDEXER = Indexer()
    print(f"  QuickIndex  数据库：{INDEXER.db_path}")
    print(f"  索引范围：{', '.join(INDEXER.config.get('roots') or [])}")

    if is_ours(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"  已在运行，直接打开窗口：{url}")
        if open_browser:
            open_window(url)
        return

    port = find_port(port)
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    if rebuild:
        INDEXER.start_index(rebuild=True)
    else:
        threading.Thread(target=scheduler,
                         args=(bool(INDEXER.config.get("auto_index_on_start", True)),),
                         daemon=True).start()

    print(f"  服务地址：{url}")
    print("  关闭窗口后按 Ctrl+C 退出服务\n")
    if open_browser:
        threading.Timer(0.35, lambda: open_window(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("  已退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
