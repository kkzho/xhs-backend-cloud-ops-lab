"""模拟小红书后端应用服务（note-api）。

真实的 HTTP 服务进程，可注入：延迟、错误率、依赖不可用、FD 泄漏、内存增长。
通过 HTTP 控制面 /__control 修改行为，/__stats 暴露运行时指标。
"""
from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "latency_ms": 8,
    "error_rate": 0.0,
    "health": "ok",
    "upstream": None,          # 例：127.0.0.1:13306，不可达时返回 503
    "fd_leak": False,
    "leak_mb": 0,
    "version": os.environ.get("XHS_VERSION", "v2026.11.18-3"),
}
LEAK_POOL = []                 # 故意不关闭的句柄（FD 泄漏）
MEM_POOL = []                  # 故意不释放的内存（内存增长）
LOCK = threading.Lock()
STATS = {"requests": 0, "errors": 0, "started_at": time.time()}
LOGFILE = os.environ.get("XHS_LOG", "")


def log(level: str, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {level} [note-api] {msg}"
    print(line, flush=True)
    if LOGFILE:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _leak_memory(target_mb: int) -> None:
    """后台线程持续申请内存，模拟对象未释放导致 RSS 持续上涨。"""
    chunk = 4 * 1024 * 1024
    while True:
        with LOCK:
            if STATE["leak_mb"] <= 0:
                break
            if sum(len(b) for b in MEM_POOL) >= STATE["leak_mb"] * 1024 * 1024:
                time.sleep(0.2)
                continue
            MEM_POOL.append(bytearray(chunk))
        time.sleep(0.05)


def _upstream_alive() -> bool:
    up = STATE.get("upstream")
    if not up:
        return True
    host, _, port = up.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xhs-note-api"

    def log_message(self, fmt, *args):  # 关闭默认 stderr 访问日志，统一走 log()
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        started = time.time()
        path = self.path.split("?")[0]
        with LOCK:
            STATS["requests"] += 1

        if path == "/__stats":
            proc = __import__("psutil").Process()
            try:
                rss_mb = round(proc.memory_info().rss / 1024 / 1024, 1)
                fds = proc.num_fds() if hasattr(proc, "num_fds") else proc.num_handles()
            except Exception:
                rss_mb, fds = -1, -1
            self._send(200, {"rss_mb": rss_mb, "fds": fds, **STATS,
                             "uptime_sec": round(time.time() - STATS["started_at"]),
                             "version": STATE["version"], "state": STATE})
            return

        if path == "/__control":
            # 控制面仅本机调试使用，生产环境不暴露
            self._send(200, {"ok": True, "state": STATE})
            return

        # 健康检查：依赖异常时返回 500，触发 SLB 摘除
        if path == "/healthz":
            if STATE["health"] != "ok":
                self._send(500, {"status": "fail", "reason": STATE["health"]})
            else:
                self._send(200, {"status": "ok"})
            self._access(path, 200, started)
            return

        if STATE["latency_ms"]:
            time.sleep(STATE["latency_ms"] / 1000.0)

        if not _upstream_alive():
            with LOCK:
                STATS["errors"] += 1
            self._send(503, {"code": "UPSTREAM_UNAVAILABLE", "msg": "feed service unavailable"})
            self._access(path, 503, started)
            return

        if STATE["fd_leak"]:
            # 典型缺陷：请求内打开文件/连接后未关闭
            try:
                LEAK_POOL.append(open(os.devnull, "r"))
            except OSError as exc:
                with LOCK:
                    STATS["errors"] += 1
                log("ERROR", f"OSError: [Errno {exc.errno}] {exc.strerror}")
                self._send(500, {"code": "TOO_MANY_OPEN_FILES", "msg": str(exc)})
                self._access(path, 500, started)
                return

        if random.random() < STATE["error_rate"]:
            with LOCK:
                STATS["errors"] += 1
            self._send(500, {"code": "INTERNAL_ERROR"})
            self._access(path, 500, started)
            return

        if path.startswith("/api/v1/note/"):
            note_id = path.rsplit("/", 1)[-1]
            self._send(200, {"note_id": note_id, "title": "秋日穿搭 | 山系风", "likes": 12873})
        elif path == "/api/v1/feed":
            self._send(200, {"items": 20, "next_cursor": "c_9f2a"})
        else:
            self._send(404, {"code": "NOT_FOUND"})
        self._access(path, 200, started)

    def do_POST(self):
        if self.path == "/__control":
            length = int(self.headers.get("Content-Length", 0))
            patch = json.loads(self.rfile.read(length) or b"{}")
            with LOCK:
                STATE.update(patch)
                if patch.get("leak_mb", 0) > 0:
                    threading.Thread(target=_leak_memory, args=(STATE["leak_mb"],), daemon=True).start()
            self._send(200, {"ok": True, "state": STATE})
            return
        self._send(404, {"code": "NOT_FOUND"})

    def _access(self, path: str, status: int, started: float) -> None:
        rt = int((time.time() - started) * 1000)
        log("INFO", f"method=GET path={path} status={status} rt={rt}ms client={self.client_address[0]}")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log("INFO", f"note-api started version={STATE['version']} listen={host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
