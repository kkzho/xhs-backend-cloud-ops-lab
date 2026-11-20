# C · 存储与文件系统

覆盖云盘容量、inode、日志轮转、临时文件。核心原则：
**`df -h` 有空间不代表能写文件，还要看 `df -i` 与"已删除但仍被占用"的句柄。**

---

## C1 · 日志写满云盘，服务报 No space left on device

**业务现象**
服务上传/写日志失败，错误日志出现 `OSError: [Errno 28] No space left on device`；
`df -h` 使用率 100%；重启服务也无效（文件仍在）。

**影响与定级**
P1。写入失败会导致请求失败、数据丢失；若系统盘满，可能引发 sshd/日志服务异常 → P0。

**常见根因**
1. 日志未轮转或保留策略过长；
2. 大文件上传/临时文件未清理；
3. **文件已删除但被进程持有**（`lsof +L1` 可见），空间不释放；
4. 核心转储（core dump）占满；
5. 云盘本身容量规划不足。

**首查命令**

```bash
df -h
df -i
du -sh /data/* | sort -h | tail
find /data -type f -size +500M -exec ls -lh {} \;
lsof +L1 | head                 # 已删除但仍被占用的文件
lsof -p <pid> | wc -l
```

**判断依据**
- `df` 100% 且 `du` 统计远小于已用 → 存在 deleted-open 文件；
- 某个目录独占大部分空间 → 日志/临时文件；
- 清理后 `df` 不降 → 句柄未释放，需重启或 `truncate`。

**处置要点**
1. 先删最安全的大文件（历史日志），**不要删正在写的文件**；
2. 对 deleted-open：优先 `truncate -s 0`，无法定位时滚动重启持有进程；
3. 恢复后立即补轮转策略，避免几小时后再满；
4. 系统盘与数据盘分开，避免日志拖垮系统。

**预防与自动化**
- logrotate 按大小 + 时间双策略，保留 7 天并压缩；
- 磁盘 80%/90% 两级告警，应用侧预留 10% buffer；
- 自动化：`automation/disk_guard.py`（定位 → 保留策略清理 → 释放句柄 → 校验水位）。

**关联案例**：[case-06](../02-cases/case-06-disk-full.md) · [Notebook](../../labs/case-06-disk-full/lab.ipynb)

---

## C2 · 会话临时文件耗尽 inode

**业务现象**
`df -h` 显示还有空间，但创建文件报 `No space left on device`；`df -i` 使用率 100%；
服务上传头像/写会话文件失败，日志报错与磁盘满一致，容易误判。

**影响与定级**
P1。症状与磁盘满高度相似，误判会浪费大量时间。

**常见根因**
1. 会话/上传临时文件未按 TTL 清理；
2. 小文件高密度目录（每用户一个文件、缩略图切片）；
3. 大量空文件（如失败的断点续传占位）；
4. 容器层/日志目录重复创建小文件。

**首查命令**

```bash
df -h && df -i
find /data -type f | wc -l
for d in /data/*; do echo "$(find $d -type f | wc -l) $d"; done | sort -n | tail
find /data -type f -mtime +3 | head
```

**判断依据**
- `df -h` 有余量 + `df -i` 100% → inode 耗尽（确定）；
- 文件数集中在单一目录 + 单文件极小 → TTL 缺失；
- 文件 mtime 集中在某次发布后 → 新逻辑未清理。

**处置要点**
1. 按 TTL 清理（`-mtime +1`），先小批量验证再全量；
2. 清理脚本必须排除正在使用的文件（按 mtime + 业务白名单）；
3. 恢复后补 TTL 清理任务与 inode 告警；
4. 长期方案：对象存储（OSS）承载上传，本地只留索引。

**预防与自动化**
- inode 使用率 > 80% 告警（很多团队只监控容量，这是盲区）；
- 目录级配额/清理任务纳入巡检；
- 自动化：`automation/inode_guard.py`（df -i → 目录密度 → TTL 清理 → 兜底清理 → 校验）。

**关联案例**：[case-07](../02-cases/case-07-inode.md) · [Notebook](../../labs/case-07-inode/lab.ipynb)
