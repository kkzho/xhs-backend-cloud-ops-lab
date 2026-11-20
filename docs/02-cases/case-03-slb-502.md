# 案例 03 · SLB 后端健康检查失败，接口全量 502

> 分类：[B 网络与负载均衡](../01-knowledge-base/02-network.md) ｜ 等级：P0 ｜ 角色：一线值班

## 一、运维前

### 1.1 业务背景

`xhs-api-slb`（七层 HTTPS）后面挂 2 台 `note-api`，健康检查 `GET /healthz`，
间隔 2s、超时 1s、连续 2 次失败摘除。当天 DBA 对 RDS 做参数调整，导致
`note-api` 的数据库连接在 1 分钟内全部超时。

### 1.2 触发诱因

- RDS 参数调整触发连接闪断，应用连接池未配置重连；
- `/healthz` 实现为"检查 DB 连接"，DB 不可用时健康检查返回 500；
- 两台实例同时受影响，SLB 后端全部 `abnormal`，返回 502。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 14:21:10 | SLB 监控 | 后端 `i-bp1xhsapi01`、`-02` 健康检查失败 |
| 14:21:12 | SLB 访问日志 | `SLB_502` 比例 100% |
| 14:21:30 | App 监控 | 首页接口错误率 98%，P0 告警 |
| 14:21:45 | 用户反馈 | 全站无法刷新 |

## 二、运维中

### 2.1 处理逻辑线

```text
确认全站 502（SLB 层）→ 检查后端健康状态
   ↓
直连后端 /healthz：500（不是网络问题）
   ↓
看应用日志：大量 DB 连接超时 / pool exhausted
   ↓
确认 RDS 侧变更（DBA 参数调整）→ 联系 DBA 回滚/恢复
   ↓
应用侧重启恢复连接池（或等待重连）
   ↓
SLB 连续 2 次健康后自动挂回，验证业务 200
```

### 2.2 逐步拆解

**Step 1 · 分层定位（2 分钟）**

```bash
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
# ServerHealthStatus: abnormal / abnormal

curl -sS -m 3 http://127.0.0.1:8080/healthz
# {"status":"fail","reason":"db_timeout"}   ← 本机也不健康，排除 SLB 配置问题

curl -sS -m 3 -o /dev/null -w "%{http_code}\n" http://10.10.1.11:8080/healthz
# 500  ← 私网访问同样 500，排除安全组
```

**Step 2 · 应用取证（3 分钟）**

```bash
tail -n 100 /var/log/xhs/note-api.log | grep -E "ERROR|timeout"
# pymysql.err.OperationalError: (2013, 'Lost connection to MySQL server during query')
# pool exhausted: active=20 idle=0 wait=147

mysql -h rm-bp1xhsfeed01 -u xhs_app -p -e "SHOW PROCESSLIST"
# 连接数为 3（远低于上限），说明是网络/参数导致的连接失败，不是打满
```

**Step 3 · 关联变更（1 分钟）**

与 DBA 确认：14:19 调整了 `wait_timeout` 与连接数上限并触发了实例重启。故障时间完全吻合。

**Step 4 · 恢复**

1. DBA 恢复参数/等待实例可用；
2. 应用侧重启（重建连接池）；
3. 观察 SLB 健康状态：

```bash
watch -n 2 'aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01 | grep ServerHealthStatus'
```

**Step 5 · 业务验证**

```bash
for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" https://api.xiaohongshu-lab.com/api/v1/feed; done
# 200 0.052 / 200 0.048 / 200 0.061
```

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 不等 DBA，先重启应用 | 连接池已损坏，重启最快恢复 | 冷启动 10s，容量短时下降 |
| 健康检查保留 DB 依赖 | 能真实反映可用性 | DB 抖动会放大为全站 502（需配合降级） |
| 未直接摘除全部后端 | 摘除后 SLB 无可用后端，仍是 502 | 无意义 |

## 三、运维后

### 3.1 复盘

- **直接原因**：RDS 参数变更导致连接闪断，应用连接池未重连；
- **根本原因**：`/healthz` 强依赖 DB，DB 抖动直接导致全站摘除；缺少变更联动与连接池自愈；
- **做得好**：分层排查快速排除 SLB/安全组；与 DBA 联动及时。
- **待改进**：没有 DB 变更的灰度/通知机制；健康检查缺少"浅健康"与"深健康"分层。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| `/healthz` 只检查进程与关键依赖（浅），DB 用 `/readyz` | 架构 | DB 抖动不直接摘除全部后端 |
| 连接池开启 `health_check` + 自动重连 | 代码 | 闪断后 30s 内自愈 |
| RDS 变更纳入变更日历并通知业务方 | 流程 | 变更前 30 分钟通知 |
| SLB 5xx 与后端健康双指标告警 | 监控 | 1 分钟内发现 |
| DB 故障降级开关（读缓存/静态兜底） | 架构 | DB 不可用时核心接口可降级 |

### 3.3 自动化沉淀

脚本：[automation/slb_recover.py](../../automation/slb_recover.py)

```bash
python automation/slb_recover.py --case case-03-slb-502 --apply
```

逻辑：SLB 健康状态 + 直连探活 + 经 SLB 探活 → 摘除异常后端 → 重启 → 等待连续健康 → 挂回 → 校验 200。

## 四、演练

Notebook：[labs/case-03-slb-502/lab.ipynb](../../labs/case-03-slb-502/lab.ipynb)

沙箱启动真实七层转发进程，`inject_slb_backend_unhealthy()` 让后端 `/healthz` 返回 500，
2 次检查后摘除，经 SLB 的请求真实返回 `SLB_502`。执行脚本后恢复 200。

## 五、思考题

1. 健康检查应该检查多深？检查 DB 的利弊分别是什么？
2. 如果只有一台后端且健康检查失败，SLB 应该返回什么？如何避免全站 502？
3. 502 与 504 的排查路径有什么不同？
