# D · 数据库与缓存

覆盖 RDS MySQL 与云 Redis。核心原则：
**数据库问题先看连接与慢查询，缓存问题先看内存与命中率；两者都可能表现为"应用超时"。**

---

## D1 · 连接池泄漏打满 RDS，接口报 1040

**业务现象**
接口大面积 500，日志出现 `pymysql.err.OperationalError: (1040, 'Too many connections')`；
RDS 控制台连接数 100%；CPU 与 QPS 不高，但线程池/连接池被占满。

**影响与定级**
P1 → 若所有实例共享同一实例则 P0。

**常见根因**
1. 代码路径异常时未 `close()`/未归还连接（连接池泄漏）；
2. 连接池 `max_open` 配置过大，多实例叠加超过 RDS 上限；
3. 慢查询导致连接长时间占用（`Sleep` 少、`Query` 多）；
4. 连接未设置 `idle_timeout` / 探活，失效连接堆积；
5. 突发流量导致连接数瞬时翻倍。

**首查命令**

```bash
# RDS 侧（也可用 DMS 控制台）
mysql -h rm-xxx.mysql.rds.aliyuncs.com -u xhs_app -p -e "SHOW PROCESSLIST"
mysql -h ... -e "SHOW STATUS LIKE 'Threads_connected'"
mysql -h ... -e "SHOW VARIABLES LIKE 'max_connections'"
```

云侧取证：

```bash
aliyun rds DescribeDBInstancePerformance --DBInstanceId rm-xxx --Key ConnectionUsage
aliyun rds DescribeSlowLogs --DBInstanceId rm-xxx --StartTime 2026-11-20T00:00Z --EndTime 2026-11-20T23:59Z
```

**判断依据**
- 大量 `Sleep` 且 `Time` 很大 → 连接泄漏/未归还；
- 大量 `Query` 且耗时长 → 慢查询占满连接；
- 多实例 `max_open` 之和 > `max_connections` → 配置问题；
- 连接数在发布后突增 → 新代码泄漏。

**处置要点**
1. 止血：`KILL` 长时间空闲会话（保留业务活跃连接），必要时临时提高 `max_connections`；
2. 修复连接池：`max_open` 收敛、`idle_timeout`、`health_check`、异常路径 `finally` 归还；
3. 慢查询：加索引/改写/限流，避免在连接内做重计算；
4. 长期：引入数据库代理（如 RDS 数据库代理）做连接复用。

**预防与自动化**
- 连接使用率 > 80% 告警，发布后 30 分钟重点观察；
- 连接池指标（active/idle/wait）纳入应用监控；
- 自动化：`automation/rds_session_guard.py`（会话分析 → KILL 空闲 → 修池参数 → 校验）。

**关联案例**：[case-08](../02-cases/case-08-rds-conn.md) · [Notebook](../../labs/case-08-rds-conn/lab.ipynb)

---

## D2 · 大 key / 热 key 打满 Redis，写入被拒

**业务现象**
Feed 缓存写入失败，日志出现 `OOM command not allowed when used memory > 'maxmemory'`；
接口 RT 升高、缓存命中率下降；Redis 监控 `used_memory` 接近上限。

**影响与定级**
P1。缓存击穿会进一步压垮 DB，可能演变为 P0。

**常见根因**
1. 大 key（排行榜全量、详情大 JSON）占用数 MB；
2. 热 key（明星/爆款笔记）流量集中，单分片压力过高；
3. `maxmemory-policy=noeviction`，写满后直接拒绝写入；
4. 未设置 TTL，冷数据永久驻留；
5. 容量规划不足或未及时扩容。

**首查命令**

```bash
redis-cli INFO memory
redis-cli INFO stats
redis-cli --bigkeys
redis-cli MEMORY USAGE feed:rank:global:all
redis-cli CONFIG GET maxmemory-policy
redis-cli SLOWLOG GET 10
```

云侧取证：

```bash
aliyun kvstore DescribeHistoryMonitorValues --InstanceId r-xxx --Key MemoryUsage
aliyun kvstore DescribeInstanceAttribute --InstanceId r-xxx
```

**判断依据**
- `used_memory` ≈ `maxmemory` 且 `evicted_keys=0` → noeviction 拒绝写入；
- `--bigkeys` 出现 > 1MB 的 key → 大 key；
- 命中率下降 + 单分片 CPU 高 → 热 key；
- 写入失败集中在某类 key → 业务设计问题。

**处置要点**
1. 止血：临时切 `allkeys-lru` 恢复写入（注意会淘汰冷数据）；
2. 拆分大 key：Hash 分片、只存必要字段、排行榜按维度拆分；
3. 热 key：本地缓存 + 随机过期 + 读写分离/多副本；
4. 补 TTL，冷数据落 DB/OSS；
5. 评估扩容（集群版分片）而不是无限调大内存。

**预防与自动化**
- 大 key/热 key 巡检（每日 `--bigkeys` + 采样热点）；
- 内存 75%/85% 两级告警，命中率下降告警；
- 自动化：`automation/redis_mem_guard.py`（内存分析 → 切策略 → 清理大 key → 校验写入）。

**关联案例**：[case-09](../02-cases/case-09-redis.md) · [Notebook](../../labs/case-09-redis/lab.ipynb)
