"""模拟 RDS MySQL 实例（连接数上限 + 会话列表）。

真实 TCP 监听：超过 max_connections 的连接会收到
`ERROR 1040 (HY000): Too many connections`（与线上一致）。
控制面 HTTP 提供 SHOW PROCESSLIST / KILL 等价能力，供演示与演练使用。
"""
from __future__ import annotations

import json
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
SESSIONS: dict[int, dict] = {}
NEXT_ID = [1001]
MAX_CONN = [50]
STATS = {"total": 0, "rejected": 0, "killed": 0}


class MysqlTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with LOCK:
            if len(SESSIONS) >= MAX_CONN[0]:
                STATS["rejected"] += 1
                try:
                    self.request.sendall(b"ERROR 1040 (HY000): Too many connections\n")
                except OSError:
                    pass
                return
            sid = NEXT_ID[0]
            NEXT_ID[0] += 1
            SESSIONS[sid] = {
                "id": sid,
                "user": "xhs_app",
                "host": f"{self.client_address[0]}:{self.client_address[1]}",
                "db": "xhs_feed",
                "command": "Sleep",
                "connected_at": time.time(),
                "sock": self.request,
            }
            STATS["total"] += 1
        try:
            while True:
                data = self.request.recv(1024)
                if not data:
                    break
                if data.strip().upper() == b"QUIT":
                    break
                with LOCK:
                    if sid in SESSIONS:
                        SESSIONS[sid]["command"] = "Query"
        except OSError:
            pass
        finally:
            with LOCK:
                SESSIONS.pop(sid, None)


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query).get("q", [""])[0].strip().lower()
        if u.path == "/stats":
            with LOCK:
                self._json(200, {"max_connections": MAX_CONN[0], "threads_connected": len(SESSIONS),
                                 **STATS})
            return
        if q.startswith("show processlist"):
            now = time.time()
            with LOCK:
                rows = [{"Id": s["id"], "User": s["user"], "Host": s["host"], "db": s["db"],
                         "Command": s["command"], "Time": int(now - s["connected_at"]),
                         "State": "Waiting for client" if s["command"] == "Sleep" else "executing",
                         "Info": "NULL"} for s in SESSIONS.values()]
            self._json(200, {"rows": rows})
            return
        if "max_connections" in q:
            self._json(200, {"rows": [{"Variable_name": "max_connections", "Value": str(MAX_CONN[0])}]})
            return
        if "threads_connected" in q:
            with LOCK:
                self._json(200, {"rows": [{"Variable_name": "Threads_connected", "Value": str(len(SESSIONS))}]})
            return
        if q.startswith("kill"):
            sid = int(q.split()[-1])
            with LOCK:
                sess = SESSIONS.pop(sid, None)
                if sess:
                    STATS["killed"] += 1
                    try:
                        sess["sock"].shutdown(socket.SHUT_RDWR)
                        sess["sock"].close()
                    except OSError:
                        pass
            self._json(200, {"killed": bool(sess), "id": sid})
            return
        if q.startswith("set global max_connections"):
            MAX_CONN[0] = int(q.split("=")[-1])
            self._json(200, {"ok": True, "max_connections": MAX_CONN[0]})
            return
        self._json(400, {"error": "unsupported sql", "q": q})


def main() -> None:
    tcp_port = int(sys.argv[1]) if len(sys.argv) > 1 else 13306
    ctrl_port = int(sys.argv[2]) if len(sys.argv) > 2 else 13307
    if len(sys.argv) > 3:
        MAX_CONN[0] = int(sys.argv[3])
    tcp = ReuseServer(("127.0.0.1", tcp_port), MysqlTCPHandler)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    print(f"mysql-lite listening tcp={tcp_port} ctrl={ctrl_port} max_connections={MAX_CONN[0]}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ctrl_port), Control).serve_forever()


if __name__ == "__main__":
    main()
