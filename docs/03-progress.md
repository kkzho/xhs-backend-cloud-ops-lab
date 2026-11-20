# 项目进度存档

> 更新时间：2026-11-20 ｜ 状态：全部阶段完成
> 仓库：https://github.com/kkzho/xhs-backend-cloud-ops-lab

本文件记录项目的阶段产出，便于复盘与面试时快速定位材料。

## 阶段总览

| 阶段 | 产出 | 位置 | 状态 |
|------|------|------|------|
| 1 项目规划 | 项目结构、学习路径、Notebook 约定、沙箱边界 | [README](../README.md)、[guidance](00-guidance.md) | 完成 |
| 2 知识库 | 6 大类、12 个高频问题、通用排查顺序 | [knowledge-base](01-knowledge-base/README.md) | 完成 |
| 3 首个案例 | 案例 01：CPU 打满（运维前/中/后） | [case-01](02-cases/case-01-cpu-runaway.md) | 完成 |
| 4 全部案例 | 12 份案例手册，含复盘、预防、自动化 | [cases](02-cases/README.md) | 完成 |
| 5 实操演练 | 12 个 Notebook（重现→取证→判断→处置→验证→自动化） | `labs/case-*/lab.ipynb` | 完成 |
| 6 验证交付 | 批量执行 12/12 通过，README 与索引可读 | `tools/run_all_labs.py` | 完成 |

## 知识库结构

| 分类 | 文档 | 问题 |
|------|------|------|
| A 计算与实例 | [01-compute.md](01-knowledge-base/01-compute.md) | A1 CPU 打满、A2 OOM Kill |
| B 网络与负载均衡 | [02-network.md](01-knowledge-base/02-network.md) | B1 SLB 502、B2 安全组超时、B3 证书过期 |
| C 存储与文件系统 | [03-storage.md](01-knowledge-base/03-storage.md) | C1 磁盘写满、C2 inode 耗尽 |
| D 数据库与缓存 | [04-data.md](01-knowledge-base/04-data.md) | D1 RDS 连接打满、D2 Redis 内存打满 |
| E 应用运行时 | [05-application.md](01-knowledge-base/05-application.md) | E1 FD 泄漏、E2 发布端口错误 |
| F 可观测性与告警 | [06-observability.md](01-knowledge-base/06-observability.md) | F1 采集断流 + 告警风暴 |

## 案例明细

| # | 案例 | 直接原因 | 关键工具 | 自动化 | Notebook |
|---|------|---------|---------|--------|----------|
| 01 | [CPU 打满](02-cases/case-01-cpu-runaway.md) | cron 1 分钟 + 无锁导致任务并发 | `uptime`、`ps`、CMS | `cpu_guard.py` | [lab](../labs/case-01-cpu-runaway/lab.ipynb) |
| 02 | [OOM Kill](02-cases/case-02-memory-oom.md) | 对象泄漏导致 RSS 超过水位 | `dmesg`、`systemctl`、RSS | `memory_guard.py` | [lab](../labs/case-02-memory-oom/lab.ipynb) |
| 03 | [SLB 502](02-cases/case-03-slb-502.md) | 健康检查依赖超时，后端被摘除 | `DescribeHealthStatus`、直连探活 | `slb_recover.py` | [lab](../labs/case-03-slb-502/lab.ipynb) |
| 04 | [安全组超时](02-cases/case-04-sg-timeout.md) | 误删入方向规则，SYN 被丢弃 | 规则比对、策略探测 | `sg_audit.py` | [lab](../labs/case-04-sg-timeout/lab.ipynb) |
| 05 | [证书过期](02-cases/case-05-tls-cert.md) | 证书 notAfter 已过 | `openssl s_client`、TLS 校验 | `cert_renew.py` | [lab](../labs/case-05-tls-cert/lab.ipynb) |
| 06 | [磁盘写满](02-cases/case-06-disk-full.md) | 日志无保留策略 + deleted-open 句柄 | `df`、`du`、`lsof +L1` | `disk_guard.py` | [lab](../labs/case-06-disk-full/lab.ipynb) |
| 07 | [inode 耗尽](02-cases/case-07-inode.md) | 游客会话文件无 TTL | `df -i`、`find` | `inode_guard.py` | [lab](../labs/case-07-inode/lab.ipynb) |
| 08 | [RDS 连接打满](02-cases/case-08-rds-conn.md) | 异常分支未归还连接 | `SHOW PROCESSLIST`、`KILL` | `rds_session_guard.py` | [lab](../labs/case-08-rds-conn/lab.ipynb) |
| 09 | [Redis 内存打满](02-cases/case-09-redis.md) | 大 key + 无 TTL + noeviction | `INFO memory`、`--bigkeys` | `redis_mem_guard.py` | [lab](../labs/case-09-redis/lab.ipynb) |
| 10 | [FD 泄漏](02-cases/case-10-fd.md) | 异常分支未关闭文件 | `lsof`、`ulimit` | `fd_guard.py` | [lab](../labs/case-10-fd/lab.ipynb) |
| 11 | [发布 502](02-cases/case-11-release.md) | 配置端口与 SLB 后端不一致 | `ss`、`curl`、发布记录 | `deploy_gate.py` | [lab](../labs/case-11-release/lab.ipynb) |
| 12 | [告警风暴](02-cases/case-12-alert.md) | 规则阈值失配 + Logtail 停止 | 告警历史、SLS 采集状态 | `alert_dedup.py` | [lab](../labs/case-12-alert/lab.ipynb) |

## Notebook 统一结构

每个 `lab.ipynb` 均包含 6 个步骤，可单独运行、可重复运行：

1. **异常重现**：调用 `inject_*()` 产生真实故障；
2. **取证**：执行 `lab.sh()` / `lab.aliyun()` 收集分层证据；
3. **判断**：把证据收敛为结论；
4. **手工处置**：执行最小必要变更止血；
5. **验证**：`lab.check()` 可执行断言；
6. **自动化**：重置后运行 `automation/*.py --apply` 并校验 `RESULT: REMEDIATED`。

## 自动化脚本约定

- 统一 CLI：`--case <case_id>` + `--apply`（缺省 dry-run）；
- 统一输出：`RESULT: REMEDIATED` 或 `RESULT: FAILED`；
- 统一退出码：成功 `0`，失败 `1`，可直接作为巡检任务或发布门禁。

## 验证记录

| 项目 | 结果 |
|------|------|
| 故障注入 | 12/12 可复现 |
| 自动化脚本 `--apply` | 12/12 返回 `RESULT: REMEDIATED` |
| Notebook 全量执行 | 12/12 PASS（`python tools/run_all_labs.py`） |
| 分类完整性 | 6 大类覆盖计算/网络/存储/数据/应用/可观测性 |
| 文档链接 | README、知识库、案例、Notebook 双向可达 |

复现命令：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tools/run_all_labs.py
```
