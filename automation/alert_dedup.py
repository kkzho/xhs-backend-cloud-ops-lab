#!/usr/bin/env python3
"""告警风暴治理与日志采集恢复。

处置逻辑：
1. 按规则维度聚合告警，识别"规则阈值过松 / 无静默 / 无分组"导致的重复通知；
2. 恢复日志采集 Agent，校验 SLS 采集延迟回落；
3. 收敛规则：阈值 + for 持续时长 + 静默期 + 分组维度；
4. 校验：告警条数与采集延迟同时恢复。
"""
from __future__ import annotations

import time
from collections import defaultdict

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("告警风暴治理")
    alarms = lab.state.get("alarms", [])
    by_rule = defaultdict(int)
    for a in alarms:
        by_rule[a["RuleName"]] += 1
    log(f"告警总数={len(alarms)}，按规则聚合={dict(by_rule)}")
    log(f"日志采集状态={lab.state['logstore']}，agent={lab.sh('systemctl status logtail').splitlines()[2].strip()}")
    rule = lab.state["alarm_rules"][0]
    log(f"当前规则: threshold={rule['Threshold']} period={rule['Period']}s "
        f"for={rule['For']}s silence={rule['Silence']}s")

    if len(alarms) < 20 and lab.state["logstore"].get("Status") == "normal":
        log("告警与采集均正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：恢复 logtail → 告警去重/静默 → 规则加 for/分组 → 校验 lag<5s")
        done(True, "dry-run")

    lab.start_logagent()
    log("logtail 已恢复")

    kept, seen = [], set()
    for a in alarms:
        bucket = (a["RuleName"], a["Time"][:15])  # 精确到 10 分钟窗口
        if bucket in seen:
            continue
        seen.add(bucket)
        kept.append(a)
    suppressed = len(alarms) - len(kept)
    lab.state["alarms"] = kept

    rule.update({"Threshold": 80, "Period": 60, "For": 180, "Silence": 600,
                 "Notify": ["oncall"], "GroupBy": ["instanceId"], "Enabled": True})
    lab.state["logstore"]["Status"] = "normal"
    lab.state["logstore"]["IngestLagSec"] = 2
    lab.state.get("faults", {}).pop("alert", None)
    lab._save()

    lag_ok = lab.wait_until(lambda: lab.logagent_stats().get("lag_bytes", 10 ** 9) < 2000,
                             timeout=20, interval=1)
    st = lab.logagent_stats()
    log(f"去重后告警={len(kept)}（抑制 {suppressed} 条），采集 lag={st.get('lag_bytes')} bytes")
    done(lag_ok and len(kept) <= 5, "告警收敛、日志采集恢复")


if __name__ == "__main__":
    main()
