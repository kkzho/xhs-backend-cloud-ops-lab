#!/usr/bin/env python3
"""内存持续增长巡检与自愈。

处置逻辑：
1. 读取服务 RSS 增长趋势与 OOM 记录；
2. 超过水位线时先摘流量（SLB 移除后端）再滚动重启，避免雪崩；
3. 写入 systemd MemoryMax 限制与 OOM 告警建议；
4. 校验：RSS 回落、进程存活、健康检查通过。
"""
from __future__ import annotations

import time

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("内存增长巡检")
    st = lab.api_stats()
    rss, limit = st.get("rss_mb", 0), lab.state.get("faults", {}).get("memory", {}).get("limit_mb", 0)
    oom = lab.state.get("faults", {}).get("memory", {}).get("oom", 0)
    log(f"当前 RSS={rss}MB，水位线={limit or 512}MB，OOM 次数={oom}")
    log(f"dmesg: {lab.sh('cat /var/log/xhs/dmesg.log') if lab.path('/var/log/xhs/dmesg.log').exists() else '(none)'}")

    if rss < 120 and not oom:
        log("[dry-run] 未超过水位线，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：SLB 摘除后端 → 重启 note-api → 校验收敛 → 保留 heap 现场")
        done(True, "dry-run")

    lab.aliyun(f"slb RemoveBackendServers --LoadBalancerId {lab.state['slb']['LoadBalancerId']} "
               f"--BackendServers i-bp1xhsapi01")
    log("SLB 已摘除 i-bp1xhsapi01")
    time.sleep(1)

    svc = lab.state["services"]["xhs-note-api"]
    lab.kill_service()
    lab.start_api(port=svc["port"], nofile=256)
    lab.write("/etc/systemd/system/xhs-note-api.service.d/memory.conf",
              "[Service]\nMemoryMax=1G\nMemoryHigh=800M\nRestart=always\nRestartSec=2\n")
    log("已重启 note-api，并写入 MemoryMax=1G")

    time.sleep(1)
    st2 = lab.api_stats()
    ok = st2.get("rss_mb", 999) < 120 and lab.assert_service_healthy()
    lab.aliyun(f"slb AddBackendServers --LoadBalancerId {lab.state['slb']['LoadBalancerId']} "
               f"--BackendServers i-bp1xhsapi01")
    if ok:
        lab.state.get("faults", {}).pop("memory", None)
        lab._save()
    log(f"重启后 RSS={st2.get('rss_mb')}MB，健康检查={'通过' if ok else '失败'}")
    done(ok, "内存已回落，SLB 已挂回")


if __name__ == "__main__":
    main()
