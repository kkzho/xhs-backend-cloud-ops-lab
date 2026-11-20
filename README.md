# 小红书 App 后端 · 阿里云运维实操实验室

> 用 12 个可复现的线上故障，把 Linux 排查、云上计算/网络/存储/数据库、监控告警串成一套可演练、可验证的运维能力证明。

- **业务背景**：模拟小红书笔记与 Feed 后端（`note-api`）在阿里云杭州地域的生产部署：ECS + SLB + RDS MySQL + Redis + SLS + 云监控。
- **项目形态**：`知识库 → 故障案例手册 → 可运行 Notebook 演练 → 自动化脚本沉淀`，四层一一对应。
- **不需要阿里云账号**：云侧 OpenAPI 由沙箱模拟器返回同构数据（响应含 `"Mock": true`），本地故障由真实进程、真实 socket、真实证书复现，零成本、可离线、可重复执行。

> 免责声明：本项目为个人学习与面试演示用，与小红书官方无关；业务数据、实例 ID、域名均为虚构。

---

## 一、这个项目解决什么问题

一线云运维/售后运维的日常，不是"会背命令"，而是：

1. 看到告警后，能在 **5 分钟内判断影响面**（是单机、单可用区，还是全站）；
2. 能用 **证据链** 定位根因（指标 → 进程 → 日志 → 配置 → 变更）；
3. 处置时知道 **先止血还是先定位**，以及每一步的副作用；
4. 事后能沉淀 **自动化与预防措施**，而不是重复救火。

仓库中的每个案例都按 `运维前（背景/诱因/告警） → 运维中（处理逻辑线/工具/措施） → 运维后（复盘/预防/自动化代码）` 组织，并在 Notebook 中真实执行。

## 二、快速开始

```bash
git clone git@github.com:kkzho/xhs-backend-cloud-ops-lab.git
cd xhs-backend-cloud-ops-lab
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 方式一：交互式演练（推荐）
jupyter lab
# 打开 labs/case-01-cpu-runaway/lab.ipynb，逐个 Cell 执行

# 方式二：无头验证全部实验（CI 友好）
python tools/run_all_labs.py
```

要求：Python ≥ 3.10、Linux/macOS；无需 root、无需 Docker。所有实验数据写入 `labs/.runtime/`（已 gitignore），随时可删。

## 三、仓库结构

```text
.
├── docs/
│   ├── 00-guidance.md              # 学习路径、约定、评分自检
│   ├── 01-knowledge-base/          # 高频问题知识库（6 大类 / 12 个问题）
│   │   ├── README.md               # 分类体系与索引
│   │   ├── 01-compute.md
│   │   ├── 02-network.md
│   │   ├── 03-storage.md
│   │   ├── 04-data.md
│   │   ├── 05-application.md
│   │   └── 06-observability.md
│   ├── 02-cases/                   # 12 份完整处置手册（运维前/中/后）
│   └── 03-progress.md              # 项目进度存档（各阶段产出清单）
├── labs/
│   ├── common/                     # 演练沙箱框架
│   │   ├── xhs_lab.py              # 故障注入 / 命令路由 / 云侧取证 / 断言
│   │   ├── aliyun_sim.py           # ECS/SLB/RDS/Redis/CMS/CAS/SLS OpenAPI 模拟
│   │   └── sims/                   # 真实进程：应用、SLB、MySQL-lite、Redis-lite、日志 Agent
│   └── case-xx-*/lab.ipynb         # 每个案例一个 Notebook（异常重现→指引→操作→验证）
├── automation/                     # 复盘沉淀的自动化脚本（可单独运行）
├── tools/run_all_labs.py           # 批量执行与结果汇总
└── requirements.txt
```

## 四、案例总览

| # | 业务问题 | 分类 | 关键证据 | 自动化脚本 | Notebook |
|---|---------|------|---------|-----------|----------|
| 01 | 榜单任务并发导致 CPU 打满，Feed 超时 | 计算与实例 | `ps`、CMS `cpu_total`、cron | `cpu_guard.py` | [lab](labs/case-01-cpu-runaway/lab.ipynb) |
| 02 | 服务内存泄漏触发 OOM Kill 反复重启 | 计算与实例 | RSS 曲线、`dmesg`、重启次数 | `memory_guard.py` | [lab](labs/case-02-memory-oom/lab.ipynb) |
| 03 | SLB 后端健康检查失败，接口 502 | 网络与负载均衡 | SLB 健康状态、直连探活、访问日志 | `slb_recover.py` | [lab](labs/case-03-slb-502/lab.ipynb) |
| 04 | 安全组误删，VPC 内调用超时 | 网络与负载均衡 | `tcpdump`、安全组规则、探测 | `sg_audit.py` | [lab](labs/case-04-sg-timeout/lab.ipynb) |
| 05 | HTTPS 证书过期，App 全量请求失败 | 网络与负载均衡 | `openssl s_client`、客户端校验 | `cert_renew.py` | [lab](labs/case-05-tls-cert/lab.ipynb) |
| 06 | 日志写满云盘，`No space left on device` | 存储与文件系统 | `df`、`du`、`lsof +L1` | `disk_guard.py` | [lab](labs/case-06-disk-full/lab.ipynb) |
| 07 | 会话临时文件耗尽 inode，无法创建文件 | 存储与文件系统 | `df -i`、文件密度、TTL | `inode_guard.py` | [lab](labs/case-07-inode/lab.ipynb) |
| 08 | 连接池泄漏打满 RDS，接口报 1040 | 数据库与缓存 | `SHOW PROCESSLIST`、连接使用率 | `rds_session_guard.py` | [lab](labs/case-08-rds-conn/lab.ipynb) |
| 09 | 大 key + 热 key 打满 Redis，写入被拒 | 数据库与缓存 | `INFO memory`、`--bigkeys`、命中率 | `redis_mem_guard.py` | [lab](labs/case-09-redis/lab.ipynb) |
| 10 | FD 泄漏，`Too many open files` | 应用运行时 | `lsof`、`fds/limit`、错误日志 | `fd_guard.py` | [lab](labs/case-10-fd/lab.ipynb) |
| 11 | 发布配置端口错误，SLB 全量 502 | 应用运行时 | `ss`、`curl`、发布历史 | `deploy_gate.py` | [lab](labs/case-11-release/lab.ipynb) |
| 12 | 日志断流 + 告警规则失配引发告警风暴 | 可观测性与告警 | SLS 延迟、告警历史、规则配置 | `alert_dedup.py` | [lab](labs/case-12-alert/lab.ipynb) |

每份案例的"运维前/中/后"完整拆解见 [docs/02-cases](docs/02-cases)，知识库条目见 [docs/01-knowledge-base](docs/01-knowledge-base)。

## 五、沙箱是怎么做到"真实"的

| 能力 | 实现方式 | 说明 |
|------|---------|------|
| CPU 打满 | 真实多进程忙循环 | `ps`/`top` 可直接观察到异常进程 |
| 内存泄漏 / OOM | 真实进程持续申请内存，看门狗按水位 SIGKILL 并拉起 | 生成 `dmesg` OOM 记录与重启次数 |
| SLB 502 | 真实七层转发进程 + 2s 健康检查 | 后端不健康时真实返回 `SLB_502` |
| 磁盘写满 | 沙箱卷配额 + 真实文件写入 | `df/du` 数值来自真实目录统计 |
| inode 耗尽 | 真实创建 1.9 万个小文件 | `df -i` 使用率与 `find` 结果真实 |
| TLS 过期 | `cryptography` 签发已过期证书，HTTPS 服务真实加载 | 客户端报 `certificate has expired` |
| RDS 连接打满 | 真实 TCP 服务，超过 `max_connections` 返回 1040 | `SHOW PROCESSLIST` / `KILL` 可操作 |
| Redis 内存打满 | 真实持有内存的 Redis-lite，执行 maxmemory 与淘汰策略 | `INFO`/`--bigkeys`/`CONFIG` 输出与线上一致 |
| FD 泄漏 | 子进程 `RLIMIT_NOFILE=256`，请求内泄漏句柄 | 真实触发 `Errno 24` |
| 云产品数据 | `state.json` 渲染 OpenAPI 响应 | 字段与阿里云一致，`"Mock": true` |

> 生产环境请把 Notebook 中的 `lab.sh()` / `lab.aliyun()` 替换为等价命令：`aliyun ecs ...`、`systemctl`、`journalctl`、`ss`、`lsof`。每份 Notebook 末尾都附有命令对照表。

## 六、能力映射（面试可讲）

- **Linux**：进程/CPU/内存/FD/文件系统排查，`ps/top/lsof/ss/df/du/find/journalctl` 组合取证；
- **云网络**：SLB 四层/七层健康检查、后端摘除与挂载、安全组最小权限、证书生命周期；
- **存储**：云盘水位、inode、已删除但被占用的句柄、日志轮转与保留策略；
- **数据库/缓存**：RDS 连接数与会话治理、慢查询、Redis 大 key/热 key/淘汰策略；
- **可观测性**：云监控指标取证、告警规则收敛（阈值/持续时长/静默/分组）、日志采集断流定位；
- **工程化**：把每次处置沉淀为可执行脚本（dry-run + apply + 校验 + 退出码），可接入巡检/CI。

## 七、License

MIT
