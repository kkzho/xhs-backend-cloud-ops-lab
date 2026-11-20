# 案例 10 · FD 泄漏耗尽句柄，接口 500 (Too many open files)

> 分类：[E 应用运行时](../01-knowledge-base/05-application.md) ｜ 等级：P1 ｜ 角色：一线值班（On-call）

## 一、运维前

### 1.1 业务背景

`xhs-note-api` 由 systemd 管理，`LimitNOFILE=256`（历史遗留，未随连接数增长调整）。
一次版本在"笔记详情"里新增了缩略图签发逻辑：每次请求 `open()` 一个本地模板文件，
异常分支没有 `close()`。服务单实例每秒约 40 个详情请求。

### 1.2 触发诱因

- 代码 `try/finally` 缺失，异常时文件句柄不归还；
- `LimitNOFILE=256` 远低于实际并发所需（生产建议 ≥ 65535）；
- 进程未配置句柄使用率监控；
- 泄漏累积约 6 分钟后触顶，新请求 `accept`/`open` 全部失败。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 11:36:12 | 应用日志 | `OSError: [Errno 24] Too many open files` |
| 11:36:13 | 应用日志 | `HTTP 500 TOO_MANY_OPEN_FILES /api/v1/note/8f2a41c9` |
| 11:36:40 | SLB | 健康检查失败，摘除后端 |
| 11:37:10 | 用户反馈 | 笔记详情页白屏 |

## 二、运维中

### 2.1 处理逻辑线

```text
确认 500 的具体错误码（Errno 24，不是 DB/超时）
   ↓
lsof -p <pid> | wc -l 对比 LimitNOFILE → 确认句柄耗尽
   ↓
看句柄类型：大量 REG（模板文件）→ 指向文件未关闭
   ↓
止血：滚动重启释放句柄 + 临时提升 LimitNOFILE
   ↓
验证：接口 200，FD 回落到基线
   ↓
根治：修 finally/with + 系统级上限 + FD 使用率告警
```

### 2.2 逐步拆解

**Step 1 · 从错误码定性（1 分钟）**

```bash
tail -n 50 /var/log/xhs/note-api.log
# OSError: [Errno 24] Too many open files
# HTTP 500 TOO_MANY_OPEN_FILES
```

`Errno 24` 直接指向句柄，不需要再查 DB 与网络。

**Step 2 · 量化句柄使用（2 分钟）**

```bash
ps -eo pid,cmd | grep note-api
lsof -p <pid> | wc -l
lsof -p <pid> | awk '{print $5}' | sort | uniq -c | sort -rn | head
cat /proc/<pid>/limits        # 生产：Max open files 256
```

判断：句柄数 256 且以 `REG`（普通文件）为主 → **文件未关闭**；
若以 `IPv4/sock` 为主，则应查下游连接池/HTTP client。

**Step 3 · 止血（3 分钟）**

```bash
# 先摘流（SLB 已在摘除中），再滚动重启释放句柄
systemctl restart xhs-note-api
sleep 2
lsof -p $(pgrep -f 'sims.service' | head -1) | wc -l
```

同时临时提升上限，防止短时间再次触顶：

```bash
# /etc/systemd/system/xhs-note-api.service.d/limits.conf
[Service]
LimitNOFILE=65535
```

`systemctl daemon-reload && systemctl restart xhs-note-api`。

**Step 4 · 恢复确认（3 分钟）**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:PORT/api/v1/note/8f2a41c9
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:PORT/healthz
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
```

**Step 5 · 根因加固（当天）**

```python
# 错误写法
f = open(tpl)
data = render(f)      # 抛异常则 f 永不关闭
return data

# 正确写法
with open(tpl) as f:
    return render(f)
```

- 代码扫描：禁止裸 `open(`，统一 `with`；
- FD 使用率 > 80% 告警；
- 压测加入"异常分支 1000 次"用例，验证句柄不增长。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 重启而非热修 | 句柄无法在线回收 | 服务中断数秒 |
| 提升 LimitNOFILE | 留出安全水位，防止二次故障 | 掩盖泄漏，必须同步修代码 |
| 摘流后重启 | 避免 502 打到用户 | 容量短暂减半 |

## 三、运维后

### 3.1 复盘

- **直接原因**：异常分支未关闭文件句柄。
- **根本原因**：`LimitNOFILE` 过低；无 FD 监控；代码规范缺失。
- **做得好**：用 `lsof` 区分文件句柄与 socket 句柄，方向明确。
- **待改进**：健康检查失败前就应有 FD 水位告警，提前 5 分钟介入。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| `with`/`finally` 统一资源管理 | 工程 | 压测 FD 曲线平稳 |
| `LimitNOFILE=65535` | 配置 | 上限 ≥ 峰值 4 倍 |
| FD 80% 告警 | 监控 | 5 分钟内定位进程 |
| 静态扫描裸 `open(` | 质量 | CI 阻断 |
| 定期 `lsof` 快照留档 | 流程 | 可回溯增长趋势 |

### 3.3 自动化沉淀

脚本：[automation/fd_guard.py](../../automation/fd_guard.py)

```bash
python automation/fd_guard.py --case case-10-fd --apply
```

逻辑：读取进程 FD 与 `LimitNOFILE` → 超 80% 时重启止血并保留现场
→ 提升上限 → 校验接口 200 与 FD 回落 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-10-fd/lab.ipynb](../../labs/case-10-fd/lab.ipynb)

1. `lab.start_api(nofile=256)` 用 `RLIMIT_NOFILE=256` 启动真实服务；
2. `lab.inject_fd_leak(requests=320)` 让每次请求泄漏一个句柄，直到返回 500；
3. `lab.sh("lsof -p <pid>")` 观察句柄增长；
4. `fd_guard.py --apply` 完成处置；
5. 断言：接口恢复 200，FD 数回落。

## 五、思考题

1. `lsof` 输出里 `REG`、`IPv4`、`PIPE` 各代表什么？分别指向哪类泄漏？
2. 为什么提升 `LimitNOFILE` 不能替代修代码？
3. 容器环境下（`ulimit` 继承自 dockerd）该如何正确设置上限？
