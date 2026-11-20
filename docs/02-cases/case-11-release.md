# 案例 11 · 发布配置端口写错，SLB 全量 502

> 分类：[E 应用运行时](../01-knowledge-base/05-application.md) ｜ 等级：P0 ｜ 角色：一线值班（On-call）+ 发布负责人

## 一、运维前

### 1.1 业务背景

`xhs-note-api` 采用"配置中心下发 + 滚动重启"发布：SLB 监听 443 → 后端端口由
`/etc/xhs/note-api.conf` 的 `listen_port` 决定，健康检查路径 `/healthz`。
发布流水线包含"启动服务"但没有"端口连通性门禁"，SLB 健康检查是唯一的兜底。

### 1.2 触发诱因

- 本次发布模板里 `listen_port` 被硬编码成"当前端口 +10"（灰度环境遗留值）；
- 发布脚本只校验"进程存活"，进程确实起来了，但监听在错误端口；
- SLB 后端仍指向旧端口，健康检查全部失败 → 摘除全部后端；
- 流水线没有"发布后冒烟失败自动回滚"。

### 1.3 告警信息

| 时间 | 来源 | 内容 |
|------|------|------|
| 22:14:03 | 发布平台 | 版本 `v2026.11.20-1` 发布完成，状态 failed |
| 22:14:20 | SLB | 所有后端 `HealthStatus=abnormal`，返回 `SLB_502` |
| 22:14:25 | 云监控 CMS | SLB `UnhealthyBackendCount=1`，7 层 QPS 跌至 0 |
| 22:15:00 | 用户反馈 | App 全站"网络异常" |

## 二、运维中

### 2.1 处理逻辑线

```text
全量 502 + 刚刚发布 → 第一时间按"发布故障"处理，而不是查网络
   ↓
确认后端进程存活，但 ss 看不到期望端口监听
   ↓
对比配置：listen_port 与 SLB 后端端口不一致
   ↓
止血：立即回滚到上一版本配置并重启
   ↓
验证：端口监听正确、健康检查 normal、冒烟 200
   ↓
根治：发布门禁（端口+健康+冒烟）失败自动回滚
```

### 2.2 逐步拆解

**Step 1 · 用"时间相关性"锁定发布（1 分钟）**

```bash
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01
# 所有后端 abnormal
aliyun cms DescribeAlarmHistory --StartTime "2026-11-20 22:10:00"
```

结论：故障起点与发布时间重合，且是**全量**失败 → 优先回滚，不深挖。

**Step 2 · 验证"进程活着但端口不对"（2 分钟）**

```bash
systemctl status xhs-note-api
ss -lntp
# 期望 127.0.0.1:21220，实际只有 127.0.0.1:21230

cat /etc/xhs/note-api.conf
# listen_port = 21230     ← 与 SLB 后端端口不一致

curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:21220/healthz   # 000 连接拒绝
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:21230/healthz   # 200，服务本身正常
```

判断：**服务没坏，是流量打到了没人监听的端口**。这类 502 的特征是
"直连旧端口拒绝、直连新端口正常"。

**Step 3 · 止血：回滚（2 分钟）**

```bash
# 回滚配置到上一版本（发布系统里的 last-known-good）
cp /etc/xhs/note-api.conf.bak /etc/xhs/note-api.conf
grep listen_port /etc/xhs/note-api.conf       # 21220
systemctl restart xhs-note-api
```

若无配置备份，则显式改回正确端口并重启，同时把发布标记为失败。

**Step 4 · 恢复确认（3 分钟）**

```bash
ss -lntp | grep 21220
curl -s -o /dev/null -w "%{http_code} %{time_total}\n" http://127.0.0.1:21220/healthz
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:21220/api/v1/feed
aliyun slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01   # normal
```

**Step 5 · 根因加固（当天）**

发布流水线增加三道门禁，任一失败自动回滚：

1. **端口门禁**：`ss -lntp` 必须包含配置中的 `listen_port`；
2. **健康门禁**：`/healthz` 连续 3 次 200；
3. **冒烟门禁**：`/api/v1/feed`、`/api/v1/note/<id>` 返回 200 且 P99 < 300ms。

同时配置模板变量化，禁止硬编码端口；灰度发布按 10% → 50% → 100% 放量。

### 2.3 关键取舍

| 选择 | 原因 | 副作用 |
|------|------|--------|
| 先回滚再定位 | 全量故障，每分钟都在损失 | 丢失本次发布功能 |
| 回滚配置而非重新发布 | 重新发布需 5+ 分钟，回滚 30 秒 | 需保证配置有备份 |
| 不改 SLB 后端端口"救急" | 会把错误配置固化，掩盖问题 | 需多等一次重启 |

## 三、运维后

### 3.1 复盘

- **直接原因**：配置模板硬编码错误端口，服务监听与 SLB 后端不一致。
- **根本原因**：发布流水线只校验进程存活，没有端口/健康/冒烟门禁；无自动回滚。
- **做得好**：用"发布时间相关性 + 全量失败"快速决策回滚，避免长时间排查。
- **待改进**：SLB 健康检查是最后一道防线，不能当第一道。

### 3.2 预防措施

| 措施 | 类型 | 验收标准 |
|------|------|---------|
| 发布门禁三件套 | 工程 | 任一失败自动回滚 |
| 配置模板变量化 | 工程 | 无硬编码端口/IP |
| 灰度发布 10%→50%→100% | 流程 | 单批次可观测 5 分钟 |
| 发布事件与告警联动 | 监控 | 发布期间告警自动标注 |
| 配置变更审计 | 流程 | 可追溯到人与版本 |

### 3.3 自动化沉淀

脚本：[automation/deploy_gate.py](../../automation/deploy_gate.py)

```bash
python automation/deploy_gate.py --case case-11-release --apply
```

逻辑：`ss` 端口检查 → `/healthz` → `/api/v1/feed` 冒烟 → 任一失败读取上一版本配置回滚
→ 回滚后再次冒烟 → 输出 `RESULT: REMEDIATED`。

## 四、演练

Notebook：[labs/case-11-release/lab.ipynb](../../labs/case-11-release/lab.ipynb)

1. `lab.inject_bad_release()` 把服务起在错误端口，并写入失败发布记录；
2. `lab.sh("ss -lntp")` 对比配置端口，`lab.curl()` 复现 502/连接拒绝；
3. `deploy_gate.py --apply` 完成门禁检查与回滚；
4. 断言：正确端口监听、健康检查 200、SLB 状态 normal。

## 五、思考题

1. 为什么"进程存活"不能作为发布成功的判据？
2. 回滚和"前滚修复"分别适用于什么场景？
3. 如何设计灰度发布，使这类配置错误只影响 10% 流量？
