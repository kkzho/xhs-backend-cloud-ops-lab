# 案例 09 · 大 key 撑爆 Redis，榜单缓存写入被拒

> 分类：[D 数据库与缓存](../01-knowledge-base/04-data.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

首页 Feed 榜单全部放在云 Redis（`r-bp1xhsfeed01`，32MB 规格，沙箱按比例缩小）中：
`feed:rank:global:all` 全站榜单、`feed:rank:cat:*` 分类榜单、`note:detail:*` 详情缓存。
淘汰策略为 `noeviction`（历史上为了避免缓存击穿后污染 DB 而设置）。

### 1.2 触发诱因

- 运营上线"全站热榜"，把 500 万条笔记的排序结果整体写成 1 个 6MB 的 String key；
- 分类榜单从 6 个扩展到 10 个，每个 2MB；
- 详情缓存无 TTL，长期堆积；
- `maxmemory=32MB` 且 `noeviction`：内存一到上限，**所有写命令直接报 OOM**。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 20:11:07 | 云监控 CMS | Redis `UsedMemory` ≈ `MaxMemory`（32MB） |
| 20:11:09 | 应用日志 | `redis SET failed: OOM command not allowed when used memory > 'maxmemory'` |
| 20:11:40 | 业务监控 | 首页榜单接口降级返回空列表，缓存命中率 98% → 71% |
| 20:12:20 | 用户反馈 | 首页"热门"空白 |

## 二、运维中

### 2.1 处理逻辑线

```text
确认是"内存满导致拒绝写入"，不是网络/连接问题
   ↓
INFO memory 看 used_memory / maxmemory / policy / evicted
   ↓
--bigkeys 找到 6MB 全站榜单 key
   ↓
止血：切 allkeys-lru 恢复写入 → 拆分大 key → 设置 TTL
   ↓
验证：可正常 SET，内存回落，命中率恢复
   ↓
根治：大 key 治理规范 + 内存/淘汰告警 + 容量评估
```

### 2.2 逐步拆解

**Step 1 · 确认内存与策略（2 分钟）**

```bash
redis-cli INFO memory
# used_memory_human: 31.8M
# maxmemory_human: 32.0M
# maxmemory_policy: noeviction
# evicted_keys: 0          ← 没有淘汰，说明是被"写满后拒绝"

redis-cli INFO stats | grep -E "evicted|rejected"
```

判断：`noeviction` + `evicted_keys=0` + 写报 OOM → **策略选择错误**是放大器。

**Step 2 · 找到大 key（3 分钟）**

```bash
redis-cli --bigkeys
# Biggest string found 'feed:rank:global:all' has 6291456 bytes
# Biggest string found 'note:detail:8f2a41c9' has 4194304 bytes

redis-cli MEMORY USAGE feed:rank:global:all
redis-cli TTL note:detail:8f2a41c9        # -1，永不过期
```

**Step 3 · 止血（5 分钟）**

```bash
# 1) 先改为 LRU，让写入不再被拒（牺牲少量冷数据）
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# 2) 删除/拆分最大的 key
redis-cli DEL feed:rank:global:all

# 3) 详情缓存补 TTL
redis-cli EXPIRE note:detail:8f2a41c9 3600

redis-cli INFO memory | grep used_memory_human
redis-cli SET feed:rank:smoke 1
```

> 为什么先切策略再删 key：切策略是秒级、可回滚的操作，能立即恢复写入能力；
> 删 key 需要确认业务可重建（榜单可由 DB 重算），顺序不能反。

**Step 4 · 恢复确认（3 分钟）**

```bash
redis-cli INFO stats | grep keyspace_hits
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:PORT/api/v1/feed
```

**Step 5 · 根因加固（当天）**

- 全站榜单拆分为 `feed:rank:global:{0..9}` 分片，单 key < 1MB；
- 分类榜单改为 Hash 或 ZSet，只存 topN；
- 所有缓存 key 强制 TTL，禁止 `-1`；
- 内存 70% 预警、85% 严重告警；`bigkeys` 每周巡检。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 切 `allkeys-lru` 而非 `volatile-lru` | 大量 key 无 TTL，volatile 无法释放 | 可能淘汰热点数据，需快速重建 |
| 直接删大 key | 榜单可重算，恢复最快 | 重建期间榜单为空/降级 |
| 不立即扩容 | 单 key 6MB 属设计问题，扩容只是延后 | 需当天完成拆分改造 |

## 三、运维后

### 3.1 复盘

- **直接原因**：单 key 过大 + 无 TTL + `noeviction` 策略，写满即拒绝写入。
- **根本原因**：缺大 key 规范与容量评估；淘汰策略与业务形态不匹配。
- **做得好**：`INFO memory` 一次看清 used/max/policy/evicted 四要素，快速定位策略问题。
- **待改进**：缓存命中率骤降应直接触发告警，而不是等用户反馈。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 单 key < 1MB、集合元素 < 5000 | 规范 | 每周 `--bigkeys` 无告警 |
| 所有缓存强制 TTL | 工程 | `TTL=-1` 的 key 为 0 |
| 内存 70%/85% 告警 + 命中率告警 | 监控 | 10 分钟内发现 |
| 榜单分片 + topN | 架构 | 单分片 < 1MB |
| 淘汰策略按业务评审 | 流程 | 生产默认 `volatile-lru` 或 `allkeys-lru` |

### 3.3 自动化沉淀

脚本：[automation/redis_mem_guard.py](../../automation/redis_mem_guard.py)

```bash
python automation/redis_mem_guard.py --case case-09-redis --apply
```

逻辑：`INFO memory` + `--bigkeys` 取证 → 切 `allkeys-lru` → 删除超大 key
→ 校验可写入且内存回落 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-09-redis/lab.ipynb](../../labs/case-09-redis/lab.ipynb)

1. `lab.start_redis()` 启动真实持有内存的 Redis-lite（`maxmemory=32MB`）；
2. `lab.inject_redis_memory()` 写入 6MB + 4MB + 10×2MB，触发 `noeviction` 拒绝写入；
3. `lab.sh("redis-cli INFO memory")`、`lab.sh("redis-cli --bigkeys")` 复现取证；
4. `redis_mem_guard.py --apply` 完成处置；
5. 断言：`SET` 返回 OK，`used_memory < maxmemory`。

## 五、思考题

1. `noeviction`、`volatile-lru`、`allkeys-lru` 分别适合什么业务？
2. 大 key 除了内存问题，还会带来哪些运行时风险（提示：阻塞、网络、删除）？
3. 榜单缓存被淘汰后，如何避免请求瞬间穿透到 DB？
