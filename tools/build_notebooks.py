#!/usr/bin/env python3
"""生成 12 个案例的 Notebook 实验（内容以案例手册与自动化脚本为准）。

用法：
    .venv/bin/python tools/build_notebooks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import nbformat as nbf

REPO = Path(__file__).resolve().parent.parent
LABS = REPO / "labs"

HEADER = """\
> 沙箱实验：真实进程 + 阿里云 OpenAPI 模拟（响应带 `Mock: true`）。
> 云产品字段/命令与生产一致，资源规格与账单为模拟值。

## 场景

{scenario}

## 练习目标

{bullets}

## 前置

- 仓库根目录执行 `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
- 使用该 venv 的 Python kernel 打开本 Notebook；或运行 `tools/run_all_labs.py` 批量执行
"""

SETUP = """\
# 环境准备：定位仓库根目录并加载沙箱
import json, os, signal, ssl, socket, sys, time, gzip, shutil, urllib.parse, urllib.request
from pathlib import Path

REPO = Path.cwd().resolve()
while not (REPO / "labs" / "common" / "xhs_lab.py").exists():
    if REPO.parent == REPO:
        raise RuntimeError("请在 xhs-backend-cloud-ops-lab 仓库内运行本 Notebook")
    REPO = REPO.parent
sys.path.insert(0, str(REPO))

from labs.common import Lab

lab = Lab("{case_id}")
lab.reset(start_api=True)
{setup_extra}
lab.summary()
"""

FOOTER = """\
## 复盘与思考题

{questions}

## 收尾

沙箱资源会在本 Cell 中停止；重新运行请从第 1 个代码 Cell 开始。
"""

CASES = [
    {
        "id": "case-01-cpu-runaway",
        "n": "01",
        "title": "榜单任务并发导致 CPU 打满，Feed 接口超时",
        "scenario": "榜单预计算 cron 被改成每分钟执行且没有加锁，单轮任务未结束下一轮已启动，"
                    "4 个忙循环进程把 8 核实例的 CPU 吃满，Feed P99 从 120ms 涨到 820ms，"
                    "SLB 健康检查失败并摘除后端。",
        "bullets": "- 用 `uptime` / `ps` 区分“资源不足”与“进程异常”\n"
                   "- 定位定时任务并发叠加的证据\n"
                   "- 先止血（kill）再改造（flock/nice/limit）\n"
                   "- 用 `cpu_guard.py` 完成自动化处置与校验",
        "setup_extra": "",
        "inject": """\
lab.inject_cpu_runaway(procs=4)
time.sleep(1)
print("已注入：4 个忙循环进程（模拟榜单任务并发）")
""",
        "observe": [
            ("系统负载", 'print(lab.sh("uptime"))'),
            ("CPU 排序进程", 'print(lab.sh("ps -eo pid,ppid,user,%cpu,etime,cmd --sort=-%cpu | head -8"))'),
            ("定时任务配置", 'print(lab.sh("cat /etc/cron.d/xhs-ranking"))'),
            ("业务探活", 'print(lab.curl(lab.api + "/api/v1/feed"))'),
            ("SLB 健康状态", 'print(lab.aliyun("slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01"))'),
        ],
        "diagnose": """\
fault = lab.state["faults"]["cpu"]
print("异常进程数:", len(fault["pids"]))
print("单进程 CPU:", fault["cpu"], "%（单核口径）")
print("cron 是否加锁:", "flock" in lab.sh("cat /etc/cron.d/xhs-ranking"))
print("判断：进程数随时间线性增加 → 定时任务并发叠加，而非流量突增")
""",
        "manual": """\
# 先 TERM/SIGKILL 清理任务进程（生产先用 TERM 保护事务）
for pid in lab.state["faults"]["cpu"]["pids"]:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass

# 改造 crontab：串行 + 降优先级 + 限流
lab.write("/etc/cron.d/xhs-ranking",
          "*/5 * * * * xhs flock -n /var/lock/ranking.lock nice -n 10 "
          "python3 /srv/xhs/jobs/ranking_precompute.py --limit 2000\\n")
lab.state["faults"].pop("cpu", None)
lab._save()
time.sleep(1)
print(lab.sh("ps -eo pid,%cpu,cmd --sort=-%cpu | head -5"))
""",
        "checks": [
            ("异常进程已清除", "'while True' not in lab.sh('ps aux')"),
            ("cron 已加锁限流", "'flock' in lab.path('/etc/cron.d/xhs-ranking').read_text()"),
            ("服务健康", "'HTTP_CODE=200' in lab.curl(lab.api + '/healthz')"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "cpu_guard.py",
        "auto_md": "脚本采样 CPU 占用、识别业务组异常进程，记录现场后终止，并把 crontab 改写为 "
                   "`flock + nice + --limit`，最后校验残留进程与业务探活。",
        "questions": "1. 如果 CPU 高但流量正常、进程是 Java，你会如何进一步定位？\n"
                     "2. 为什么“CPU 高”不等于“要扩容”？\n"
                     "3. kill 后任务需要补跑，如何保证不再次叠加？",
    },
    {
        "id": "case-02-memory-oom",
        "n": "02",
        "title": "内存泄漏触发 OOM Kill，服务反复重启",
        "scenario": "笔记详情缓存改版后引入对象泄漏，进程 RSS 持续上涨，超过 220MB 后被内核 OOM Kill，"
                    "systemd 自动拉起，形成“重启—再涨—再杀”的循环，期间接口间歇性 502。",
        "bullets": "- 用 `dmesg` 与重启次数确认 OOM Kill\n"
                   "- 摘流量后重启止血，保留现场\n"
                   "- 用 systemd `MemoryMax` 限制内存并配置告警\n"
                   "- 用 `memory_guard.py` 完成自动化处置",
        "setup_extra": "",
        "inject": """\
lab.inject_memory_leak(limit_mb=220)
print("OOM 次数:", lab.state["faults"]["memory"]["oom"])
print("服务重启次数:", lab.state["services"]["xhs-note-api"]["restarts"])
""",
        "observe": [
            ("内核 OOM 记录", 'print(lab.sh("cat /var/log/xhs/dmesg.log"))'),
            ("服务状态", 'print(lab.sh("systemctl status xhs-note-api"))'),
            ("运行指标", 'print(json.dumps(lab.api_stats(), ensure_ascii=False, indent=2))'),
            ("应用日志", 'print(lab.sh("tail -n 5 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
st = lab.api_stats()
fault = lab.state["faults"]["memory"]
print("当前 RSS:", st.get("rss_mb"), "MB；水位线:", fault["limit_mb"], "MB；OOM:", fault["oom"], "次")
print("重启次数:", lab.state["services"]["xhs-note-api"]["restarts"])
print("判断：RSS 单调上涨且触发 OOM → 内存泄漏，而非流量突增")
""",
        "manual": """\
svc = lab.state["services"]["xhs-note-api"]
lb = lab.state["slb"]["LoadBalancerId"]

# 1) 先摘流量，避免重启期间请求打到半启动实例
lab.aliyun(f"slb RemoveBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
# 2) 重启止血（保留 dmesg / 日志现场）
lab.kill_service()
lab.start_api(port=svc["port"], nofile=256)
# 3) 加内存上限，防止单进程拖垮整机
lab.write("/etc/systemd/system/xhs-note-api.service.d/memory.conf",
          "[Service]\\nMemoryMax=1G\\nMemoryHigh=800M\\nRestart=always\\nRestartSec=2\\n")
# 4) 健康后挂回
lab.aliyun(f"slb AddBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
time.sleep(1)
print("重启后 RSS:", lab.api_stats().get("rss_mb"), "MB")
""",
        "checks": [
            ("RSS 已回落", "lab.api_stats().get('rss_mb', 999) < 120"),
            ("发生过 OOM Kill", "lab.state['services']['xhs-note-api']['restarts'] >= 1"),
            ("服务健康", "'HTTP_CODE=200' in lab.curl(lab.api + '/healthz')"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "memory_guard.py",
        "auto_md": "脚本读取 RSS 与 OOM 记录，先经 SLB 摘除后端，再重启服务并写入 "
                   "`MemoryMax`，健康后自动挂回，最后校验 RSS 与探活。",
        "questions": "1. `dmesg` 中哪些字段能证明是 OOM Kill 而不是应用主动退出？\n"
                     "2. 为什么先摘流量再重启？\n"
                     "3. MemoryMax 与容器 memory limit 同时存在时以哪个为准？",
    },
    {
        "id": "case-03-slb-502",
        "n": "03",
        "title": "SLB 健康检查失败，全量 502",
        "scenario": "一次依赖变更后，note-api 的 `/healthz` 因数据库超时返回 500，SLB 连续 2 次探测失败"
                    "摘除唯一后端，App 全量请求返回 502。应用进程本身存活，直连业务接口也正常。",
        "bullets": "- 从 SLB 侧取证：健康状态、后端列表\n"
                   "- 区分“后端真故障”与“探活配置/依赖问题”\n"
                   "- 摘除 → 修复 → 等待健康 → 挂回\n"
                   "- 用 `slb_recover.py` 完成自动化处置",
        "setup_extra": "lab.start_slb()",
        "inject": """\
lab.inject_slb_backend_unhealthy()
print("已注入：后端健康检查失败（/healthz 依赖 db_timeout）")
""",
        "observe": [
            ("SLB 健康状态", 'print(lab.aliyun("slb DescribeHealthStatus --LoadBalancerId lb-bp1xhsslb01"))'),
            ("直连后端 /healthz", 'print(lab.curl(lab.api + "/healthz"))'),
            ("经 SLB 访问", 'print(lab.curl(lab.slb + "/api/v1/feed"))'),
            ("SLB 转发统计", 'print(json.dumps(lab.slb_stats(), ensure_ascii=False, indent=2))'),
        ],
        "diagnose": """\
print("后端状态:", lab.state["slb"]["BackendServers"][0]["HealthStatus"])
print("直连业务接口:", lab.curl(lab.api + "/api/v1/feed").splitlines()[-1])
print("经 SLB:", lab.curl(lab.slb + "/api/v1/feed").splitlines()[-1])
print("判断：直连正常、经 SLB 502 → 问题在健康检查/后端摘除，而非业务代码完全不可用")
""",
        "manual": """\
lb = lab.state["slb"]["LoadBalancerId"]
# 1) 摘除异常后端，防止 502 继续放大
lab.aliyun(f"slb RemoveBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
# 2) 修复后端（重启清理异常依赖状态）
lab.restart_api(port=lab.ports["api"])
# 3) 等健康检查通过后挂回（挂回后列表恢复）
lab.wait_until(lambda: "HTTP_CODE=200" in lab.curl(lab.slb + "/api/v1/feed"), timeout=20, interval=1)
lab.aliyun(f"slb AddBackendServers --LoadBalancerId {lb} --BackendServers i-bp1xhsapi01")
lab.state["slb"]["BackendServers"][0]["HealthStatus"] = "normal"
lab._save()
print("经 SLB:", lab.curl(lab.slb + "/api/v1/feed").splitlines()[-1])
""",
        "checks": [
            ("经 SLB 返回 200", "'HTTP_CODE=200' in lab.curl(lab.slb + '/api/v1/feed')"),
            ("后端状态 normal", "lab.state['slb']['BackendServers'][0]['HealthStatus'] == 'normal'"),
        ],
        "auto_setup": "lab.reset(start_api=True)\nlab.start_slb()",
        "auto_script": "slb_recover.py",
        "auto_md": "脚本从 `DescribeHealthStatus` 取证，直连判断故障域，先摘除后端止血，"
                   "重启恢复后等待健康检查通过并挂回。",
        "questions": "1. 健康检查路径应该检查哪些依赖？为什么不宜直接复用重业务接口？\n"
                     "2. 只有 1 个后端时，摘除等于全站不可用，如何设计高可用？\n"
                     "3. 502、504、503 分别可能对应哪一层的问题？",
    },
    {
        "id": "case-04-sg-timeout",
        "n": "04",
        "title": "安全组误删，VPC 内调用超时",
        "scenario": "一次安全组收敛变更误删了 note-api 端口的入方向规则，VPC 内上游调用出现 TCP 连接超时"
                    "（SYN 无响应、无 RST），实例本身运行正常。",
        "bullets": "- 用 `tcp_probe` 观察“超时”而非“拒绝”\n"
                   "- 对比安全组规则与基线\n"
                   "- 以最小权限补回规则并复测\n"
                   "- 用 `sg_audit.py` 完成自动化处置",
        "setup_extra": "",
        "inject": """\
lab.inject_sg_misconfig()
print("已注入：删除 api 端口的安全组入方向规则")
""",
        "observe": [
            ("安全组规则", 'print(lab.aliyun("ecs DescribeSecurityGroups --SecurityGroupId sg-bp1xhsapi"))'),
            ("连通性探测", 'print(lab.tcp_probe("10.10.1.11", lab.ports["api"]))'),
            ("实例状态", 'print(lab.aliyun("ecs DescribeInstances"))'),
        ],
        "diagnose": """\
sg = lab.state["security_groups"][0]
ports = [r["PortRange"] for r in sg["Rules"]]
print("入方向端口:", ports)
print("业务端口:", lab.ports["api"])
print("探测结果:", lab.tcp_probe("10.10.1.11", lab.ports["api"]))
print("判断：timeout（无 RST）通常指向安全组/网络 ACL 丢包，而非进程未监听")
""",
        "manual": """\
sg = lab.state["security_groups"][0]
port = lab.ports["api"]
# 最小权限：仅放通 VPC 网段到业务端口
lab.aliyun(f"ecs AuthorizeSecurityGroup --SecurityGroupId {sg['SecurityGroupId']} "
           f"--IpProtocol tcp --PortRange {port}/{port} --SourceCidrIp 10.10.0.0/16 "
           f"--Description xhs-api-vpc")
lab.state["faults"].pop("network", None)
lab._save()
print(lab.tcp_probe("10.10.1.11", port))
""",
        "checks": [
            ("端口已放通", "lab.tcp_probe('10.10.1.11', lab.ports['api'])['result'] == 'open'"),
            ("规则存在", "any(r['PortRange'].startswith(str(lab.ports['api'])) for r in lab.state['security_groups'][0]['Rules'])"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "sg_audit.py",
        "auto_md": "脚本把安全组规则与基线比对，找出缺口后按最小权限提交 `AuthorizeSecurityGroup`，"
                   "并用策略模拟器复测连通性。",
        "questions": "1. 连接超时与连接拒绝分别说明什么？\n"
                     "2. 安全组变更如何做审批与回滚？\n"
                     "3. 为什么安全组规则要按“网段 + 端口”最小化，而不是全放通？",
    },
    {
        "id": "case-05-tls-cert",
        "n": "05",
        "title": "HTTPS 证书过期，App 全量请求失败",
        "scenario": "接入层证书到期未续签，客户端 TLS 握手报 `certificate has expired`，"
                    "App 所有 HTTPS 请求失败；服务进程与端口均正常。",
        "bullets": "- 用 `openssl s_client` 读取 `notAfter`\n"
                   "- 复现客户端证书校验失败\n"
                   "- 签发/替换证书并重启 HTTPS 服务\n"
                   "- 用 `cert_renew.py` 完成自动化巡检与续签",
        "setup_extra": """\
def cert_ok():
    try:
        ctx = ssl.create_default_context(cafile=str(lab.dir / "tls/server.crt"))
        with socket.create_connection(("127.0.0.1", lab.ports["api"]), timeout=3) as s:
            with ctx.wrap_socket(s, server_hostname="localhost"):
                return True
    except Exception:
        return False
""",
        "inject": """\
lab.inject_cert_expired()
time.sleep(1)
print("已注入：过期证书 + HTTPS 模式重启，cert_ok() =", cert_ok())
""",
        "observe": [
            ("证书有效期", 'print(lab.sh("openssl x509 -in %s -noout -subject -dates" % (lab.dir / "tls/server.crt")))'),
            ("证书订单", 'print(lab.aliyun("cas ListUserCertificateOrder"))'),
            ("客户端校验", 'print("证书校验通过" if cert_ok() else "证书校验失败：certificate has expired")'),
            ("服务日志", 'print(lab.sh("tail -n 3 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
print("证书状态:", lab.state["certs"][0]["Status"], "notAfter:", lab.state["certs"][0]["NotAfter"])
print("客户端校验:", "通过" if cert_ok() else "失败")
print("判断：端口可连但 TLS 校验失败 → 证书问题，而非网络或进程问题")
""",
        "manual": """\
from labs.common.sims.certs import make_cert

# 1) 签发新证书（生产为 CAS 申请/上传）
make_cert(lab.dir / "tls/server.crt", lab.dir / "tls/server.key", days=90)
# 2) 重启 HTTPS 服务加载新证书（生产为 SLB 证书替换/热加载）
lab.restart_api(port=lab.ports["api"], tls=True)
lab.state["certs"][0].update({
    "NotAfter": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * 90)),
    "Status": "valid",
})
lab._save()
print("证书替换完成，客户端校验:", "通过" if cert_ok() else "失败")
""",
        "checks": [
            ("HTTPS 校验通过", "cert_ok()"),
            ("证书状态 valid", "lab.state['certs'][0]['Status'] == 'valid'"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "cert_renew.py",
        "auto_md": "脚本读取证书 `notAfter` 并做客户端校验，失败时签发新证书、重启 HTTPS 服务并再次校验证书链。",
        "questions": "1. 证书过期前应设置几级提醒？分别提前多久？\n"
                     "2. 只更新了 SLB 证书但后端仍用旧证书，会有什么现象？\n"
                     "3. 如何避免证书私钥泄露？",
    },
    {
        "id": "case-06-disk-full",
        "n": "06",
        "title": "日志写满云盘，笔记服务写入失败",
        "scenario": "日志按天切割但从不清理，大促期间日志量翻倍写满 /data；同时有一个已删除但被进程持有的"
                    "日志文件，`rm` 后空间不释放，服务写入报 `Errno 28`。",
        "bullets": "- 用 `df -h` / `du -sh` 定位空间占用\n"
                   "- 用 `lsof +L1` 找出 deleted-open 文件\n"
                   "- 清理 + 重启释放句柄，恢复写入\n"
                   "- 用 `disk_guard.py` 完成自动化处置",
        "setup_extra": "",
        "inject": """\
lab.inject_disk_full()
used, _ = lab.disk_usage()
print(f"已注入：/data 使用率 {used / lab.quota_mb * 100:.0f}%")
""",
        "observe": [
            ("分区使用率", 'print(lab.sh("df -h /data"))'),
            ("目录占用", 'print(lab.sh("du -sh /data/*"))'),
            ("deleted-open 文件", 'print(lab.sh("lsof +L1"))'),
            ("应用日志", 'print(lab.sh("tail -n 3 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
used, _ = lab.disk_usage()
fault = lab.state["faults"]["disk"]
print(f"/data 使用率: {used / lab.quota_mb * 100:.0f}%")
print("deleted-open:", fault["deleted_open"])
print("判断：空间耗尽且存在 deleted 句柄 → 需要“清理 + 释放句柄”，只 rm 无效")
""",
        "manual": """\
# 1) 按保留策略清理历史日志（保留最近 2 个）
logs = sorted((lab.data / "logs").glob("app-*.log"), key=lambda f: f.stat().st_mtime)
for f in logs[:-2]:
    f.unlink(missing_ok=True)

# 2) 重启持有 deleted 文件句柄的进程，释放空间
svc = lab.state["services"]["xhs-note-api"]
lab.kill_service()
lab.start_api(port=svc["port"])
lab.state["faults"].pop("disk", None)
lab._save()
time.sleep(0.5)
used, _ = lab.disk_usage()
print(f"处置后使用率: {used / lab.quota_mb * 100:.0f}%")
""",
        "checks": [
            ("使用率 < 70%", "lab.disk_usage()[0] / lab.quota_mb < 0.7"),
            ("服务健康", "'HTTP_CODE=200' in lab.curl(lab.api + '/healthz')"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "disk_guard.py",
        "auto_md": "脚本用 `df/du/lsof +L1` 取证，按保留策略清理并压缩日志，重启持有 deleted 句柄的进程，"
                   "最后校验水位回落。",
        "questions": "1. `df` 100% 但 `du` 只有 60%，可能有哪些原因？\n"
                     "2. 生产重启释放句柄前，如何把影响面压到最小？\n"
                     "3. 日志要保留 180 天，本地盘方案该如何调整？",
    },
    {
        "id": "case-07-inode",
        "n": "07",
        "title": "会话临时文件耗尽 inode，磁盘“有空间却写不进去”",
        "scenario": "游客模式的草稿会话文件没有 TTL 清理，/data/sessions 堆积近 2 万个空文件，"
                    "`df -h` 显示仍有空间，但写入报 `Errno 28 (inode exhausted)`。",
        "bullets": "- 用 `df -i` 识别 inode 耗尽\n"
                   "- 用 `find | wc -l` 定位高密度小文件目录\n"
                   "- 按 TTL 清理并验证写入恢复\n"
                   "- 用 `inode_guard.py` 完成自动化处置",
        "setup_extra": "",
        "inject": """\
lab.inject_inode_exhaustion()
_, files = lab.disk_usage()
print(f"已注入：{files} 个文件 / inode 上限 {lab.inode_limit}")
""",
        "observe": [
            ("空间使用率", 'print(lab.sh("df -h /data"))'),
            ("inode 使用率", 'print(lab.sh("df -i /data"))'),
            ("会话文件数", 'print(lab.sh("find /data/sessions -type f | wc -l"))'),
            ("会话目录大小", 'print(lab.sh("du -sh /data/sessions"))'),
        ],
        "diagnose": """\
used, files = lab.disk_usage()
print(f"空间: {used / lab.quota_mb * 100:.0f}%；inode: {files / lab.inode_limit * 100:.0f}%")
print("判断：空间正常但 inode 接近 100% → 小文件耗尽 inode，不是磁盘容量问题")
""",
        "manual": """\
cutoff = time.time() - 86400
sessions = lab.data / "sessions"
removed = 0
for f in sessions.glob("*.tmp"):
    if f.stat().st_mtime < cutoff:
        f.unlink()
        removed += 1

# 兜底：仍高于 70% 时按最旧 mtime 继续清理到 40%
_, files = lab.disk_usage()
if files / lab.inode_limit > 0.7:
    for f in sorted(sessions.glob("*.tmp"), key=lambda p: p.stat().st_mtime)[:max(files - int(lab.inode_limit * 0.4), 0)]:
        f.unlink(missing_ok=True)

_, files = lab.disk_usage()
print(f"清理 {removed} 个超期文件；inode: {files}/{lab.inode_limit} ({files / lab.inode_limit * 100:.0f}%)")
""",
        "checks": [
            ("inode < 70%", "lab.disk_usage()[1] / lab.inode_limit < 0.7"),
            ("服务健康", "'HTTP_CODE=200' in lab.curl(lab.api + '/healthz')"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "inode_guard.py",
        "auto_md": "脚本读取 `df -i`，按 24h TTL 清理会话文件；若仍高于水位，则按 mtime 清理到 40%，"
                   "并校验 inode 与业务可用性。",
        "questions": "1. 为什么删除大文件后 `df` 变化不明显，删除大量小文件却很明显？\n"
                     "2. XFS 与 ext4 的 inode 分配差异如何影响这类故障？\n"
                     "3. 如何设计会话文件存储，避免“一会话一文件”？",
    },
    {
        "id": "case-08-rds-conn",
        "n": "08",
        "title": "连接池泄漏打满 RDS，笔记接口报 1040",
        "scenario": "详情页异常分支未归还数据库连接，连接数缓慢上涨到 `max_connections=50`，"
                    "应用报 `1040 Too many connections`，SLB 健康检查也随之失败。",
        "bullets": "- 用 `SHOW PROCESSLIST` 区分“连接泄漏”与“慢查询”\n"
                   "- KILL 长空闲会话止血\n"
                   "- 修正连接池参数（pre_ping/recycle/timeout）\n"
                   "- 用 `rds_session_guard.py` 完成自动化处置",
        "setup_extra": "lab.start_mysql()",
        "inject": """\
lab.inject_rds_pool_leak(connections=60)
print("已注入：60 条应用连接（max_connections=50）")
""",
        "observe": [
            ("会话列表", 'print(lab.sh("mysql -e SHOW PROCESSLIST"))'),
            ("连接统计", 'print(json.dumps(lab.mysql_stats(), ensure_ascii=False, indent=2))'),
            ("实例参数", 'print(lab.sh("mysql -e SHOW VARIABLES LIKE max_connections"))'),
            ("应用日志", 'print(lab.sh("tail -n 4 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
st = lab.mysql_stats()
rows = [r for r in st.get("sessions", [])]
print("Threads_connected:", st.get("threads_connected"), "/ max:", st.get("max_connections"))
print("判断：连接数触顶且大量 Sleep → 连接泄漏；慢查询会表现为 Query 状态长时间占用")
""",
        "manual": """\
def sql(q):
    url = f"http://127.0.0.1:{lab.ports['mysql_ctrl']}/sql?q={urllib.parse.quote(q)}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read()).get("rows", [])

rows = sql("show processlist")
killed = 0
for r in rows:
    if r["Command"] == "Sleep" and int(r["Time"]) >= 1:
        sql(f"kill {r['Id']}")
        killed += 1
print("已 KILL", killed, "个空闲会话")

lab.write("/etc/xhs/note-api.conf",
          "# 连接池已修复\\nlisten_port = %d\\n"
          "db.pool.max_open = 20\\ndb.pool.max_idle = 5\\n"
          "db.pool.idle_timeout = 30s\\ndb.pool.health_check = true\\n" % lab.ports["api"])
lab.state["faults"].pop("rds", None)
lab._save()
print("Threads_connected:", lab.mysql_stats().get("threads_connected"))
""",
        "checks": [
            ("连接数回落", "lab.mysql_stats().get('threads_connected', 999) < 30"),
            ("池参数已修正", "'db.pool.health_check = true' in lab.path('/etc/xhs/note-api.conf').read_text()"),
        ],
        "auto_setup": "lab.reset(start_api=True)\nlab.start_mysql()",
        "auto_script": "rds_session_guard.py",
        "auto_md": "脚本统计 Sleep 会话与来源，KILL 长空闲连接，写入连接池修正参数，最后校验 "
                   "`Threads_connected` 回落。",
        "questions": "1. `Sleep` 多与 `Query` 多分别指向什么问题？\n"
                     "2. 为什么 `pool_recycle` 必须小于 RDS 的 `wait_timeout`？\n"
                     "3. 泄漏来自无法修改的第三方 SDK 时，如何兜底？",
    },
    {
        "id": "case-09-redis",
        "n": "09",
        "title": "大 key 撑爆 Redis，榜单缓存写入被拒",
        "scenario": "全站榜单被写成单个 6MB key，分类榜单 10×2MB，`maxmemory=32MB` 且策略为 `noeviction`，"
                    "内存触顶后所有写命令报 OOM，首页榜单降级为空。",
        "bullets": "- 用 `INFO memory` 同时看 used/max/policy/evicted\n"
                   "- 用 `--bigkeys` 找到大 key\n"
                   "- 切淘汰策略止血 + 拆分/删除大 key\n"
                   "- 用 `redis_mem_guard.py` 完成自动化处置",
        "setup_extra": "lab.start_redis()",
        "inject": """\
lab.inject_redis_memory()
print(lab.sh("redis-cli INFO memory"))
""",
        "observe": [
            ("内存与策略", 'print(lab.sh("redis-cli INFO memory"))'),
            ("大 key 扫描", 'print(lab.sh("redis-cli --bigkeys"))'),
            ("写入复现", 'print(lab.sh("redis-cli SET feed:rank:probe 1048576"))'),
            ("应用日志", 'print(lab.sh("tail -n 3 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
info = {}
for line in lab.sh("redis-cli INFO memory").splitlines():
    if ":" in line and not line.startswith("#"):
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
used, maxmem = int(info["used_memory"]), int(info["maxmemory"])
print(f"used={used / 1048576:.1f}MB max={maxmem / 1048576:.1f}MB policy={info['maxmemory_policy']}")
print("判断：used ≈ max 且策略为 noeviction → 写入被拒，需要先恢复写入能力")
""",
        "manual": """\
# 1) 切淘汰策略，秒级恢复写入（可回滚）
lab.sh("redis-cli CONFIG SET maxmemory-policy allkeys-lru")
# 2) 删除/拆分超大 key（榜单可重算）
lab.sh("redis-cli DEL feed:rank:global:all")
lab.sh("redis-cli DEL note:detail:8f2a41c9")
lab.redis("SET feed:rank:global:all 2097152")
print(lab.sh("redis-cli INFO memory"))
print("写入验证:", lab.sh("redis-cli SET feed:rank:smoke 1048576"))
""",
        "checks": [
            ("写入恢复", "'OK' in lab.sh('redis-cli SET feed:rank:smoke 1048576')"),
            ("内存低于上限", "int([l.split(':')[1] for l in lab.sh('redis-cli INFO memory').splitlines() if l.startswith('used_memory:')][0]) <= int([l.split(':')[1] for l in lab.sh('redis-cli INFO memory').splitlines() if l.startswith('maxmemory:')][0])"),
        ],
        "auto_setup": "lab.reset(start_api=True)\nlab.start_redis()",
        "auto_script": "redis_mem_guard.py",
        "auto_md": "脚本读取 `INFO memory` 与 `--bigkeys`，切 `allkeys-lru`、删除超大 key，"
                   "最后用 `SET` 验证写入恢复且内存低于上限。",
        "questions": "1. `noeviction`、`volatile-lru`、`allkeys-lru` 分别适合什么业务？\n"
                     "2. 大 key 除了内存，还有哪些运行时风险？\n"
                     "3. 缓存被淘汰后如何避免请求穿透到 DB？",
    },
    {
        "id": "case-10-fd",
        "n": "10",
        "title": "FD 泄漏耗尽句柄，接口 500 (Too many open files)",
        "scenario": "详情页新增模板文件读取逻辑，异常分支未关闭句柄；`LimitNOFILE=256`，"
                    "约 6 分钟后句柄耗尽，接口返回 500 `TOO_MANY_OPEN_FILES`。",
        "bullets": "- 用错误码 `Errno 24` 快速定性\n"
                   "- 用 `lsof -p` 统计句柄类型\n"
                   "- 重启止血 + 提升 LimitNOFILE\n"
                   "- 用 `fd_guard.py` 完成自动化处置",
        "setup_extra": "",
        "inject": """\
lab.inject_fd_leak(requests=320)
print("已注入：请求泄漏句柄，当前 fds =", lab.api_stats().get("fds"))
""",
        "observe": [
            ("句柄使用", 'print("fds =", lab.api_stats().get("fds"), "/ LimitNOFILE =", lab.state["services"]["xhs-note-api"]["nofile"])'),
            ("句柄明细", 'print(lab.sh("lsof -p %d | head -20" % lab.state["services"]["xhs-note-api"]["pid"]))'),
            ("句柄上限", 'print(lab.sh("ulimit -n"))'),
            ("应用日志", 'print(lab.sh("tail -n 5 /var/log/xhs/note-api.log"))'),
        ],
        "diagnose": """\
st = lab.api_stats()
svc = lab.state["services"]["xhs-note-api"]
print(f"fds={st.get('fds')} / LimitNOFILE={svc.get('nofile')}")
print("业务请求:", lab.curl(lab.api + "/api/v1/note/8f2a41c9").splitlines()[-1])
print("判断：fds 触顶且以 REG 文件为主 → 文件未关闭导致泄漏")
""",
        "manual": """\
svc = lab.state["services"]["xhs-note-api"]
# 1) 提升上限（生产应 65535，沙箱内核生效值 4096）
unit = lab.path("/etc/systemd/system/xhs-note-api.service").read_text()
lab.write("/etc/systemd/system/xhs-note-api.service", unit.replace("LimitNOFILE=256", "LimitNOFILE=65535"))
# 2) 重启释放泄漏句柄
lab.restart_api(port=svc["port"], nofile=4096)
lab.state["faults"].pop("fd", None)
lab._save()
time.sleep(1)
print("重启后 fds:", lab.api_stats().get("fds"))
print("业务请求:", lab.curl(lab.api + "/api/v1/note/8f2a41c9").splitlines()[-1])
""",
        "checks": [
            ("FD 已回落", "lab.api_stats().get('fds', 999) < 100"),
            ("接口恢复 200", "'HTTP_CODE=200' in lab.curl(lab.api + '/api/v1/note/8f2a41c9')"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "fd_guard.py",
        "auto_md": "脚本读取 FD 使用率与上限，超 80% 时提升 `LimitNOFILE`、重启释放句柄，"
                   "最后校验接口 200 与 FD 回落。",
        "questions": "1. `lsof` 中 `REG`、`IPv4`、`PIPE` 分别指向哪类泄漏？\n"
                     "2. 为什么提升上限不能替代修代码？\n"
                     "3. 容器环境下如何正确设置 FD 上限？",
    },
    {
        "id": "case-11-release",
        "n": "11",
        "title": "发布配置端口写错，SLB 全量 502",
        "scenario": "发布模板把 `listen_port` 写成错误端口，进程正常启动但监听端口与 SLB 后端不一致，"
                    "健康检查全部失败，全量请求 502。",
        "bullets": "- 用“发布时间相关性 + 全量失败”快速决策回滚\n"
                   "- 用 `ss -lntp` 对比配置与监听\n"
                   "- 回滚配置并重新冒烟\n"
                   "- 用 `deploy_gate.py` 做发布门禁与自动回滚",
        "setup_extra": "",
        "inject": """\
lab.inject_bad_release()
expected = lab.state["services"]["xhs-note-api"]["port"]
print(f"已注入：服务监听 {expected + 10}，SLB 期望 {expected}")
""",
        "observe": [
            ("监听端口", 'print(lab.sh("ss -lntp"))'),
            ("应用配置", 'print(lab.sh("cat /etc/xhs/note-api.conf"))'),
            ("期望端口探活", 'print(lab.curl("http://127.0.0.1:%d/healthz" % lab.state["services"]["xhs-note-api"]["port"]))'),
            ("错误端口探活", 'print(lab.curl("http://127.0.0.1:%d/healthz" % (lab.state["services"]["xhs-note-api"]["port"] + 10)))'),
            ("发布记录", 'print(json.dumps(lab.state["deploy"], ensure_ascii=False, indent=2))'),
        ],
        "diagnose": """\
svc = lab.state["services"]["xhs-note-api"]
expected = svc["port"]
listening = lab.sh("ss -lntp")
print("期望端口:", expected, "| 是否监听:", f":{expected}" in listening)
print("错误端口:", expected + 10, "| 是否监听:", f":{expected + 10}" in listening)
print("判断：进程存活但期望端口未监听 → 发布配置错误，优先回滚")
""",
        "manual": """\
expected = lab.state["services"]["xhs-note-api"]["port"]
# 回滚配置到上一版本端口
lab.write("/etc/xhs/note-api.conf",
          f"listen_host = 127.0.0.1\\nlisten_port = {expected}\\nworker_processes = 4\\n")
lab.restart_api(port=expected)
lab.state["deploy"]["Status"] = "rolled_back"
lab.state["deploy"]["History"].insert(0, {
    "Version": lab.state["deploy"]["CurrentVersion"],
    "Time": time.strftime("%F %H:%M"),
    "Result": "rolled_back",
})
lab.state["deploy"]["CurrentVersion"] = "v2026.11.18-3"
lab._save()
print(lab.curl(f"http://127.0.0.1:{expected}/healthz").splitlines()[-1])
""",
        "checks": [
            ("期望端口已监听", 'f":{expected}" in lab.sh("ss -lntp")'),
            ("健康检查通过", "'HTTP_CODE=200' in lab.curl(f'http://127.0.0.1:{expected}/healthz')"),
            ("发布已回滚", "lab.state['deploy']['Status'] == 'rolled_back'"),
        ],
        "auto_setup": "lab.reset(start_api=True)",
        "auto_script": "deploy_gate.py",
        "auto_md": "脚本执行端口/健康/冒烟三层门禁，失败时按上一版本配置回滚并重启，"
                   "回滚后再次冒烟通过才返回成功。",
        "questions": "1. 为什么“进程存活”不能作为发布成功判据？\n"
                     "2. 回滚与前滚修复分别适用什么场景？\n"
                     "3. 如何设计灰度发布，把配置错误限制在 10% 流量？",
    },
    {
        "id": "case-12-alert",
        "n": "12",
        "title": "日志采集断流 + 告警规则失配，告警风暴",
        "scenario": "压测时把 CPU 告警规则阈值改成 1%、取消持续与静默且未恢复，叠加 Logtail 停止，"
                    "10 分钟内产生 240 条重复告警，真实错误日志同时断流。",
        "bullets": "- 用告警值判断“规则坏了”而非“业务坏了”\n"
                   "- 恢复 Logtail 采集并确认延迟回落\n"
                   "- 按规则维度去重并恢复阈值/For/静默/分组\n"
                   "- 用 `alert_dedup.py` 完成自动化处置",
        "setup_extra": "lab.start_logagent()",
        "inject": """\
lab.inject_alert_storm()
print("已注入：告警数 =", len(lab.state["alarms"]))
""",
        "observe": [
            ("采集组件", 'print(lab.sh("systemctl status logtail"))'),
            ("告警规则", 'print(lab.aliyun("cms DescribeMetricRuleList"))'),
            ("告警历史", 'print(lab.aliyun("cms DescribeAlarmHistory"))'),
            ("SLS 采集状态", 'print(lab.aliyun("sls GetLogStore --Project xhs-prod --LogStore note-api"))'),
        ],
        "diagnose": """\
rule = lab.state["alarm_rules"][0]
print("告警总数:", len(lab.state["alarms"]))
print("规则:", {k: rule[k] for k in ("Threshold", "Period", "For", "Silence")})
print("采集:", lab.state["logstore"])
print("判断：告警值远低于业务水位 + 规则阈值 1% → 规则被改坏，先降噪再查采集")
""",
        "manual": """\
# 1) 恢复日志采集
lab.start_logagent()

# 2) 按 10 分钟窗口去重
seen, kept = set(), []
for a in lab.state["alarms"]:
    key = (a["RuleName"], a["Time"][:15])
    if key in seen:
        continue
    seen.add(key)
    kept.append(a)
lab.state["alarms"] = kept

# 3) 规则恢复基线并开启分组
rule = lab.state["alarm_rules"][0]
rule.update({"Threshold": 80, "Period": 60, "For": 180, "Silence": 600, "GroupBy": ["instanceId"]})
lab.state["logstore"].update({"Status": "normal", "IngestLagSec": 2})
lab.state["faults"].pop("alert", None)
lab._save()
time.sleep(2)
print("告警数:", len(kept), "| 采集 lag:", lab.logagent_stats().get("lag_bytes"), "bytes")
""",
        "checks": [
            ("告警已收敛", "len(lab.state['alarms']) <= 5"),
            ("采集延迟恢复", "lab.logagent_stats().get('lag_bytes', 10**9) < 2000"),
            ("规则阈值恢复", "lab.state['alarm_rules'][0]['Threshold'] == 80"),
        ],
        "auto_setup": "lab.reset(start_api=True)\nlab.start_logagent()",
        "auto_script": "alert_dedup.py",
        "auto_md": "脚本按规则聚合告警，恢复 Logtail，按时间窗口去重，并把阈值/For/静默/分组恢复为基线，"
                   "最后校验告警数与采集延迟。",
        "questions": "1. 告警风暴时如何在不丢真实告警的前提下快速降噪？\n"
                     "2. 监控组件自身需要哪些自监控指标？\n"
                     "3. 如何让压测与生产告警规则互不污染？",
    },
]


def build(case: dict) -> Path:
    nb = nbf.v4.new_notebook()
    nb.metadata.update({
        "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
        "language_info": {"name": "python"},
    })
    cells = []

    cells.append(nbf.v4.new_markdown_cell(
        f"# 案例 {case['n']} · {case['title']}\n\n" + HEADER.format(
            scenario=case["scenario"], bullets=case["bullets"])))

    cells.append(nbf.v4.new_code_cell(SETUP.format(
        case_id=case["id"], setup_extra=case["setup_extra"])))

    cells.append(nbf.v4.new_markdown_cell(
        "## 步骤 1 · 异常重现\n\n下面注入一个可复现的真实故障，观察它对业务的影响。"))
    cells.append(nbf.v4.new_code_cell(case["inject"]))

    cells.append(nbf.v4.new_markdown_cell(
        "## 步骤 2 · 取证\n\n按“入口 → 主机 → 进程 → 依赖 → 日志”的分层顺序收集证据，"
        "不要一上来就重启。"))
    for label, code in case["observe"]:
        cells.append(nbf.v4.new_markdown_cell(f"**{label}**"))
        cells.append(nbf.v4.new_code_cell(code))

    cells.append(nbf.v4.new_markdown_cell("## 步骤 3 · 判断\n\n把证据收敛成一句结论，决定止血动作。"))
    cells.append(nbf.v4.new_code_cell(case["diagnose"]))

    cells.append(nbf.v4.new_markdown_cell(
        "## 步骤 4 · 手工处置（先止血）\n\n执行最小必要的变更，优先恢复业务，再考虑根治。"))
    cells.append(nbf.v4.new_code_cell(case["manual"]))

    cells.append(nbf.v4.new_markdown_cell("## 步骤 5 · 验证\n\n用可执行的断言确认恢复，而不是“看起来好了”。"))
    cells.append(nbf.v4.new_code_cell("\n".join(
        f"lab.check({label!r}, bool({expr}))" for label, expr in case["checks"])))

    cells.append(nbf.v4.new_markdown_cell(
        "## 步骤 6 · 自动化沉淀\n\n" + case["auto_md"] + "\n\n下面重置沙箱、重新注入故障，"
        "然后直接调用生产可用的自动化脚本（`--apply`；缺省为 dry-run）。"))
    cells.append(nbf.v4.new_code_cell(
        case["auto_setup"] + "\n" + case["inject"].split("print(")[0].rstrip() +
        f"\nout = lab.auto('{case['auto_script']}', '--apply')\nprint(out)"))
    cells.append(nbf.v4.new_code_cell(
        "lab.check('自动化处置返回 REMEDIATED', 'RESULT: REMEDIATED' in out)\n"
        "print('\\n自动化脚本已可作为 cron / 事件总线触发器复用')"))

    cells.append(nbf.v4.new_markdown_cell(FOOTER.format(questions=case["questions"])))
    cells.append(nbf.v4.new_code_cell("lab.stop_all()\nprint('沙箱已停止')"))

    nb.cells = cells
    out_dir = LABS / case["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "lab.ipynb"
    nbf.write(nb, path)
    return path


def main() -> None:
    paths = [build(c) for c in CASES]
    for p in paths:
        print("built", p.relative_to(REPO))
    print(f"共 {len(paths)} 个 Notebook")


if __name__ == "__main__":
    sys.exit(main())
