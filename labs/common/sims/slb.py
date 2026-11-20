"""模拟阿里云 SLB（七层）转发与健康检查。

真实 TCP 转发：后端 /healthz 失败时摘除，摘除期间返回 502（SLB_502）。
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = "127.0.0.1:8080"
HEALTH_PATH = "/healthz"
STATS = {"healthy": True, "checks": 0, "fails": 0, "requests": 0, "errors_5xx": 0}
LOG = None


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} INFO [slb] {msg}"
    print(line, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _split(addr: str) -> tuple[str, int]:
    host, _, port = addr.partition(":")
    return host, int(port)


def health_loop(interval: float = 2.0) -> None:
    host, port = _split(BACKEND)
    fails = 0
    while True:
        STATS["checks"] += 1
        ok = False
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1)
            conn.request("GET", HEALTH_PATH)
            ok = conn.getresponse().status == 200
            conn.close()
        except OSError:
            ok = False
        fails = 0 if ok else fails + 1
        if fails >= 2 and STATS["healthy"]:
            STATS["healthy"] = False
            log(f"backend {BACKEND} health check failed, removed from server group")
        if ok and not STATS["healthy"]:
            STATS["healthy"] = True
            log(f"backend {BACKEND} recovered, added back to server group")
        STATS["fails"] = fails
        time.sleep(interval)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/__stats":
            self._json(200, {"backend": BACKEND, **STATS})
            return
        STATS["requests"] += 1
        if not STATS["healthy"]:
            STATS["errors_5xx"] += 1
            self._json(502, {"code": "SLB_502", "msg": "backend server is unhealthy"})
            return
        host, port = _split(BACKEND)
        try:
            conn = http.client.HTTPConnection(host, port, timeout=3)
            conn.request("GET", self.path, headers={"Host": self.headers.get("Host", "api.xiaohongshu-lab.com")})
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if resp.status >= 500:
                STATS["errors_5xx"] += 1
            conn.close()
        except OSError as exc:
            STATS["errors_5xx"] += 1
            log(f"upstream error {exc}")
            self._json(502, {"code": "SLB_502", "msg": "upstream connect failed"})


def main() -> None:
    global BACKEND, LOG
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    if len(sys.argv) > 2:
        BACKEND = sys.argv[2]
    LOG = sys.argv[3] if len(sys.argv) > 3 else None
    threading.Thread(target=health_loop, daemon=True).start()
    log(f"slb listener started port={port} backend={BACKEND}")
    ThreadingHTTPServer(("127.0.0.1", port), Proxy).serve_forever()


if __name__ == "__main__":
    main()
