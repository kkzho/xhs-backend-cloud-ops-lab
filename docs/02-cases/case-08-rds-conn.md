# 案例 08 · 连接池泄漏打满 RDS，笔记接口报 1040

> 分类：[D 数据库与缓存](../01-knowledge-base/04-data.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

笔记详情与 Feed 服务通过 Python 应用直连 RDS MySQL（`rm-bp1xhsfeed01`，`max_connections=50`）。
连接池配置 `pool_size=20, max_overflow=10`，但**没有开启 `pool_pre_ping` 与 `pool_recycle`**。
一次版本发布把"笔记详情"查询拆成了两段事务，异常分支忘记 `close()`。

### 1.2 触发诱因

- 新版本在详情页异常分支（笔记已删除 / 权限不足）没有归还连接；
- RDS 侧 `wait_timeout=28800`，坏连接不会被服务端主动断开；
- 连接数缓慢上涨，2 小时后触及 `max_connections=50`；
- 应用未配置连接获取超时，请求直接抛 `1040`。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 16:02:33 | RDS 监控 | `ConnectionUsage` 100%，`Threads_connected=50` |
| 16:02:35 | 应用日志 | `pymysql.err.OperationalError: (1040, 'Too many connections')` |
| 16:03:10 | SLB | 后端健康检查失败（健康检查也依赖 DB） |
| 16:04:00 | 用户反馈 | 笔记详情页大面积打不开 |

## 二、运维中

### 2.1 处理逻辑线

```text
确认是"连接数打满"而非"DB 挂了"（能连上但被拒绝）
   ↓
SHOW PROCESSLIST 统计：大量 Sleep + 同一来源 IP
   ↓
判定为应用连接池泄漏（不是慢查询拖住连接）
   ↓
止血：KILL 长时间 Sleep 会话，恢复可用连接
   ↓
验证：Threads_connected 回落、接口 200
   ↓
根治：修异常分支 + 池参数（pre_ping/recycle/超时）+ 连接数告警
```

### 2.2 逐步拆解

**Step 1 · 区分"连不上"与"连太多"（2 分钟）**

```bash
mysql -h rm-bp1xhsfeed01 -e "SHOW STATUS LIKE 'Threads_connected';"
mysql -h rm-bp1xhsfeed01 -e "SHOW VARIABLES LIKE 'max_connections';"
```

报错是 `1040 Too many connections`，说明网络与实例都正常，**是并发连接超限**。
若报 `2003 Can't connect`，则要查网络/安全组/实例状态。

**Step 2 · 定位连接来源与状态（3 分钟）**

```bash
mysql -e "SHOW PROCESSLIST;"
# 50 条记录中 44 条 Command=Sleep，Time 从 300 到 6800 秒
# 来源集中在 10.10.1.11（note-api-01）

mysql -e "SELECT command, COUNT(*) FROM information_schema.processlist GROUP BY command;"
aliyun rds DescribeDBInstancePerformance \
  --DBInstanceId rm-bp1xhsfeed01 --Key ThreadsConnected
```

判断：大量长 `Sleep` + 单一来源 = **连接泄漏**；若是慢查询，会看到 `Query` 状态且
`Time` 很大、CPU 高。

**Step 3 · 止血（2 分钟）**

```bash
# 只杀 Sleep 超过 300 秒的会话，保留活跃业务连接
mysql -e "SELECT id FROM information_schema.processlist WHERE command='Sleep' AND time>300;" \
  | tail -n +2 | xargs -I{} mysql -e "KILL {};"

mysql -e "SHOW STATUS LIKE 'Threads_connected';"
```

> 为什么不用 `KILL` 全部：会打断正在执行的订单/发布类事务，造成数据不一致。

**Step 4 · 恢复确认（3 分钟）**

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" http://127.0.0.1:PORT/api/v1/note/8f2a41c9
mysql -e "SHOW STATUS LIKE 'Threads_connected';"     # 回落到 10 左右
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
```

**Step 5 · 根因加固（当天）**

```python
# 连接池参数
engine = create_engine(
    url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,      # 取连接前探活，剔除失效连接
    pool_recycle=1800,       # 30 分钟回收，早于 RDS wait_timeout
    pool_timeout=3,          # 获取连接最多等 3s，快速失败
    connect_args={"connect_timeout": 2},
)
```

异常分支统一用 `with session_scope() as s:`，保证 `close/rollback`。
RDS 侧开启 `performance_schema` 与连接数 80% 告警。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| KILL Sleep 会话而非重启应用 | 秒级恢复，不影响其他实例 | 泄漏代码仍在，会再次上涨 |
| 保留活跃连接 | 避免打断事务 | 恢复后连接数略高 |
| 临时调大 `max_connections` | 争取排查时间 | 内存压力增大，且掩盖根因，仅作兜底 |

## 三、运维后

### 3.1 复盘

- **直接原因**：异常分支未归还连接，连接池被逐步占满。
- **根本原因**：池参数缺 `pre_ping/recycle/timeout`；无连接数告警；异常路径缺单测。
- **做得好**：用 `Sleep + 来源集中` 快速区分泄漏与慢查询。
- **待改进**：健康检查不应强依赖 DB，否则 DB 抖动会放大为全站摘除。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 池参数 + 上下文管理器 | 工程 | 压测 2 小时连接数平稳 |
| 连接数 80% 告警 | 监控 | 提前 30 分钟预警 |
| 异常分支单测覆盖 | 质量 | 详情页 4xx 分支连接归还断言 |
| 健康检查拆分为 liveness/readiness | 架构 | DB 抖动不触发全量摘除 |
| 慢查询与连接来源看板 | 可观测 | 5 分钟内定位到实例与接口 |

### 3.3 自动化沉淀

脚本：[automation/rds_session_guard.py](../../automation/rds_session_guard.py)

```bash
python automation/rds_session_guard.py --case case-08-rds-conn --apply
```

逻辑：`SHOW PROCESSLIST` 统计 Sleep 与来源 → KILL 长空闲会话 → 修正池参数
→ 校验 `Threads_connected` 回落 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-08-rds-conn/lab.ipynb](../../labs/case-08-rds-conn/lab.ipynb)

1. `lab.start_mysql()` 启动一个真实 TCP 的 MySQL-lite（`max_connections=50`）；
2. `lab.inject_rds_pool_leak(connections=60)` 建立 60 条真实连接，第 51 条起返回 1040；
3. `lab.sh("mysql -e 'SHOW PROCESSLIST'")` 观察 Sleep 会话；
4. `rds_session_guard.py --apply` 完成处置；
5. 断言：连接数 < 50，业务请求 200。

## 五、思考题

1. `Sleep` 多和 `Query` 多分别指向什么类型的问题？
2. `pool_recycle` 为什么必须小于 RDS 的 `wait_timeout`？
3. 如果泄漏来自第三方 SDK 且无法改代码，你会怎么兜底？
