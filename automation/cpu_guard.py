#!/usr/bin/env python3
"""CPU 异常占用巡检与自愈。

处置逻辑：
1. 采样 1s，找出单核占用 > 60% 且属于 xhs 业务组的进程（沙箱中按 cmdline 识别）；
2. 记录现场（PID/CPU/命令）后终止异常任务；
3. 将 crontab 改为 flock + nice + 限流，避免再次并发失控；
4. 校验：异常进程消失，业务端口可访问。
"""
from __future__ import annotations

import os
import signal
import time

import psutil

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("CPU 异常占用巡检")
    first = {}
    for p in psutil.process_iter(["pid"]):
        try:
            first[p.pid] = sum(p.cpu_times()[:2])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(1.0)
    cpu = psutil.cpu_count() or 1
    hot = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            delta = sum(p.cpu_times()[:2]) - first.get(p.pid, 0)
            pct = delta / 1.0 / cpu * 100
            line = " ".join(p.info["cmdline"] or [])
            if pct > 8 and ("while True" in line or "ranking_precompute" in line):
                hot.append((p.pid, round(pct, 1), line[:80]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    log(f"检测到异常占用进程 {len(hot)} 个: {hot}")
    log("cron 现场: " + (lab.path("/etc/cron.d/xhs-ranking").read_text().strip().splitlines()[-1]))

    if not apply:
        log("[dry-run] 计划：kill 上述 PID；crontab 增加 flock/nice/--limit；保留现场到事件记录")
        done(True, "dry-run")

    fault = lab.state.get("faults", {}).get("cpu", {})
    pids = {h[0] for h in hot} | set(fault.get("pids", []))
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    log(f"已终止 {len(pids)} 个异常进程")

    lab.write("/etc/cron.d/xhs-ranking",
              "# 已改造：串行 + 限流 + 降优先级，避免并发风暴\n"
              "*/5 * * * * xhs flock -n /var/lock/ranking.lock nice -n 10 "
              "python3 /srv/xhs/jobs/ranking_precompute.py --limit 2000\n")
    lab.state.get("faults", {}).pop("cpu", None)
    lab._save()
    time.sleep(1.0)

    verify = {}
    for p in psutil.process_iter(["pid"]):
        try:
            verify[p.pid] = sum(p.cpu_times()[:2])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(1.0)
    remain = 0
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            delta = sum(p.cpu_times()[:2]) - verify.get(p.pid, 0)
            if delta / cpu * 100 > 8 and "while True" in " ".join(p.info["cmdline"] or []):
                remain += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    healthy = lab.curl(lab.api + "/healthz")
    ok = remain == 0 and "status" in healthy
    log(f"校验：残留异常进程={remain}，业务探活={'正常' if 'status' in healthy else '异常'}")
    done(ok, "CPU 已恢复，cron 已加固")


if __name__ == "__main__":
    main()
