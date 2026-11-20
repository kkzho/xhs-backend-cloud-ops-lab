# A · 计算与实例

覆盖 ECS 实例、CPU/内存资源、进程与定时任务。核心原则：**先确认是"资源不够"还是"用法不对"**，
两者处置完全不同（扩容 vs 限制/修复）。

---

## A1 · CPU 被异常任务打满，接口超时

**业务现象**
Feed 首屏加载变慢，P99 从 120ms 涨到 800ms+；App 出现"加载失败，请重试"；单台 ECS 的
CPU 使用率持续 95%+，同机其他服务被拖慢。

**影响与定级**
P1（核心功能受损）。若 SLB 后端全部实例同时触发 → 升级 P0。

**常见根因**
1. 定时任务（榜单预计算、数据同步）并发执行且无锁、无限流；
2. 死循环 / 正则回溯 / 大文件解析；
3. 突发流量（活动、爬虫）打满 worker；
4. 实例规格与业务量不匹配（长期高水位）。

**首查命令**

```bash
uptime                                   # load average 是否远高于核数
ps -eo pid,ppid,user,%cpu,%mem,etime,cmd --sort=-%cpu | head -20
top -H -p <pid>                          # 线程级热点
cat /etc/cron.d/*                        # 是否有并发定时任务
ls -l /proc/<pid>/cwd                    # 进程工作目录，确认是哪个任务
```

云侧取证：

```bash
aliyun cms QueryMetricList --Namespace acs_ecs_dashboard \
  --MetricName cpu_total --Dimensions '[{"instanceId":"i-xxx"}]'
```

**判断依据**
- 异常进程的启动时间与告警时间吻合、且属于业务批任务 → 用法问题；
- CPU 高但 QPS 不高 → 不是流量问题；
- 多台实例同时高 → 共同触发的任务/依赖，而非单机故障。

**处置要点**
1. 记录现场（PID、命令行、启动时间、cron 配置）；
2. 终止异常任务（`kill` 优于 `pkill -f`，避免误杀）；
3. 给任务加 `flock` 串行、`nice` 降优先级、`--limit` 限流、错峰执行；
4. 若是流量问题：先限流/扩容，再评估规格。

**预防与自动化**
- 批任务统一走调度平台，禁止散落 crontab；
- 云监控对"非工作时段 CPU > 85%"单独告警；
- 自动化：`automation/cpu_guard.py`（采样 → 识别 → 终止 → 改 cron → 校验）。

**关联案例**：[case-01](../02-cases/case-01-cpu-runaway.md) · [Notebook](../../labs/case-01-cpu-runaway/lab.ipynb)

---

## A2 · 内存泄漏触发 OOM Kill，服务反复重启

**业务现象**
服务每隔十几分钟重启一次，监控出现"锯齿形"内存曲线；请求偶发 502；`systemctl status`
显示 `Restarts` 增长；`dmesg` 有 `Out of memory: Killed process`。

**影响与定级**
P1。反复重启会造成连接抖动、缓存击穿；若实例上还有其它进程，可能被连带杀死。

**常见根因**
1. 应用层对象未释放（缓存无上限、全局 list/dict 持续增长）；
2. 连接/文件句柄持有大 buffer；
3. `-Xmx` 或容器 memory limit 配置小于实际峰值；
4. 大查询一次性加载全量数据。

**首查命令**

```bash
dmesg -T | grep -i -E "out of memory|killed process" | tail
systemctl status note-api                 # Restarts / Memory
ps -o pid,rss,vsz,etime,cmd -p <pid>
cat /proc/<pid>/status | grep -E "VmRSS|VmPeak"
journalctl -u note-api --since "30 min ago" | grep -i -E "oom|memory"
```

**判断依据**
- RSS 单调上涨、重启后回落 → 泄漏（不是流量）；
- OOM 记录中的进程名与业务进程一致；
- 泄漏速率与某接口调用量相关 → 定位到接口。

**处置要点**
1. **先止血**：从 SLB 摘除该实例 → 重启 → 挂回（避免重启期间接流量）；
2. 保留现场：`py-spy dump` / heap profile / `jmap`，不要直接覆盖日志；
3. 临时提高 MemoryMax 只能争取时间，不是修复；
4. 修复后灰度观察 24h 内存曲线。

**预防与自动化**
- 应用内置 RSS 上报，云监控对"1h 增长 > 30%"告警；
- systemd 配置 `MemoryMax` + `Restart=always`，防止拖垮整机；
- 自动化：`automation/memory_guard.py`（水位检测 → 摘流 → 重启 → 挂回 → 校验）。

**关联案例**：[case-02](../02-cases/case-02-memory-oom.md) · [Notebook](../../labs/case-02-memory-oom/lab.ipynb)
