# E · 应用运行时

覆盖进程/句柄/端口/发布与配置。核心原则：
**发布后立刻故障，优先回滚；非发布故障，优先看"资源上限"与"监听状态"。**

---

## E1 · FD 泄漏，接口报 Too many open files

**业务现象**
服务运行一段时间后接口 500，日志出现 `OSError: [Errno 24] Too many open files`；
`ss` 显示连接堆积；重启后短时间恢复，随后复发。

**影响与定级**
P1。若进程无法接受新连接 → 该实例完全不可用。

**常见根因**
1. 请求内 `open()` / `socket` / DB 连接未关闭（异常路径最常见）；
2. HTTP 客户端未复用连接池，短连接风暴；
3. `LimitNOFILE` 过小（默认 1024 或 256）；
4. 大量 `TIME_WAIT`（客户端侧），端口耗尽；
5. 日志轮转后旧句柄未释放。

**首查命令**

```bash
ulimit -n
cat /proc/<pid>/limits | grep "open files"
ls /proc/<pid>/fd | wc -l
lsof -p <pid> | awk '{print $5}' | sort | uniq -c | sort -rn | head
cat /proc/<pid>/fd/* 2>/dev/null | head        # 看是否有异常文件/管道
ss -s                                          # 连接状态汇总
```

**判断依据**
- FD 数量随请求量线性增长、重启归零 → 泄漏；
- 大量 `socket` 类型 FD → 连接未关闭；
- FD 数接近 LimitNOFILE → 上限问题（但根因仍需修）。

**处置要点**
1. 止血：滚动重启实例（先摘 SLB 流量）；
2. 提升 `LimitNOFILE`（systemd `LimitNOFILE=65535`）只是缓冲；
3. 修复代码：`with` / `try-finally` / 连接池复用，异常路径必须释放；
4. 压测验证 FD 曲线是否平稳。

**预防与自动化**
- FD 使用率 > 80% 告警（按进程维度）；
- 代码规范 + 静态扫描（未关闭的资源）；
- 自动化：`automation/fd_guard.py`（水位 → 重启 → 提升上限 → 业务校验）。

**关联案例**：[case-10](../02-cases/case-10-fd.md) · [Notebook](../../labs/case-10-fd/lab.ipynb)

---

## E2 · 发布后端口未监听，SLB 全量 502

**业务现象**
发布完成后 1 分钟内 App 大量 502；SLB 后端全部 `abnormal`；ECS 上进程存在但端口未监听，
或监听了错误端口；`systemctl status` 显示 `active` 但探活失败。

**影响与定级**
P0（发布即全量故障）。

**常见根因**
1. 配置文件端口/监听地址错误（只监听 `127.0.0.1`）；
2. 启动参数与环境变量不一致（测试/生产配置混用）；
3. 端口被占用，新进程启动失败但旧进程已停；
4. 依赖未就绪（DB/Redis 连接失败导致启动中断）；
5. 健康检查路径变更未同步 SLB。

**首查命令**

```bash
systemctl status note-api -l
journalctl -u note-api --since "10 min ago"
ss -lntp | grep -E "8080|8081"
curl -v http://127.0.0.1:8080/healthz
grep -n "listen" /etc/xhs/note-api.conf
```

云侧取证：

```bash
aliyun slb DescribeHealthStatus --LoadBalancerId lb-xxx
```

**判断依据**
- 进程在、端口不在 → 监听配置/启动失败；
- 端口在、健康检查失败 → 路径/依赖问题；
- 发布记录时间与故障时间一致 → 优先回滚。

**处置要点**
1. **先回滚**（分钟级恢复），再排查；
2. 回滚要恢复到上一版本配置 + 二进制，不只回滚代码；
3. 复盘补发布门禁：启动后自动探活，失败即自动回滚；
4. 配置中心化，禁止手工改生产配置。

**预防与自动化**
- 发布流水线加入"端口监听 + /healthz + 业务冒烟"三层校验；
- 灰度发布（1 台 → 20% → 全量），每步观察 5 分钟；
- 自动化：`automation/deploy_gate.py`（三层探活 → 自动回滚 → 冒烟 → 退出码供 CI 卡点）。

**关联案例**：[case-11](../02-cases/case-11-release.md) · [Notebook](../../labs/case-11-release/lab.ipynb)
