# 案例 06 · 日志写满云盘，笔记服务写入失败

> 分类：[C 存储与文件系统](../01-knowledge-base/03-storage.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

`xhs-note-api-01` 的 `/data` 是一块 128MB 的 ESSD 云盘（生产为 500GB，沙箱按比例缩小），
存放应用日志、图片处理临时文件与本地缓存。日志按天切割，但**没有保留策略**，
历史 `app-*.log` 从未清理。笔记详情页在写入"浏览埋点"时同步写本地日志。

### 1.2 触发诱因

- 大促期间流量翻倍，单日日志量从 4GB 涨到 11GB；
- 日志切割后只归档不删除，保留 30 天以上；
- 另有 1 个 `app.log.1` 被应用进程以打开句柄持有，`rm` 后空间不释放，形成"看不见的 19MB"；
- 云盘没有配置用量告警（或告警阈值仅 95%，触发即已不可写）。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 14:22:10 | 云监控 CMS | `diskusage_utilization` /data = 96.2% |
| 14:23:41 | 应用日志 | `OSError: [Errno 28] No space left on device, path=/data/logs/app.log` |
| 14:24:05 | SLB | 后端健康检查连续失败 2 次，摘除 `i-bp1xhsapi01` |
| 14:25:30 | 用户反馈 | 笔记详情页"发布失败/加载失败" |

## 二、运维中

### 2.1 处理逻辑线

```text
确认影响面（单实例、SLB 已摘除 → 容量减半）
   ↓
df -h 确认分区打满；du -sh 定位大目录
   ↓
lsof +L1 发现 deleted 但仍被持有的文件（rm 不释放）
   ↓
止血：清理历史日志 + 重启持有句柄的进程释放空间
   ↓
验证：df 回落、写文件成功、SLB 健康恢复
   ↓
根治：日志轮转 + 保留策略 + 90% 告警 + 日志外送 SLS
```

### 2.2 逐步拆解

**Step 1 · 确认是"空间满"还是"配额/inode"（1 分钟）**

```bash
df -h /data          # 96% used，确认是空间而非 inode
df -i /data          # inode 正常，排除小文件耗尽
```

**Step 2 · 定位占用（3 分钟）**

```bash
du -sh /data/*            # logs 占 92%
du -sh /data/logs/* | sort -rh | head
find /data -type f -size +10M -exec ls -lh {} \;
lsof +L1                  # 关键：deleted 文件仍被 note-api 持有 19MB
```

判断：`du` 统计的总量 + `lsof +L1` 的 deleted 文件 > `df` 显示量，
说明**有已删除但未释放的句柄**，单纯 `rm` 不能解决。

**Step 3 · 止血（3 分钟）**

```bash
# 1) 按保留策略清理历史日志（保留最近 2 个 + 压缩）
find /data/logs -name 'app-*.log' -mtime +1 -delete
gzip /data/logs/app-00*.log

# 2) 释放 deleted-open 句柄：滚动重启应用（生产可先摘流）
systemctl restart xhs-note-api

df -h /data
```

> 为什么先清日志再重启：清理能立即释放大部分空间；重启只为释放 inode 句柄，
> 且会短暂中断服务，必须配合 SLB 摘流。

**Step 4 · 恢复确认（3 分钟）**

```bash
df -h /data                                        # < 70%
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:PORT/healthz
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
```

**Step 5 · 根因加固（当天）**

```bash
# /etc/logrotate.d/xhs-note-api
/data/logs/app.log {
    daily
    rotate 3
    maxsize 200M
    compress
    delaycompress
    copytruncate
}
```

同时：日志实时投递 SLS，本地只保留 3 天；云盘 85% 预警、92% 严重告警；
大数据量目录迁移到独立数据盘。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 先删旧日志而非扩容 | 扩容需要停机挂盘，且治标不治本 | 历史日志丢失（已有 SLS 备份则无影响） |
| 重启释放 deleted 句柄 | 不重启无法回收空间 | 服务中断数秒，需摘流 |
| `copytruncate` 轮转 | 避免应用未重开文件导致日志丢失 | 切割瞬间可能丢少量日志 |

## 三、运维后

### 3.1 复盘

- **直接原因**：日志无保留策略 + 流量翻倍，写满云盘。
- **根本原因**：缺少容量水位告警与日志生命周期管理；`rm` 后未检查 deleted-open 句柄。
- **做得好**：先 `df/du/lsof` 三层取证，避免"删了文件但空间没回来"的二次故障。
- **待改进**：日志应默认外送，本地盘不承担长期存储。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| logrotate + 保留 3 天 + 压缩 | 工程 | 日志目录稳定 < 20% |
| 日志实时投递 SLS | 架构 | 本地盘不再承担归档 |
| 85% / 92% 两级告警 | 监控 | 提前 30 分钟预警 |
| 每周 `lsof +L1` 巡检 | 流程 | deleted-open 文件为 0 |
| 临时文件统一落 `/data/tmp` 并 TTL 清理 | 工程 | 临时目录可被安全清空 |

### 3.3 自动化沉淀

脚本：[automation/disk_guard.py](../../automation/disk_guard.py)

```bash
python automation/disk_guard.py --case case-06-disk-full --apply
```

逻辑：`df/du/lsof +L1` 取证 → 按保留策略清理并压缩 → 释放 deleted-open 句柄
→ 校验水位回落 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-06-disk-full/lab.ipynb](../../labs/case-06-disk-full/lab.ipynb)

1. `lab.inject_disk_full()` 写入真实日志文件把 `/data` 打到 96%，并登记一个 deleted-open 文件；
2. 用 `lab.sh("df -h /data")`、`lab.sh("du -sh /data/*")`、`lab.sh("lsof +L1")` 复现取证过程；
3. `disk_guard.py --apply` 完成清理；
4. 断言：使用率 < 70%，`/healthz` 200。

## 五、思考题

1. `df` 显示 100% 但 `du` 只有 60%，可能有哪些原因？如何逐个排除？
2. 生产环境重启释放句柄前，如何把影响面压到最小？
3. 如果日志必须保留 180 天，本地盘方案该如何调整？
