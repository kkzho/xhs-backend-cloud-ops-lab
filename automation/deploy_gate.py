#!/usr/bin/env python3
"""发布后健康门禁与一键回滚。

处置逻辑：
1. 发布后执行三层探活：进程/端口（ss）、本机健康检查（/healthz）、业务冒烟（/api/v1/feed）；
2. 任一失败即判定发布失败，读取上一版本配置并回滚；
3. 回滚后再次冒烟，通过才返回成功；
4. 输出发布结论，供 CI 卡点使用。
"""
from __future__ import annotations

import re
import time

from _base import cli, done, log


def _probe(lab, port: int) -> dict:
    listening = f":{port}" in lab.sh("ss -lntp")
    health = lab.curl(f"http://127.0.0.1:{port}/healthz")
    smoke = lab.curl(f"http://127.0.0.1:{port}/api/v1/feed")
    return {"listen": listening, "health": "HTTP_CODE=200" in health, "smoke": "HTTP_CODE=200" in smoke}


def main() -> None:
    lab, apply = cli("发布健康门禁")
    svc = lab.state["services"]["xhs-note-api"]
    expected = svc["port"]
    unit = lab.path("/etc/systemd/system/xhs-note-api.service").read_text()
    m = re.search(r"Environment=PORT=(\d+)", unit)
    if m:
        expected = int(m.group(1))

    before = _probe(lab, expected)
    log(f"发布版本={lab.state['deploy']['CurrentVersion']}，期望端口={expected}")
    log(f"三层探活: {before}")
    log("ss -lntp:\n" + lab.sh("ss -lntp"))
    log("journalctl:\n" + lab.sh("journalctl -u xhs-note-api -n 6"))

    if all(before.values()):
        log("冒烟通过，发布成功")
        lab.state["deploy"]["Status"] = "success"
        lab._save()
        done(True, "NOOP")

    if not apply:
        log(f"[dry-run] 计划：回滚到上一版本配置（listen_port={expected}）→ 重启 → 重新冒烟")
        done(True, "dry-run")

    lab.write("/etc/xhs/note-api.conf",
              "# 回滚：恢复上一版本配置\n"
              f"listen_host = 127.0.0.1\nlisten_port = {expected}\n"
              "worker_processes = 4\nworker_connections = 1024\nmax_open_files = 65535\n")
    lab.restart_api(port=expected, nofile=4096)
    log(f"已回滚并重启，listen_port={expected}")

    ok = lab.wait_until(lambda: all(_probe(lab, expected).values()), timeout=15, interval=1)
    lab.state["deploy"]["Status"] = "rolled_back"
    lab.state["deploy"]["History"].insert(0, {
        "Version": lab.state["deploy"]["CurrentVersion"], "Time": time.strftime("%F %H:%M"),
        "Result": "rolled_back"})
    lab.state["deploy"]["CurrentVersion"] = "v2026.11.18-3"
    lab._save()
    log(f"回滚后探活: {_probe(lab, expected)}")
    done(ok, "回滚完成，服务恢复")


if __name__ == "__main__":
    main()
