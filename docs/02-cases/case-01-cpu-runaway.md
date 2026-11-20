# 案例 01 · 榜单任务并发导致 CPU 打满，Feed 接口超时

> 分类：[A 计算与实例](../01-knowledge-base/01-compute.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

小红书笔记 Feed 依赖"榜单预计算"任务：每分钟扫描近 1 小时互动数据，重算
`feed_rank` 并写入缓存。任务部署在 `xhs-note-api-01`（8C32G）的 crontab 中，
与在线接口混部。当晚有明星发布爆款笔记，互动数据量约为日常 6 倍。

### 1.2 触发诱因

- 运营同学为"活动榜单"临时把任务频率从 5 分钟调到 1 分钟；
- 脚本无 `flock` 锁：上一轮未结束，下一轮已启动，进程数线性叠加；
- 任务内部全表扫描 + 无 `LIMIT`，单轮耗时从 40s 涨到 3 分钟；
- 混部导致在线接口 CPU 配额被挤压。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 21:04:12 | 云监控 CMS | `cpu_total` > 90%，持续 3 分钟，实例 `i-bp1xhsapi01` |
| 21:05:30 | SLB | 后端 `i-bp1xhsapi01` 健康检查失败（响应 > 1s），已摘除 |
| 21:06:02 | 应用监控 | Feed P99 820ms（基线 120ms），错误率 3.1% |
| 21:07:45 | 用户反馈 | App 首屏"加载失败，请重试" |

## 二、运维中

### 2.1 处理逻辑线

```text
确认影响面（单实例 → 已摘除，整体容量 -50%）
   ↓
看 load/CPU 与进程，判断"资源不够"还是"进程异常"
   ↓
发现 4 个 python 任务进程，启动时间间隔 1 分钟 → 定时任务叠加
   ↓
确认 cron 配置（1 分钟频率、无锁）
   ↓
止血：kill 任务进程 → 恢复在线接口
   ↓
改造任务：flock 串行 + nice 降优先级 + --limit 限流 + 错峰
   ↓
验证：CPU 回落、SLB 健康、P99 恢复
```

### 2.2 逐步拆解

**Step 1 · 确认影响面与时间线（2 分钟）**

```bash
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
aliyun cms QueryMetricList --Namespace acs_ecs_dashboard --MetricName cpu_total \
  --Dimensions '[{"instanceId":"i-bp1xhsapi01"}]'
```

结论：只有 `-01` 异常，SLB 已自动摘除；`-02` 仍在服务，属于**容量减半**而非全站故障 → P1。

**Step 2 · 主机层取证（3 分钟）**

```bash
uptime
# 21:12:03 up 128 days, load average: 31.42, 28.87, 19.95   ← 8 核跑到 31，严重超载

ps -eo pid,ppid,user,%cpu,etime,cmd --sort=-%cpu | head -10
# 41201  1  xhs  392  04:12  python3 jobs/ranking_precompute.py
# 41288  1  xhs  388  03:11  python3 jobs/ranking_precompute.py
# 41302  1  xhs  381  02:09  python3 jobs/ranking_precompute.py
# 41355  1  xhs  376  01:08  python3 jobs/ranking_precompute.py

cat /etc/cron.d/xhs-ranking
# */1 * * * * xhs cd /srv/xhs && python3 jobs/ranking_precompute.py
```

判断：4 个同类进程、启动时间间隔 1 分钟、CPU 各接近 4 核 → **任务并发叠加**，不是流量问题
（QPS 与基线持平）。

**Step 3 · 止血（1 分钟）**

```bash
kill 41201 41288 41302 41355      # 先 TERM，让任务写完当前批次
sleep 5
ps -eo pid,%cpu,cmd --sort=-%cpu | head -3
```

> 为什么不用 `pkill -f ranking_precompute`：同机还有数据导出任务，模糊匹配有误杀风险。
> 为什么先 TERM：任务写 `feed_rank` 有事务，SIGKILL 可能留下半成品数据。

**Step 4 · 恢复确认（5 分钟）**

```bash
uptime                                  # load 回落到 4.x
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" http://127.0.0.1:8080/api/v1/feed
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01   # normal
```

**Step 5 · 根因加固（当天）**

```bash
# 改造后的 crontab
*/5 * * * * xhs flock -n /var/lock/ranking.lock nice -n 10 \
  python3 /srv/xhs/jobs/ranking_precompute.py --limit 2000
```

同时把在线服务与批任务拆分到不同实例（或给任务加 cgroup CPU 限额）。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 直接 kill 而不是扩容 | 扩容 5 分钟起，且根因是任务叠加，扩容会被继续吃满 | 本轮榜单数据延迟 |
| 先 TERM 后观察 | 保护数据一致性 | 多等 5 秒 |
| 频率从 1 分钟改回 5 分钟 | 恢复原始 SLA，避免再次叠加 | 榜单实时性下降，需与运营确认 |

## 三、运维后

### 3.1 复盘

- **直接原因**：cron 频率调整 + 无锁 → 任务并发；数据量 6 倍放大单轮耗时。
- **根本原因**：批任务与在线服务混部、缺少并发保护与资源隔离；变更无评审。
- **做得好**：SLB 自动摘除避免了全站故障；先取证后 kill，保留了现场。
- **待改进**：CPU 告警只有 90% 一档，没有"非工作时段/批任务"维度；容量评估缺失。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 批任务统一 `flock` + 限流 + 错峰 | 工程 | 任意时刻同类任务进程 ≤ 1 |
| 任务与在线服务分实例部署 | 架构 | 在线实例 CPU 基线 < 40% |
| CPU 85%/95% 两级告警 + 批任务单独告警 | 监控 | 5 分钟内可定位到进程 |
| 定时任务变更走审批并记录 | 流程 | 变更可追溯 |
| 任务耗时/处理量上报 | 可观测 | 单轮耗时 > 2 分钟告警 |

### 3.3 自动化沉淀

脚本：[automation/cpu_guard.py](../../automation/cpu_guard.py)

```bash
python automation/cpu_guard.py --case case-01-cpu-runaway --apply
```

逻辑：采样 1s → 识别业务组内高 CPU 进程 → 记录现场 → 终止 → 改写 crontab（flock/nice/limit）
→ 校验残留进程与业务探活 → 输出 `RESULT: REMEDIATED` 与退出码。

## 四、演练

Notebook：[labs/case-01-cpu-runaway/lab.ipynb](../../labs/case-01-cpu-runaway/lab.ipynb)

1. 运行全部 Cell：`lab.inject_cpu_runaway()` 会启动 4 个真实忙循环进程；
2. 观察 `lab.sh("ps aux --sort=-%cpu")` 与 CMS 指标；
3. 执行 `cpu_guard.py --apply` 完成处置；
4. 断言：异常进程为 0、`/healthz` 200。

**生产等价命令**：把 `lab.sh()` 换成 ECS 上的 `ps/top/kill`，`lab.aliyun()` 换成真实 `aliyun` CLI。

## 五、思考题

1. 如果异常进程是 Java 应用且 CPU 高但流量正常，你会怎么进一步定位（提示：线程栈）？
2. 为什么"CPU 高"不等于"要扩容"？什么信号才说明需要扩容？
3. 如果 kill 后任务必须补跑，如何保证不再次叠加？
