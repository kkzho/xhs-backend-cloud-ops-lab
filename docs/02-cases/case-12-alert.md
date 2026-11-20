# 案例 12 · 日志采集断流 + 告警规则失配，告警风暴

> 分类：[F 可观测性与告警](../01-knowledge-base/06-observability.md) ｜ 等级：P2 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

`note-api` 日志经 Logtail 采集到 SLS（`xhs-prod/note-api`），云监控 CMS 基于日志与指标配置告警。
告警规则 `note-api CPU使用率` 当前为：阈值 80%、周期 60s、持续 3 个周期、静默 600s、按实例通知。
上周一次"压测演练"把规则临时改成阈值 1%、周期 10s、持续 0、静默 0，**没有改回去**。

### 1.2 触发诱因

- 规则阈值 1% + 无持续时间 + 无静默 → 每个采集周期都触发，且不合并；
- 同时 Logtail 因磁盘满被 OOM Kill 后未自动恢复，SLS 采集延迟升到 600s；
- 告警通知没有分组维度，240 条告警逐条推送，值班同学被刷屏；
- 因为日志断流，**真正的错误日志反而看不到**，排障失去第一手证据。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 09:00:00 | 云监控 CMS | `note-api CPU使用率` 持续触发（值 0.02%，阈值 1%） |
| 09:00–09:40 | 告警通道 | 累计 240 条重复告警，平均 10s 一条 |
| 09:05:00 | SLS | LogStore `IngestLagSec=600`，`Status=agent_stopped` |
| 09:12:00 | 值班同学 | IM 被刷屏，真实故障信息被淹没 |

## 二、运维中

### 2.1 处理逻辑线

```text
先止"告警风暴"：这不是故障本身，是噪音
   ↓
看规则：阈值/周期/For/静默 四项全错 → 恢复合理值 + 开分组
   ↓
再查"为什么没有日志"：Logtail 停止 → 恢复采集
   ↓
验证：采集延迟回落、告警数量归零
   ↓
根治：规则变更审批 + 规则基线巡检 + 采集组件自愈
```

### 2.2 逐步拆解

**Step 1 · 判断"风暴"还是"真故障"（2 分钟）**

```bash
aliyun cms DescribeAlarmHistory --StartTime "2026-11-20 08:50:00"
aliyun cms DescribeMetricRuleList
```

判断：告警值只有 0.02%，远低于业务真实水位 → **规则阈值被改坏**，
不是 CPU 真高。此时不要急着重启服务。

**Step 2 · 恢复采集（3 分钟）**

```bash
systemctl status logtail
# inactive (dead)，上次退出原因 OOM

journalctl -u logtail -n 20
systemctl start logtail
sleep 3
systemctl status logtail
```

```bash
# SLS 侧确认采集恢复
aliyun sls GetLogStore --Project xhs-prod --LogStore note-api
# IngestLagSec 应从 600 降到个位数
```

> 采集断流必须先修：没有日志，后续所有判断都是猜的。

**Step 3 · 收敛规则（3 分钟）**

```bash
# 恢复为：阈值 80%、周期 60s、持续 3 周期、静默 600s、按 instanceId 分组
aliyun cms PutResourceMetricRule \
  --RuleName "note-api CPU使用率" \
  --MetricName cpu_total --Threshold 80 --Period 60 --For 3 \
  --SilenceTime 600 --GroupBy instanceId
```

同时把"压测临时规则"纳入变更审批，禁止直接改生产规则。

**Step 4 · 验证（2 分钟）**

```bash
aliyun cms DescribeMetricRuleList | grep -A6 'note-api CPU'
aliyun cms DescribeAlarmHistory --StartTime "2026-11-20 09:20:00"   # 无新增
systemctl is-active logtail                                        # active
```

**Step 5 · 根因加固（当天）**

- 告警规则纳入 Git 管理（IaC），变更走 MR；
- 每日巡检：规则阈值是否在基线区间、静默是否 ≥ 300s、是否配置分组；
- Logtail 加 `Restart=always` + 采集延迟 > 60s 告警；
- 告警通道加"5 分钟内同类告警合并 + 升级"策略。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 先改规则再查业务 | 风暴会淹没真实告警，先降噪 | 可能短暂漏掉真实异常 |
| 恢复采集而非重建 LogStore | 保留 offset 与历史 | 需确认 offset 文件未损坏 |
| 规则入 Git 管理 | 杜绝"临时改生产" | 变更流程变长 |

## 三、运维后

### 3.1 复盘

- **直接原因**：压测临时规则未恢复；Logtail 停止后无自愈。
- **根本原因**：告警规则无版本管理与巡检；可观测性组件缺少健康监控。
- **做得好**：先识别"告警值不合理"，避免被噪音带着去重启服务。
- **待改进**：监控系统本身也要被监控（采集延迟、规则命中量）。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 告警规则 IaC + MR 审批 | 流程 | 变更可追溯 |
| 规则基线日巡检 | 监控 | 阈值/静默/分组合规 |
| Logtail `Restart=always` | 工程 | 异常退出 10s 内自愈 |
| 采集延迟 > 60s 告警 | 监控 | 断流 1 分钟内发现 |
| 告警合并与升级策略 | 工程 | 单事件通知 ≤ 3 条 |

### 3.3 自动化沉淀

脚本：[automation/alert_dedup.py](../../automation/alert_dedup.py)

```bash
python automation/alert_dedup.py --case case-12-alert --apply
```

逻辑：按规则维度聚合告警识别失配 → 恢复 Logtail 采集 → 收敛规则（阈值/For/静默/分组）
→ 校验告警数与采集延迟 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-12-alert/lab.ipynb](../../labs/case-12-alert/lab.ipynb)

1. `lab.inject_alert_storm()` 停止 Logtail、写入 240 条重复告警、把规则改成阈值 1%/无静默；
2. `lab.sh("systemctl status logtail")`、`lab.aliyun("cms DescribeMetricRuleList")` 复现；
3. `alert_dedup.py --apply` 完成降噪与采集恢复；
4. 断言：采集延迟 < 60s、规则阈值恢复 80、告警数不再增长。

## 五、思考题

1. 告警风暴期间，如何在不丢真实告警的前提下快速降噪？
2. 为什么"监控组件自身"也需要被监控？列出 3 个关键自监控指标。
3. 如何设计告警规则，使"压测"与"生产"互不污染？
