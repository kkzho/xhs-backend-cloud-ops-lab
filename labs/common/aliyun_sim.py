"""阿里云 OpenAPI 模拟器（ECS / SLB / RDS / Redis / CMS / CAS / SLS）。

返回值字段与真实 OpenAPI 对齐，便于在 Notebook 中练习 CLI 取证；
本模块不连接阿里云，所有数据来自沙箱 state.json（响应中带 MOCK 标记）。
"""
from __future__ import annotations

import json
import random
import time


def _rid() -> str:
    return f"{random.randint(0x10000000, 0xFFFFFFFF):08X}-{random.randint(0x1000, 0xFFFF):04X}-4A3E-B7C1-{random.randint(0x100000000000, 0xFFFFFFFFFFFF):012X}"


def _args(tokens: list[str]) -> dict:
    out, i = {}, 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:]
            val = tokens[i + 1] if i + 1 < len(tokens) and not tokens[i + 1].startswith("--") else "true"
            out[key] = val
            i += 2 if val != "true" else 1
        else:
            i += 1
    return out


def _metric_value(state: dict, metric: str) -> float:
    faults = state.get("faults", {})
    if metric in ("cpu_total", "CpuTotal", "cpu_utilization"):
        return 96.4 if "cpu" in faults else 23.1
    if metric in ("memory_usedutilization", "MemoryUsedPercent"):
        return 92.7 if "memory" in faults else 41.3
    if metric in ("diskusage_utilization", "DiskUsage"):
        return 97.9 if "disk" in faults else 36.5
    if metric in ("ConnectionUsage", "connection_usage"):
        return 100.0 if "rds" in faults else float(state["rds"].get("ConnectionUsage", 24))
    if metric in ("MemoryUsage", "memory_usage"):
        return 100.0 if "redis" in faults else 25.0
    if metric in ("sls_ingest_lag", "IngestLag"):
        return 612.0 if state.get("logstore", {}).get("Status") != "normal" else 2.0
    if metric in ("RequestTime", "rt"):
        return 820.0 if "cpu" in faults else 46.0
    return 12.0


def _datapoints(state: dict, metric: str, n: int = 12) -> str:
    base = _metric_value(state, metric)
    now = int(time.time())
    pts = []
    for i in range(n):
        val = base * (0.55 + 0.45 * (i + 1) / n) + random.uniform(-1.5, 1.5)
        pts.append({"timestamp": (now - 60 * (n - i)) * 1000,
                    "instanceId": state["ecs"][0]["InstanceId"],
                    "Average": round(max(val, 0), 2),
                    "Maximum": round(max(val, 0) * 1.08, 2)})
    return json.dumps(pts, ensure_ascii=False)


def render(state: dict, cmd: str, save=None) -> str:
    tokens = cmd.split()
    if len(tokens) < 3:
        return json.dumps({"error": "usage: aliyun <service> <Action> [--Key Value]"}, ensure_ascii=False)
    service, action = tokens[1].lower(), tokens[2]
    a = _args(tokens[3:])
    resp: dict

    if service == "ecs" and action == "DescribeInstances":
        resp = {"RequestId": _rid(), "TotalCount": len(state["ecs"]), "PageNumber": 1, "PageSize": 10,
                "Instances": {"Instance": [
                    {**i, "InstanceChargeType": "PrePaid", "OSName": "Alibaba Cloud Linux 3",
                     "CreationTime": "2025-03-11T09:12Z", "Mock": True} for i in state["ecs"]]}}

    elif service == "ecs" and action == "DescribeSecurityGroups":
        resp = {"RequestId": _rid(), "TotalCount": len(state["security_groups"]),
                "SecurityGroups": {"SecurityGroup": [
                    {"SecurityGroupId": sg["SecurityGroupId"], "SecurityGroupName": sg["SecurityGroupName"],
                     "VpcId": state["meta"]["vpc"], "Permissions": {"Permission": sg["Rules"]}}
                    for sg in state["security_groups"]]}}

    elif service == "ecs" and action in ("AuthorizeSecurityGroup", "RevokeSecurityGroup"):
        sgid = a.get("SecurityGroupId", state["security_groups"][0]["SecurityGroupId"])
        sg = next((s for s in state["security_groups"] if s["SecurityGroupId"] == sgid), state["security_groups"][0])
        rule = {"Direction": "ingress", "IpProtocol": a.get("IpProtocol", "tcp"),
                "PortRange": a.get("PortRange", "80/80"), "SourceCidrIp": a.get("SourceCidrIp", "0.0.0.0/0"),
                "Description": a.get("Description", "manual")}
        if action == "AuthorizeSecurityGroup":
            if rule not in sg["Rules"]:
                sg["Rules"].append(rule)
        else:
            sg["Rules"] = [r for r in sg["Rules"] if not (
                r.get("PortRange") == rule["PortRange"] and r.get("IpProtocol") == rule["IpProtocol"])]
        if save:
            save()
        resp = {"RequestId": _rid(), "Mock": True, "SecurityGroupId": sgid,
                "Rules": sg["Rules"]}

    elif service == "cms" and action == "QueryMetricList":
        metric = a.get("MetricName", "cpu_total")
        resp = {"RequestId": _rid(), "Code": "200", "Success": True,
                "Period": "60", "MetricName": metric,
                "Datapoints": _datapoints(state, metric), "Mock": True}

    elif service == "cms" and action == "DescribeAlarmHistory":
        resp = {"RequestId": _rid(), "Success": True, "Code": "200",
                "Total": len(state.get("alarms", [])),
                "AlarmHistoryList": {"AlarmHistory": state.get("alarms", [])[-20:]}, "Mock": True}

    elif service == "cms" and action == "DescribeMetricRuleList":
        resp = {"RequestId": _rid(), "Success": True, "Total": len(state["alarm_rules"]),
                "AlarmList": {"Alarm": state["alarm_rules"]}, "Mock": True}

    elif service == "slb" and action == "DescribeHealthStatus":
        backends = state["slb"]["BackendServers"]
        resp = {"RequestId": _rid(), "TotalCount": len(backends),
                "BackendServers": {"BackendServer": [
                    {"ServerId": b["ServerId"], "Port": b["Port"], "Weight": b["Weight"],
                     "ServerHealthStatus": b.get("HealthStatus", "normal")} for b in backends]}, "Mock": True}

    elif service == "slb" and action == "DescribeLoadBalancerAttribute":
        lb = state["slb"]
        resp = {"RequestId": _rid(), "LoadBalancerId": lb["LoadBalancerId"],
                "LoadBalancerName": lb["LoadBalancerName"], "Address": lb["Address"],
                "AddressType": lb["AddressType"], "ListenerPorts": {"ListenerPort": [l["ListenerPort"] for l in lb["Listeners"]]},
                "BackendServers": {"BackendServer": lb["BackendServers"]},
                "HealthCheck": lb["HealthCheck"], "Mock": True}

    elif service == "slb" and action in ("RemoveBackendServers", "AddBackendServers"):
        lb = state["slb"]
        sid = a.get("BackendServers", "").strip("[]").replace('"', "")
        if action == "RemoveBackendServers":
            lb["BackendServers"] = [b for b in lb["BackendServers"] if b["ServerId"] != sid]
        elif not any(b["ServerId"] == sid for b in lb["BackendServers"]):
            lb["BackendServers"].append({"ServerId": sid, "Port": state["services"].get("xhs-note-api", {}).get("port", 8080),
                                         "Weight": 100, "HealthStatus": "normal"})
        if save:
            save()
        resp = {"RequestId": _rid(), "LoadBalancerId": lb["LoadBalancerId"],
                "BackendServers": {"BackendServer": lb["BackendServers"]}, "Mock": True}

    elif service == "rds" and action == "DescribeDBInstanceAttribute":
        r = state["rds"]
        resp = {"RequestId": _rid(), "Items": {"DBInstanceAttribute": [{
            "DBInstanceId": r["DBInstanceId"], "Engine": r["Engine"], "DBInstanceClass": r["Class"],
            "MaxConnections": r["MaxConnections"], "ConnectionMode": "Standard",
            "DBInstanceStatus": "Running", "ZoneId": "cn-hangzhou-h"}]}, "Mock": True}

    elif service == "rds" and action == "DescribeDBInstancePerformance":
        key = a.get("Key", "ConnectionUsage")
        resp = {"RequestId": _rid(), "DBInstanceId": state["rds"]["DBInstanceId"],
                "PerformanceKeys": {"PerformanceKey": [{
                    "Key": key, "Unit": "%" if "Usage" in key else "count",
                    "Values": {"PerformanceValue": [
                        {"Value": f"{_metric_value(state, key):.2f}", "Date": time.strftime("%Y-%m-%dT%H:%MZ")}]}}]}, "Mock": True}

    elif service == "rds" and action == "DescribeSlowLogs":
        resp = {"RequestId": _rid(), "TotalRecordCount": 3, "Items": {"SQLSlowLog": [
            {"SQLText": "SELECT * FROM note WHERE tag_id IN (...) ORDER BY score DESC LIMIT 500",
             "SQLTime": "2.812", "MaxExecutionTime": "3.104", "MySQLTotalExecutionCounts": 1842},
            {"SQLText": "SELECT COUNT(*) FROM follow WHERE user_id = ?", "SQLTime": "1.204",
             "MaxExecutionTime": "1.882", "MySQLTotalExecutionCounts": 9921},
            {"SQLText": "UPDATE feed_rank SET score = ? WHERE note_id = ?", "SQLTime": "0.902",
             "MaxExecutionTime": "1.221", "MySQLTotalExecutionCounts": 20431}]}, "Mock": True}

    elif service == "kvstore" and action == "DescribeInstanceAttribute":
        r = state["redis"]
        resp = {"RequestId": _rid(), "Instances": {"DBInstanceAttribute": [{
            "InstanceId": r["InstanceId"], "InstanceClass": r["Class"],
            "InstanceStatus": "Normal", "Capacity": r["MaxMemoryMB"],
            "Bandwidth": 96, "Connections": 312}]}, "Mock": True}

    elif service == "kvstore" and action in ("DescribeHistoryMonitorValues", "DescribeMonitorValues"):
        r = state["redis"]
        used = _metric_value(state, "MemoryUsage")
        resp = {"RequestId": _rid(), "InstanceId": r["InstanceId"],
                "MonitorKeys": {"MonitorKey": [
                    {"Key": "MemoryUsage", "Unit": "%", "Values": {"MonitorValue": [{"Value": f"{used:.1f}"}]}},
                    {"Key": "HitRate", "Unit": "%", "Values": {"MonitorValue": [{"Value": f"{r.get('HitRate', 0.98) * 100:.1f}"}]}},
                    {"Key": "EvictedKeys", "Unit": "count", "Values": {"MonitorValue": [{"Value": str(r.get("EvictedKeys", 0))}]}}]},
                "Mock": True}

    elif service == "cas" and action in ("ListUserCertificateOrder", "ListCloudResources"):
        resp = {"RequestId": _rid(), "TotalCount": len(state["certs"]),
                "CertificateOrderList": state["certs"], "Mock": True}

    elif service == "sls" and action in ("GetLogStore", "GetLogStoreAttribute"):
        ls = state["logstore"]
        resp = {"RequestId": _rid(), "Project": ls["Project"], "LogStoreName": ls["LogStore"],
                "Status": ls.get("Status", "normal"), "IngestLagSec": ls.get("IngestLagSec", 1),
                "ShardCount": 4, "TTL": 30, "Mock": True}

    else:
        resp = {"RequestId": _rid(), "Mock": True,
                "Message": f"action {service}.{action} 已在沙箱模拟器中实现/占位",
                "Args": a}

    return json.dumps(resp, ensure_ascii=False, indent=2)
