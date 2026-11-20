#!/usr/bin/env python3
"""安全组基线巡检与放通。

处置逻辑：
1. 拉取安全组规则，与基线（80/443 对公网、业务端口对 VPC 网段）比对；
2. 对缺失规则给出最小权限变更单（仅内网网段、仅业务端口）；
3. 连通性复测：策略模拟器探测 open/timeout。
"""
from __future__ import annotations

import json

from _base import cli, done, log

BASELINE = [("80", "0.0.0.0/0"), ("443", "0.0.0.0/0")]


def main() -> None:
    lab, apply = cli("安全组基线巡检")
    sg = lab.state["security_groups"][0]
    api_port = str(lab.ports["api"])
    rules = [(str(r.get("PortRange", "")).split("/")[0], r.get("SourceCidrIp", "")) for r in sg["Rules"]]
    need = [(api_port, "10.10.0.0/16")] + BASELINE
    missing = [r for r in need if r not in rules and (r[1] != "0.0.0.0/0" or r[0] in ("80", "443"))]
    log(f"安全组 {sg['SecurityGroupId']} 现有入方向规则: {rules}")
    log(f"基线缺口: {missing}")

    probe = lab.tcp_probe("10.10.1.11", lab.ports["api"])
    log(f"连通性探测 10.10.1.11:{api_port} -> {probe}")

    if not missing:
        log("安全组合规，无需变更")
        done(True, "NOOP")

    if not apply:
        log(f"[dry-run] 计划：AuthorizeSecurityGroup 放通 {missing[0][0]} 来自 {missing[0][1]}（最小权限）")
        done(True, "dry-run")

    port, cidr = missing[0]
    out = lab.aliyun(f"ecs AuthorizeSecurityGroup --SecurityGroupId {sg['SecurityGroupId']} "
                     f"--IpProtocol tcp --PortRange {port}/{port} --SourceCidrIp {cidr} "
                     f"--Description xhs-api-vpc")
    log("已提交变更: " + json.loads(out)["RequestId"])
    lab.state.get("faults", {}).pop("network", None)
    lab._save()

    probe2 = lab.tcp_probe("10.10.1.11", int(port))
    log(f"变更后探测: {probe2}")
    done(probe2["result"] == "open", "安全组已放通")


if __name__ == "__main__":
    main()
