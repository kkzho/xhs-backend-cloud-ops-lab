"""模拟阿里云 Redis（maxmemory / 淘汰策略 / 大 key / 命中率）。

内部按 key 真实持有内存（bytearray），因此 RSS、used_memory、淘汰行为
与设置一致；控制面 HTTP 提供 redis-cli 等价命令。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
DATA: dict[str, dict] = {}          # key -> {mem: bytearray, atime, expire_at, hits}
STATS = {"hits": 0, "misses": 0, "evicted": 0, "expired": 0, "commands": 0}
CONF = {"maxmemory": 32 * 1024 * 1024, "maxmemory-policy": "noeviction"}
START = time.time()


def used_memory() -> int:
    return sum(len(v["mem"]) for v in DATA.values())


def _evict_if_needed(need: int) -> bool:
    """返回 True 表示可写入；False 表示 noeviction 下拒绝写入。"""
    if used_memory() + need <= CONF["maxmemory"]:
        return True
    if CONF["maxmemory-policy"] == "noeviction":
        return False
    while used_memory() + need > CONF["maxmemory"] and DATA:
        victim = min(DATA, key=lambda k: DATA[k]["atime"])
        DATA.pop(victim, None)
        STATS["evicted"] += 1
    return used_memory() + need <= CONF["maxmemory"]


def _expire() -> None:
    now = time.time()
    for k in [k for k, v in DATA.items() if v["expire_at"] and v["expire_at"] < now]:
        DATA.pop(k, None)
        STATS["expired"] += 1


def info_memory() -> str:
    um = used_memory()
    peak = max(um, getattr(info_memory, "_peak", 0))
    info_memory._peak = peak
    frag = 1.08
    return "\n".join([
        "# Memory",
        f"used_memory:{um}",
        f"used_memory_human:{um / 1024 / 1024:.2f}M",
        f"used_memory_rss:{int(um * frag)}",
        f"used_memory_peak:{peak}",
        f"used_memory_peak_human:{peak / 1024 / 1024:.2f}M",
        f"maxmemory:{CONF['maxmemory']}",
        f"maxmemory_human:{CONF['maxmemory'] / 1024 / 1024:.2f}M",
        f"maxmemory_policy:{CONF['maxmemory-policy']}",
        f"mem_fragmentation_ratio:{frag:.2f}",
        f"evicted_keys:{STATS['evicted']}",
    ])


def info_stats() -> str:
    total = STATS["hits"] + STATS["misses"]
    hr = (STATS["hits"] / total) if total else 1.0
    return "\n".join([
        "# Stats",
        f"total_connections_received:{STATS['commands']}",
        f"keyspace_hits:{STATS['hits']}",
        f"keyspace_misses:{STATS['misses']}",
        f"hit_rate:{hr:.4f}",
        f"evicted_keys:{STATS['evicted']}",
        f"expired_keys:{STATS['expired']}",
        f"uptime_in_seconds:{int(time.time() - START)}",
    ])


def bigkeys() -> str:
    lines = ["# Scanning the entire keyspace to find biggest keys as well as",
             "# average sizes per key type.  You can use -i 0.1 to sleep 0.1 sec",
             "", "-------- summary -------", ""]
    for k, v in sorted(DATA.items(), key=lambda kv: -len(kv[1]["mem"]))[:5]:
        lines.append(f"Biggest string found '{k}' has {len(v['mem'])} bytes")
    lines.append(f"Sampled 0 keys in the keyspace!")
    lines.append(f"Total key length in bytes is {sum(len(k) for k in DATA)} (avg len {0 if not DATA else round(sum(len(k) for k in DATA) / len(DATA))})")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _text(self, s: str, code: int = 200):
        body = s.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/stats":
            with LOCK:
                body = json.dumps({"used_memory": used_memory(), "maxmemory": CONF["maxmemory"],
                                   "policy": CONF["maxmemory-policy"], "keys": len(DATA), **STATS})
            self._text(body)
            return
        raw = parse_qs(u.query).get("c", [""])[0].strip()
        if not raw:
            self._text("ERR empty command", 400)
            return
        parts = raw.split()
        cmd = parts[0].upper()
        with LOCK:
            _expire()
            STATS["commands"] += 1
            if cmd == "INFO":
                self._text(info_memory() if len(parts) > 1 and parts[1].lower() == "memory" else info_stats())
            elif cmd == "DBSIZE":
                self._text(f"(integer) {len(DATA)}")
            elif cmd == "CONFIG":
                if len(parts) >= 3 and parts[1].upper() == "GET":
                    key = parts[2].lower()
                    self._text("\n".join([f"1) \"{key}\"", f"2) \"{CONF.get(key, '')}\""]))
                elif len(parts) >= 4 and parts[1].upper() == "SET":
                    key, val = parts[2].lower(), parts[3].lower()
                    if key == "maxmemory-policy":
                        CONF[key] = val
                    elif key == "maxmemory":
                        CONF[key] = int(val)
                    else:
                        self._text("ERR unsupported config")
                        return
                    self._text("OK")
                else:
                    self._text("ERR syntax error")
            elif cmd == "MEMORY" and len(parts) >= 3 and parts[1].upper() == "USAGE":
                v = DATA.get(parts[2])
                self._text(f"(integer) {len(v['mem']) if v else 0}")
            elif cmd == "--BIGKEYS":
                self._text(bigkeys())
            elif cmd == "SET":
                key, size = parts[1], int(parts[2]) if len(parts) > 2 else 64
                mem = bytearray(min(size, 8 * 1024 * 1024))
                if not _evict_if_needed(len(mem)):
                    self._text("(error) OOM command not allowed when used memory > 'maxmemory'.", 200)
                    return
                DATA[key] = {"mem": mem, "atime": time.time(), "expire_at": None, "hits": 0}
                self._text("OK")
            elif cmd == "GET":
                v = DATA.get(parts[1])
                if v:
                    v["atime"] = time.time()
                    v["hits"] += 1
                    STATS["hits"] += 1
                    self._text(f"\"{len(v['mem'])} bytes value\"")
                else:
                    STATS["misses"] += 1
                    self._text("(nil)")
            elif cmd == "DEL":
                self._text(f"(integer) {1 if DATA.pop(parts[1], None) else 0}")
            else:
                self._text(f"ERR unknown command '{cmd}'")
                return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 16379
    if len(sys.argv) > 2:
        CONF["maxmemory"] = int(sys.argv[2]) * 1024 * 1024
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
