"""模拟日志采集 Agent（类似 Logtail / fluent-bit）。

持续 tail 指定日志文件并推进 offset；进程停止后 offset 不再前进，
云侧 SLS 采集延迟（ingest lag）随之增长——用于演练"日志断流"。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WATCH = ""
OFFSET_FILE = ""
STATE = {"offset": 0, "lines": 0, "started_at": time.time(), "last_flush": 0.0}
LOCK = threading.Lock()


def follow() -> None:
    while True:
        try:
            size = os.path.getsize(WATCH)
            with open(WATCH, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(STATE["offset"])
                while f.readline():
                    with LOCK:
                        STATE["lines"] += 1
                        STATE["offset"] = f.tell()
            if size < STATE["offset"]:
                STATE["offset"] = 0
            with LOCK:
                STATE["last_flush"] = time.time()
                with open(OFFSET_FILE, "w", encoding="utf-8") as of:
                    json.dump(STATE, of)
        except FileNotFoundError:
            pass
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            size = os.path.getsize(WATCH)
        except FileNotFoundError:
            size = 0
        payload = {"file": WATCH, "file_size": size, "offset": STATE["offset"],
                   "lag_bytes": max(size - STATE["offset"], 0),
                   "lag_sec": round(time.time() - STATE["last_flush"], 1) if STATE["last_flush"] else None,
                   "lines": STATE["lines"], "alive": True}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    global WATCH, OFFSET_FILE
    WATCH = sys.argv[1]
    OFFSET_FILE = sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 18081
    threading.Thread(target=follow, daemon=True).start()
    print(f"log-agent watching {WATCH} ctrl={port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
