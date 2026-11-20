# 故障案例手册（12 个）

每个案例对应一个知识库条目、一个可运行 Notebook、一个自动化脚本。结构统一为：

- **运维前**：业务背景 / 触发诱因 / 告警信息
- **运维中**：处理逻辑线 / 逐步拆解（工具、命令、判断、措施）/ 关键取舍
- **运维后**：复盘 / 预防措施 / 自动化代码沉淀

| # | 案例 | 分类 | 等级 | Notebook |
|---|------|------|------|----------|
| 01 | [榜单任务并发导致 CPU 打满](case-01-cpu-runaway.md) | 计算 | P1 | [lab](../../labs/case-01-cpu-runaway/lab.ipynb) |
| 02 | [内存泄漏触发 OOM 反复重启](case-02-memory-oom.md) | 计算 | P1 | [lab](../../labs/case-02-memory-oom/lab.ipynb) |
| 03 | [SLB 健康检查失败全量 502](case-03-slb-502.md) | 网络 | P0 | [lab](../../labs/case-03-slb-502/lab.ipynb) |
| 04 | [安全组误删导致调用超时](case-04-sg-timeout.md) | 网络 | P1 | [lab](../../labs/case-04-sg-timeout/lab.ipynb) |
| 05 | [HTTPS 证书过期](case-05-tls-cert.md) | 网络 | P0 | [lab](../../labs/case-05-tls-cert/lab.ipynb) |
| 06 | [日志写满云盘](case-06-disk-full.md) | 存储 | P1 | [lab](../../labs/case-06-disk-full/lab.ipynb) |
| 07 | [inode 耗尽](case-07-inode.md) | 存储 | P1 | [lab](../../labs/case-07-inode/lab.ipynb) |
| 08 | [RDS 连接数打满](case-08-rds-conn.md) | 数据库 | P1 | [lab](../../labs/case-08-rds-conn/lab.ipynb) |
| 09 | [Redis 内存打满](case-09-redis.md) | 缓存 | P1 | [lab](../../labs/case-09-redis/lab.ipynb) |
| 10 | [FD 泄漏](case-10-fd.md) | 应用 | P1 | [lab](../../labs/case-10-fd/lab.ipynb) |
| 11 | [发布端口错误全量 502](case-11-release.md) | 应用 | P0 | [lab](../../labs/case-11-release/lab.ipynb) |
| 12 | [日志断流与告警风暴](case-12-alert.md) | 可观测性 | P2 | [lab](../../labs/case-12-alert/lab.ipynb) |

> 建议阅读顺序：01 → 06 → 08（覆盖 Linux/存储/数据库三大基础），再按岗位挑网络或应用类。
