# F · 可观测性与告警

覆盖云监控（CMS）、日志服务（SLS）、告警规则与通知。核心原则：
**先确认"数据是否可信"（采集是否断流），再判断"规则是否合理"，最后才是处置业务。**

---

## F1 · 日志断流 + 告警规则失配，引发告警风暴

**业务现象**
值班群 10 分钟内收到数百条相同告警；告警内容指标值极低（如 CPU 1% 也告警）；
同时日志平台查不到最近日志，SLS 采集延迟持续升高；排障时"既被刷屏、又没有日志"。

**影响与定级**
P2（噪声掩盖真实告警）；若因此漏掉真实 P0，则升级为事故。

**常见根因**
1. 日志采集 Agent（Logtail/fluent-bit）停止或 offset 卡住，日志断流；
2. 告警规则阈值过低（如 `> 0`）、无 `for` 持续时长、无静默期；
3. 未配置分组（按实例/接口聚合），同一问题每条数据点都触发；
4. 通知渠道无频控/降级策略；
5. 指标采集异常（NaN/默认值 0）被当作真实值告警。

**首查命令**

```bash
systemctl status logtail
ps -ef | grep -E "logtail|fluent-bit" | grep -v grep
tail -n 50 /var/log/logtail/logtail.log
# 观察日志文件是否增长、offset 是否推进
ls -l /data/logs/note-api.log
```

云侧取证：

```bash
aliyun sls GetLogStore --Project xhs-prod --LogStore note-api
aliyun cms DescribeAlarmHistory --StartTime 2026-11-20T00:00Z
aliyun cms DescribeMetricRuleList
```

**判断依据**
- 日志文件在增长但平台无新日志 → 采集断流（而非业务不打日志）；
- 告警值明显失真（CPU 2% 触发）→ 规则/指标问题；
- 同一规则短时间重复数百条 → 缺少 `for` 与分组/静默；
- 采集延迟与告警风暴同时出现 → 同一根因（Agent 异常导致指标/日志失真）。

**处置要点**
1. **先恢复采集**：重启 Agent，确认 offset 追赶（lag 回落）；
2. 止血噪声：对规则临时静默/去重，保留 P0 通道；
3. 修规则：合理阈值 + `for 3m` + `silence 10m` + 按 `instanceId` 分组；
4. 增加"日志采集延迟"自身的告警（元监控）；
5. 复盘通知风暴对真实告警的影响，明确升级路径。

**预防与自动化**
- 元监控：日志 lag、Agent 存活、告警规则变更审计；
- 告警规范：每条规则必须有 owner、runbook、for、silence、分组；
- 自动化：`automation/alert_dedup.py`（聚合告警 → 恢复采集 → 收敛规则 → 校验 lag）。

**关联案例**：[case-12](../02-cases/case-12-alert.md) · [Notebook](../../labs/case-12-alert/lab.ipynb)
