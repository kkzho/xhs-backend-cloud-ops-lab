#!/usr/bin/env python3
"""SLB 5xx / 健康检查失败巡检与自愈。

处置逻辑：
1. 从 SLB 侧取证（DescribeHealthStatus）确认后端状态；
2. 直连后端 /healthz 判断是"后端真故障"还是"健康检查配置错误"；
3. 先摘除异常后端止血，再重启/修复后端，健康后重新挂载；
4. 校验：经 SLB 访问返回 200。
"""
from __future__ import annotations

import time

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("SLB 后端异常巡检")
    if not lab.slb_stats().get("backend"):
        log("SLB 未启动，启动转发（端口 %s）" % lab.ports["slb"])
        lab.start_slb()

    health = lab.slb_stats()
    direct = lab.curl(lab.api + "/healthz")
    via_slb = lab.curl(lab.slb + "/api/v1/feed")
    log(f"SLB 健康检查: {health}")
    log(f"直连后端: {direct.splitlines()[-1] if direct else 'N/A'}")
    log(f"经 SLB: {via_slb.splitlines()[-1] if via_slb else 'N/A'}")

    if "HTTP_CODE=200" in via_slb and "HTTP_CODE=200" in direct:
        log("链路正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：摘除异常后端 → 重启 note-api → 等待健康检查 2 次通过 → 挂回后端")
        done(True, "dry-run")

    lb = lab.state["slb"]["LoadBalancerId"]
    lab.aliyun(f"slb RemoveBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
    log("已摘除 i-bp1xhsapi01，流量止血")
    time.sleep(1)

    lab.restart_api(port=lab.ports["api"])
    log("note-api 已重启")

    ok = lab.wait_until(lambda: "HTTP_CODE=200" in lab.curl(lab.slb + "/api/v1/feed"), timeout=20, interval=2)
    lab.aliyun(f"slb AddBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
    if ok:
        lab.state["slb"]["BackendServers"][0]["HealthStatus"] = "normal"
        lab.state.get("faults", {}).pop("slb", None)
        lab._save()
    log(f"恢复校验：{'SLB 返回 200' if ok else '仍失败'}")
    done(ok, "后端恢复，重新挂载")


if __name__ == "__main__":
    main()
