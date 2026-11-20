# 案例 02 · 内存泄漏触发 OOM Kill，服务反复重启

> 分类：[A 计算与实例](../01-knowledge-base/01-compute.md) ｜ 等级：P1 ｜ 角色：一线值班 → 二线

## 一、运维前

### 1.1 业务背景

`note-api` 在 11 月上线"笔记详情本地缓存"后，单实例日均内存从 400MB 涨到 1.8GB。
实例规格 8C32G，应用未设置 `MemoryMax`。凌晨低峰期问题暴露：服务每 12~18 分钟被
OOM Kill 一次，systemd 自动拉起，监控呈锯齿状。

### 1.2 触发诱因

- 新加的进程内缓存 `dict` 无上限、无 TTL，key 为 `note_id`，热门笔记详情被反复写入；
- 大对象（详情 JSON + 图片元信息）单条约 200KB，缓存条目数突破 10 万；
- 未配置内存水位告警，只监控了 CPU 与接口错误率。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 02:14:03 | 云监控 CMS | `memory_usedutilization` > 90%，持续 5 分钟 |
| 02:14:50 | 系统 | `Out of memory: Killed process 22107 (python3)` |
| 02:15:02 | systemd | `note-api.service: Main process exited, code=killed, signal=9` |
| 02:15:04 | systemd | `note-api.service: Scheduled restart job`（Restart=always） |
| 02:16:10 | SLB | 后端健康检查短暂失败 1 次后恢复 |

## 二、运维中

### 2.1 处理逻辑线

```text
确认是单机还是全部实例（本例仅 -01 触发，另一实例内存 3.2G 也接近水位）
   ↓
看内存曲线形态：锯齿 = 泄漏，阶梯 = 流量；确认与发布相关
   ↓
dmesg / journalctl 取 OOM 证据，确认被杀进程
   ↓
止血：SLB 摘除 → 重启 → 观察 RSS → 挂回
   ↓
保留现场（RSS 曲线、日志、必要时 py-spy/heap）
   ↓
根治：缓存加 LRU + TTL + 上限；补内存告警与 MemoryMax
```

### 2.2 逐步拆解

**Step 1 · 取证（3 分钟）**

```bash
dmesg -T | grep -i -E "out of memory|killed process" | tail -3
# [Thu Nov 20 02:14:50 2026] Out of memory: Killed process 22107 (python3) ...

systemctl status xhs-note-api
# Active: active (running) ... Restarts: 6

ps -o pid,rss,vsz,etime,cmd -p $(pgrep -f note-api)
#   PID   RSS    VSZ  ELAPSED CMD
# 22412 182340 512304  00:03 python3 -m ...
```

**Step 2 · 判断性质**

```bash
# 观察 2 分钟 RSS 增长
for i in 1 2 3 4; do ps -o rss= -p $(pgrep -f note-api); sleep 30; done
# 182340 → 428112 → 701988 → 962340  （单调上涨，重启归零）
```

结论：RSS 单调上涨、与 QPS 无相关性、重启后从低点重新爬升 → **内存泄漏**，不是流量峰值。

**Step 3 · 止血（5 分钟）**

1. SLB 摘除 `i-bp1xhsapi01`；
2. `systemctl restart xhs-note-api`（或滚动重启）；
3. 直连 `/healthz` 与业务冒烟通过后挂回。

> 顺序不能反：先摘流量再重启，否则重启期间用户请求会打到不可用实例。

**Step 4 · 保留现场**

```bash
# 泄漏期间（重启前）尽量采集
py-spy dump --pid 22107 > /tmp/note-api-stack.txt
# 或开启 tracemalloc / 堆快照（按语言选择 jmap、pprof）
cp /var/log/xhs/note-api.log /tmp/incident-20261120/
```

**Step 5 · 根治**

- 缓存改为 `LRU(maxsize=50000, ttl=600s)`，并对 value 做大小上限（拒绝 > 512KB）；
- 大 JSON 只缓存必要字段；
- systemd 增加 `MemoryMax=1G`、`MemoryHigh=800M`，防止拖垮整机；
- 云监控增加"1 小时 RSS 增长 > 30%"告警。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 先摘流再重启 | 避免重启窗口内 502 | 容量暂时 -50% |
| 重启而非等待 | 泄漏无法在线回收 | 缓存冷启动，DB 压力短时上升 |
| 临时调大内存？ | 只延后 OOM，且会影响同机服务 | 未采用 |

## 三、运维后

### 3.1 复盘

- **直接原因**：无上限进程内缓存导致 RSS 持续增长；
- **根本原因**：缓存设计缺约束；缺少内存水位/增长速率告警；无内存限制；
- **做得好**：`Restart=always` 保证了最低可用性；SLB 健康检查快速摘除；
- **待改进**：OOM 后未自动摘流，重启期间仍有请求进入。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 缓存 LRU + TTL + 单 value 上限 | 代码 | 压测 2 小时 RSS 波动 < 10% |
| `MemoryMax` / `MemoryHigh` | 系统 | OOM 只影响本服务 |
| RSS 增长速率告警 | 监控 | 30 分钟内可发现 |
| 发布前内存基线对比 | 流程 | 峰值涨幅 > 20% 需评审 |
| 定期 heap 采样 | 可观测 | 能定位 Top 对象 |

### 3.3 自动化沉淀

脚本：[automation/memory_guard.py](../../automation/memory_guard.py)

```bash
python automation/memory_guard.py --case case-02-memory-oom --apply
```

逻辑：读取 RSS/OOM 次数 → 超水位则 SLB 摘除 → 重启 → 写入 `MemoryMax` → RSS 回落校验 → 挂回。

## 四、演练

Notebook：[labs/case-02-memory-oom/lab.ipynb](../../labs/case-02-memory-oom/lab.ipynb)

沙箱用真实进程持续申请内存，超过水位后由看门狗 SIGKILL 并拉起（写入 `dmesg.log`）。
执行 `memory_guard.py --apply` 后断言：RSS < 120MB、OOM 记录存在、服务健康。

## 五、思考题

1. 如何区分"内存泄漏"与"缓存正常增长"？
2. 为什么 `Restart=always` 不能替代根因修复？
3. 如果没有 `py-spy`，你会用什么方式保留现场？
