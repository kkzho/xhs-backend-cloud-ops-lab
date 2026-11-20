# 案例 07 · 会话临时文件耗尽 inode，磁盘"有空间却写不进去"

> 分类：[C 存储与文件系统](../01-knowledge-base/03-storage.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

笔记编辑器的"草稿自动保存"会在 `/data/sessions` 下为每个会话创建临时文件，
正常流程在用户提交或会话过期后删除。年初上线了"游客模式"，游客会话没有过期清理逻辑，
文件只增不减。`/data` 同时承载日志与缓存。

### 1.2 触发诱因

- 游客模式上线后，单日新增会话文件约 1.2 万个；
- 清理任务只处理 `user_id` 非空的目录，游客文件成为"孤儿"；
- 云盘为 ext4，inode 总数随容量固定（沙箱按比例限制为 20000）；
- 监控只看 `df -h`（空间），没有 `df -i`（inode）指标。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 10:41:20 | 云监控 CMS | `diskusage_utilization` = 41%（正常） |
| 10:42:03 | 应用日志 | `OSError: [Errno 28] No space left on device (inode exhausted), path=/data/sessions` |
| 10:43:10 | SLB | 健康检查失败，摘除 `i-bp1xhsapi01` |
| 10:44:00 | 用户反馈 | "保存草稿失败" |

## 二、运维中

### 2.1 处理逻辑线

```text
现象矛盾：df -h 只有 41%，但写入报 Errno 28
   ↓
df -i 确认 inode 使用率 96% → 定位为 inode 耗尽
   ↓
find /data/sessions -type f | wc -l 找到高密度小文件目录
   ↓
止血：按 TTL 清理孤儿会话文件（保留 24h 内活跃会话）
   ↓
验证：df -i 回落、草稿保存成功
   ↓
根治：游客会话 TTL + 定时清理 + inode 告警
```

### 2.2 逐步拆解

**Step 1 · 识别"空间正常但写不进去"（1 分钟）**

```bash
df -h /data     # 41%，排除空间不足
df -i /data     # 96%，inode 几乎耗尽 → 这就是根因
```

> 这是本案例最容易被误判的点：只看 `df -h` 会得出"磁盘没问题"的错误结论，
> 然后去查应用代码和权限，浪费大量时间。

**Step 2 · 定位 inode 消耗者（3 分钟）**

```bash
# 统计各目录文件数，找到高密度目录
for d in /data/*; do echo "$(find $d -type f | wc -l) $d"; done | sort -rn | head
find /data/sessions -type f | wc -l
find /data/sessions -type f -mtime +1 | wc -l
ls -l /data/sessions | head
```

判断：`/data/sessions` 占用 1.9 万文件，其中 3 天前的孤儿文件占多数。

**Step 3 · 止血（5 分钟）**

```bash
# 1) 停掉草稿写入的清理任务，避免边删边建
systemctl stop xhs-draft-janitor 2>/dev/null || true

# 2) 按 TTL 清理（保留 24h 内）
find /data/sessions -type f -mtime +1 -delete

# 3) 若仍高于 70%，继续按时间从旧到新清理至 40%
df -i /data
```

**Step 4 · 恢复确认（3 分钟）**

```bash
df -i /data
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:PORT/healthz
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
```

**Step 5 · 根因加固（当天）**

```bash
# systemd timer：每小时清理一次孤儿会话
# /etc/systemd/system/xhs-session-janitor.timer
[Timer]
OnCalendar=hourly
Persistent=true

# 清理脚本核心逻辑
find /data/sessions -type f -mmin +1440 -delete
```

同时给游客会话加 TTL（Redis 侧 2h），从源头减少文件创建。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 按 mtime 清理而非全删 | 保护正在编辑的会话 | 清理不彻底，需二次巡检 |
| 保留 24h 而非 1h | 避免误删用户草稿 | 释放速度稍慢 |
| 不扩容 inode | ext4 扩容 inode 需重建文件系统 | 长期需改为 XFS/独立数据盘 |

## 三、运维后

### 3.1 复盘

- **直接原因**：游客会话文件无 TTL 清理，inode 被耗尽。
- **根本原因**：监控缺 `df -i`；清理任务逻辑遗漏游客分支；容量规划只看空间不看 inode。
- **做得好**：通过 `df -h` 与报错矛盾快速锁定 inode 维度。
- **待改进**：小文件场景应改用对象存储或单文件追加，而非"一会话一文件"。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 每小时孤儿会话清理 | 工程 | inode 稳定 < 40% |
| `df -i` 纳入云监控 | 监控 | 80% 预警 |
| 游客会话 TTL | 架构 | 单日新增文件下降 90% |
| 小文件目录单独分区（XFS） | 架构 | 不与日志争抢 inode |
| 写入失败错误码区分 ENOSPC/EIO | 可观测 | 告警能直接指向 inode |

### 3.3 自动化沉淀

脚本：[automation/inode_guard.py](../../automation/inode_guard.py)

```bash
python automation/inode_guard.py --case case-07-inode --apply
```

逻辑：`df -i` + `find` 统计 → TTL 清理 → 仍高于 70% 时按 mtime 继续清理至 40%
→ 校验 inode 与业务可用性 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-07-inode/lab.ipynb](../../labs/case-07-inode/lab.ipynb)

1. `lab.inject_inode_exhaustion()` 真实创建约 1.9 万个空文件；
2. `df -h` 仍显示有空间，`df -i` 打到 96%，复现"矛盾现象"；
3. `inode_guard.py --apply` 完成清理；
4. 断言：inode 使用率 < 70%，健康检查 200。

## 五、思考题

1. 为什么 `rm` 大文件后 `df` 没有立即变化，而删除大量小文件变化明显？
2. XFS 与 ext4 在 inode 分配上的差异，会如何影响这类故障？
3. 如果清理误删了用户草稿，如何设计才能既安全又及时？
