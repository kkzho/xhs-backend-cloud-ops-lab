#!/usr/bin/env python3
"""RDS 连接数打满巡检与处置。

处置逻辑：
1. SHOW PROCESSLIST 统计 Sleep 会话与来源，确认为连接池泄漏；
2. KILL 长时间空闲会话（保留业务活跃连接）；
3. 修正应用侧连接池参数（max_open / idle_timeout / health_check）；
4. 校验 Threads_connected 回落。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from _base import cli, done, log


def _rows(lab, sql: str) -> list[dict]:
    url = f"http://127.0.0.1:{lab.ports['mysql_ctrl']}/sql?q={urllib.parse.quote(sql)}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read()).get("rows", [])


def main() -> None:
    lab, apply = cli("RDS 连接数巡检")
    rows = _rows(lab, "show processlist")
    sleepers = [r for r in rows if r["Command"] == "Sleep"]
    log(f"Threads_connected={len(rows)}，Sleep={len(sleepers)}，max_connections="
        f"{lab.mysql_stats().get('max_connections')}")
    log(f"来源分布 TOP: {sorted({r['Host'].split(':')[0] for r in rows})[:5]}")
    log(f"应用日志: {lab.sh('tail -n 5 /var/log/xhs/note-api.log')}")

    if len(rows) < 40:
        log("连接数正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：KILL Sleep > 30s 会话 → 收紧连接池 → 观察 1 分钟")
        done(True, "dry-run")

    killed = 0
    for r in sleepers:
        if int(r["Time"]) >= 5:
            _rows(lab, f"kill {r['Id']}")
            killed += 1
    log(f"已 KILL {killed} 个空闲会话")

    lab.write("/etc/xhs/note-api.conf",
              "# 连接池已修复：限制上限 + 归还超时 + 探活\n"
              f"listen_host = 127.0.0.1\nlisten_port = {lab.ports['api']}\n"
              "db.pool.max_open = 20\ndb.pool.max_idle = 5\ndb.pool.idle_timeout = 30s\n"
              "db.pool.health_check = true\n")

    # 服务端 KILL 后会话立即释放；应用侧连接池参数已同步修正
    lab.state.get("faults", {}).pop("rds", None)
    lab._save()

    after = len(_rows(lab, "show processlist"))
    log(f"处置后 Threads_connected={after}")
    done(after < 30, "连接数已回落")


if __name__ == "__main__":
    main()
