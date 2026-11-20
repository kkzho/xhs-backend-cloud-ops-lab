"""小红书后端云运维演练沙箱（核心框架）。

设计原则
--------
1. **真实可复现**：CPU 打满、内存增长、FD 泄漏、TLS 过期、502、端口未监听等
   故障由真实进程/真实 socket 复现，不是打印假日志。
2. **云侧模拟**：没有阿里云账号也能练。ECS/SLB/RDS/Redis/CMS/SLS 的
   OpenAPI 返回值由 `state.json` 渲染，命令、字段与线上一致（标注 MOCK）。
3. **只动沙箱**：所有文件写入 `labs/.runtime/<case>/`，不触碰宿主机系统配置。

Notebook 中的典型用法：
    from common.xhs_lab import Lab
    lab = Lab("case-01-cpu-runaway")
    lab.reset()
    lab.inject_cpu_runaway()
    print(lab.sh("ps aux --sort=-%cpu | head -6"))
    print(lab.aliyun("cms QueryMetricList --MetricName cpu_total"))
    print(lab.auto("cpu_guard.py", "--apply"))
    lab.assert_service_healthy()
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

LABS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = LABS_DIR.parent
RUNTIME_DIR = LABS_DIR / ".runtime"
PY = sys.executable


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _http(url: str, timeout: float = 2.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except OSError as e:
        return 0, str(e)


def _json_get(url: str, timeout: float = 2.0) -> dict:
    code, body = _http(url, timeout)
    try:
        return json.loads(body)
    except Exception:
        return {"_status": code, "_body": body}


class Lab:
    """一个案例 = 一台/一组沙箱资源 + 一份可持久化的 state.json。"""

    def __init__(self, case_id: str, quota_mb: int = 128, inode_limit: int = 20000):
        self.case_id = case_id
        self.quota_mb = quota_mb
        self.inode_limit = inode_limit
        self.dir = RUNTIME_DIR / case_id
        self.fs = self.dir / "fs"
        self.data = self.fs / "data"
        self.logdir = self.fs / "var/log/xhs"
        idx = int(case_id.split("-")[1]) if case_id.split("-")[1].isdigit() else 1
        base = 21000 + idx * 20
        self.ports = {
            "api": base, "slb": base + 1, "mysql": base + 2, "mysql_ctrl": base + 3,
            "redis": base + 4, "logagent": base + 5, "https": base + 6,
        }
        self.state: dict = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._threads: list[threading.Thread] = []
        if (self.dir / "state.json").exists():
            self.state = json.loads((self.dir / "state.json").read_text("utf-8"))

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.ports['api']}"

    @property
    def slb(self) -> str:
        return f"http://127.0.0.1:{self.ports['slb']}"

    # ---------------- 生命周期 ---------------- #
    def reset(self, start_api: bool = True, start_slb: bool = False) -> "Lab":
        self.stop_all()
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        for d in (self.data / "logs", self.data / "sessions", self.logdir,
                  self.fs / "etc/xhs", self.fs / "etc/systemd/system", self.fs / "etc/cron.d"):
            d.mkdir(parents=True, exist_ok=True)
        self.state = self._seed_state()
        self._write_unit_files()
        self._save()
        if start_api:
            self.start_api()
        if start_slb:
            self.start_slb()
        return self

    def _seed_state(self) -> dict:
        return {
            "meta": {"app": "xiaohongshu", "env": "prod", "region": "cn-hangzhou",
                     "vpc": "vpc-bp1xhsprod", "mock": True, "generated_at": time.strftime("%F %T")},
            "ecs": [
                {"InstanceId": "i-bp1xhsapi01", "InstanceName": "xhs-note-api-01",
                 "PrivateIpAddress": "10.10.1.11", "EipAddress": "47.98.31.21",
                 "Status": "Running", "InstanceType": "ecs.g7.2xlarge", "ZoneId": "cn-hangzhou-h",
                 "Cpu": 8, "Memory": 32},
                {"InstanceId": "i-bp1xhsapi02", "InstanceName": "xhs-note-api-02",
                 "PrivateIpAddress": "10.10.1.12", "EipAddress": "47.98.31.22",
                 "Status": "Running", "InstanceType": "ecs.g7.2xlarge", "ZoneId": "cn-hangzhou-i",
                 "Cpu": 8, "Memory": 32},
            ],
            "slb": {
                "LoadBalancerId": "lb-bp1xhsslb01", "LoadBalancerName": "xhs-api-slb",
                "Address": "47.98.31.20", "AddressType": "internet",
                "Listeners": [{"ListenerPort": 443, "Protocol": "HTTPS"}, {"ListenerPort": 80, "Protocol": "HTTP"}],
                "BackendServers": [{"ServerId": "i-bp1xhsapi01", "Port": self.ports["api"], "Weight": 100,
                                    "HealthStatus": "normal"}],
                "HealthCheck": {"Path": "/healthz", "Interval": 2, "Timeout": 1,
                                "HealthyThreshold": 2, "UnhealthyThreshold": 2},
            },
            "rds": {"DBInstanceId": "rm-bp1xhsfeed01", "Engine": "MySQL 8.0", "Class": "rds.mysql.c1.xlarge",
                    "MaxConnections": 50, "CurrentConnections": 12, "SlowQueries": 3,
                    "ConnectionUsage": 24},
            "redis": {"InstanceId": "r-bp1xhsfeed01", "Class": "redis.master.small.default",
                      "MaxMemoryMB": 32, "UsedMemoryMB": 8, "Policy": "noeviction",
                      "HitRate": 0.98, "EvictedKeys": 0},
            "security_groups": [{
                "SecurityGroupId": "sg-bp1xhsapi", "SecurityGroupName": "xhs-api-sg",
                "Rules": [{"Direction": "ingress", "IpProtocol": "tcp", "PortRange": f"{self.ports['api']}/{self.ports['api']}",
                           "SourceCidrIp": "10.10.0.0/16", "Description": "api from vpc"}],
            }],
            "certs": [{"CertId": "cert-xhs-api", "Domain": "api.xiaohongshu-lab.com",
                       "NotAfter": "2026-12-31", "Status": "valid"}],
            "alarms": [],
            "alarm_rules": [{
                "RuleId": "rule-api-cpu", "Name": "note-api CPU使用率", "Metric": "cpu_total",
                "Threshold": 80, "Period": 60, "For": 180, "Silence": 600, "Notify": ["oncall"],
                "Enabled": True,
            }],
            "logstore": {"Project": "xhs-prod", "LogStore": "note-api",
                         "IngestLagSec": 1, "Status": "normal"},
            "deploy": {"CurrentVersion": "v2026.11.18-3", "Status": "success",
                       "History": [{"Version": "v2026.11.18-3", "Time": "2026-11-18 21:03", "Result": "success"}]},
            "services": {},
            "faults": {},
            "disk": {"Mount": "/data", "SizeMB": self.quota_mb, "InodeLimit": self.inode_limit},
        }

    def _write_unit_files(self) -> None:
        api = self.ports["api"]
        (self.fs / "etc/xhs/note-api.conf").write_text(
            "# xhs note-api 应用配置（沙箱）\n"
            f"listen_host = 127.0.0.1\nlisten_port = {api}\n"
            "worker_processes = 4\nworker_connections = 1024\n"
            f"upstream_feed = 127.0.0.1:{self.ports['mysql']}\n"
            "max_open_files = 256\n", "utf-8")
        (self.fs / "etc/systemd/system/xhs-note-api.service").write_text(
            "[Unit]\nDescription=XHS note-api service\nAfter=network.target\n\n"
            "[Service]\nType=simple\nUser=xhs\n"
            f"Environment=PORT={api}\nEnvironment=XHS_LOG={self.logdir}/note-api.log\n"
            "LimitNOFILE=256\nRestart=always\nRestartSec=2\n"
            f"ExecStart=/usr/bin/python3 -m labs.common.sims.service {api} 127.0.0.1\n\n"
            "[Install]\nWantedBy=multi-user.target\n", "utf-8")
        (self.fs / "etc/cron.d/xhs-ranking").write_text(
            "# 榜单预计算任务\n*/1 * * * * xhs cd /srv/xhs && python3 jobs/ranking_precompute.py\n", "utf-8")
        (self.fs / "etc/systemd/system/logtail.service").write_text(
            "[Unit]\nDescription=Logtail log collector\n\n[Service]\n"
            "Restart=always\nRestartSec=1\n"
            f"ExecStart=/usr/local/bin/logtail --watch {self.logdir}/note-api.log\n", "utf-8")
        (self.logdir / "note-api.log").touch()

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.dir / "state.json")

    def _pidfile(self) -> Path:
        return self.dir / "pids.json"

    # ---------------- 进程管理 ---------------- #
    def _spawn(self, name: str, module_args: list[str], log_name: str | None = None,
               nofile: int | None = None) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=str(REPO_DIR), PYTHONUNBUFFERED="1")
        log_path = self.logdir / (log_name or f"{name}.log")
        fh = open(log_path, "a", encoding="utf-8")
        pre = None
        if nofile:
            def pre():  # noqa: E306  （子进程内限制 FD 上限，复现 too many open files）
                import resource
                resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        p = subprocess.Popen([PY, *module_args], cwd=str(REPO_DIR), env=env,
                             stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=pre)
        self._procs[name] = p
        pids = json.loads(self._pidfile().read_text()) if self._pidfile().exists() else {}
        pids[name] = p.pid
        self._pidfile().write_text(json.dumps(pids))
        return p

    def start_api(self, port: int | None = None, tls: bool = False, nofile: int = 256) -> None:
        port = port or self.ports["api"]
        if tls:
            cert = self.dir / "tls/server.crt"
            key = self.dir / "tls/server.key"
            self._spawn("api", ["-m", "labs.common.sims.https_service", str(port), "127.0.0.1",
                                str(cert), str(key)], "note-api.log", nofile=nofile)
        else:
            self._spawn("api", ["-m", "labs.common.sims.service", str(port), "127.0.0.1"],
                        "note-api.log", nofile=nofile)
        self._wait_port(port, 8)
        prev = self.state.get("services", {}).get("xhs-note-api", {})
        self.state.setdefault("services", {})["xhs-note-api"] = {
            "name": "xhs-note-api", "port": port, "pid": self._procs["api"].pid,
            "status": "active", "restarts": prev.get("restarts", 0), "nofile": nofile,
        }
        self._save()

    def kill_service(self, unit: str = "xhs-note-api") -> int | None:
        """按 state 中的 pid 结束服务（供独立进程的自动化脚本使用）。"""
        svc = self.state.get("services", {}).get(unit, {})
        pid = svc.get("pid")
        if not pid:
            return None
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.4)
        return pid

    def restart_api(self, port: int | None = None, tls: bool = False, nofile: int = 256) -> None:
        self.kill_service()
        self.start_api(port=port, tls=tls, nofile=nofile)

    def start_slb(self, backend_port: int | None = None) -> None:
        backend = backend_port or self.state["services"].get("xhs-note-api", {}).get("port", self.ports["api"])
        self._spawn("slb", ["-m", "labs.common.sims.slb", str(self.ports["slb"]),
                            f"127.0.0.1:{backend}", str(self.logdir / "slb.log")], "slb.log")
        self._wait_port(self.ports["slb"], 5)

    def start_mysql(self, max_conn: int = 50) -> None:
        self._spawn("mysql", ["-m", "labs.common.sims.mysql_lite", str(self.ports["mysql"]),
                              str(self.ports["mysql_ctrl"]), str(max_conn)], "mysql.log")
        self._wait_port(self.ports["mysql"], 5)

    def start_redis(self, maxmemory_mb: int = 32) -> None:
        self._spawn("redis", ["-m", "labs.common.sims.redis_lite", str(self.ports["redis"]),
                              str(maxmemory_mb)], "redis.log")
        self._wait_port(self.ports["redis"], 5)

    def start_logagent(self) -> None:
        self._spawn("logagent", ["-m", "labs.common.sims.logagent",
                                 str(self.logdir / "note-api.log"),
                                 str(self.dir / "logtail_offset.json"),
                                 str(self.ports["logagent"])], "logtail.log")
        self._wait_port(self.ports["logagent"], 5)
        self.state["logstore"]["Status"] = "normal"
        self.state.setdefault("services", {})["logtail"] = {
            "name": "logtail", "port": self.ports["logagent"], "pid": self._procs["logagent"].pid,
            "status": "active", "restarts": 0, "nofile": 1024,
        }
        self._save()

    def stop(self, name: str) -> None:
        p = self._procs.get(name)
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except OSError:
                p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    p.kill()

    def stop_all(self) -> None:
        pids = {}
        if self._pidfile().exists():
            try:
                pids = json.loads(self._pidfile().read_text())
            except Exception:
                pids = {}
        for pid in pids.values():
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        for p in list(self._procs.values()):
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    p.kill()
        self._procs.clear()
        self._threads.clear()
        for s in getattr(self, "_leak_socks", []):
            try:
                s.close()
            except OSError:
                pass
        self._leak_socks = []

    def _alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            return psutil.Process(pid).is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    # ---------------- 等待 / 探测 ---------------- #
    def _wait_port(self, port: int, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
            time.sleep(0.15)
        return False

    def wait(self, seconds: float = 1.0) -> None:
        time.sleep(seconds)

    def wait_until(self, fn, timeout: float = 20.0, interval: float = 0.5) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(interval)
        return False

    def api_stats(self) -> dict:
        return _json_get(f"{self.api}/__stats")

    def slb_stats(self) -> dict:
        return _json_get(f"{self.slb}/__stats")

    def mysql_stats(self) -> dict:
        return _json_get(f"http://127.0.0.1:{self.ports['mysql_ctrl']}/stats")

    def redis(self, command: str) -> str:
        import urllib.parse
        code, body = _http(f"http://127.0.0.1:{self.ports['redis']}/cmd?c={urllib.parse.quote(command)}")
        return body

    def logagent_stats(self) -> dict:
        return _json_get(f"http://127.0.0.1:{self.ports['logagent']}/")

    # ---------------- 虚拟文件系统 ---------------- #
    def path(self, p: str) -> Path:
        p = str(p)
        if p.startswith("/data"):
            return self.data / p[len("/data"):].lstrip("/")
        if p.startswith("/var/log"):
            return self.fs / p.lstrip("/")
        if p.startswith("/etc") or p.startswith("/srv"):
            return self.fs / p.lstrip("/")
        return self.dir / p

    def write(self, path: str, content: str) -> Path:
        f = self.path(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, "utf-8")
        return f

    def disk_usage(self) -> tuple[float, int]:
        used = files = 0
        for root, _dirs, names in os.walk(self.data):
            for n in names:
                try:
                    used += (Path(root) / n).stat().st_size
                    files += 1
                except OSError:
                    pass
        return used / 1024 / 1024, files

    def write_data(self, name: str, size_mb: float) -> Path:
        """按配额写入数据文件；超过沙箱卷容量抛 ENOSPC（模拟云盘写满）。"""
        used, _ = self.disk_usage()
        if used + size_mb > self.quota_mb:
            raise OSError(28, "No space left on device", str(self.path(f"/data/{name}")))
        f = self.path(f"/data/{name}")
        f.parent.mkdir(parents=True, exist_ok=True)
        chunk = b"0" * (1024 * 1024)
        with open(f, "wb") as fh:
            for _ in range(int(size_mb)):
                fh.write(chunk)
        return f

    # ---------------- 命令路由（模拟 shell） ---------------- #
    def sh(self, cmd: str, timeout: float = 30) -> str:
        cmd = cmd.strip()
        try:
            return self._route(cmd, timeout)
        except Exception as exc:  # 保证 notebook 不中断，暴露真实错误
            return f"[lab] command routing error: {exc}"

    def run(self, cmd: str, timeout: float = 30) -> tuple[int, str]:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()

    def _route(self, cmd: str, timeout: float) -> str:
        low = cmd.lower()
        if low.startswith("aliyun "):
            from . import aliyun_sim
            return aliyun_sim.render(self.state, cmd, self._save)
        if low.startswith("df -i"):
            _used, files = self.disk_usage()
            pct = files / self.inode_limit * 100
            return ("Filesystem      Inodes  IUsed   IFree IUse% Mounted on\n"
                    f"/dev/vdb        {self.inode_limit:>6}  {files:>5}  {max(self.inode_limit - files, 0):>5}  {pct:>3.0f}% /data")
        if low.startswith("df"):
            used, _ = self.disk_usage()
            avail = max(self.quota_mb - used, 0)
            pct = used / self.quota_mb * 100
            return ("Filesystem      Size  Used Avail Use% Mounted on\n"
                    f"/dev/vdb        {self.quota_mb}M  {used:.0f}M  {avail:.0f}M  {pct:>3.0f}% /data")
        if low.startswith("du "):
            return self._du(cmd)
        if low.startswith("find "):
            return self._find(cmd)
        if low.startswith("systemctl "):
            return self._systemctl(cmd)
        if low.startswith("journalctl"):
            return self._journalctl(cmd)
        if low.startswith("redis-cli"):
            return self._redis_cli(cmd)
        if low.startswith("mysql"):
            return self._mysql_cli(cmd)
        if low.startswith("ss ") or low == "ss":
            return self._ss()
        if low.startswith("ps ") or low == "ps":
            return self._ps(cmd)
        if low.startswith("top"):
            return self._top()
        if low.startswith("lsof"):
            return self._lsof(cmd)
        if low.startswith("ulimit"):
            svc = self.state.get("services", {}).get("xhs-note-api", {})
            return str(svc.get("nofile", 256))
        if low.startswith("cat ") or low.startswith("tail ") or low.startswith("head ") or low.startswith("ls"):
            return self._file_cmd(cmd)
        if low.startswith("dig ") or low.startswith("nslookup"):
            return self._dns(cmd)
        if low.startswith("ping "):
            return self._ping(cmd)
        if low.startswith("tcpdump"):
            return self._tcpdump(cmd)
        if low.startswith("date"):
            return time.strftime("%a %b %e %T %Z %Y")
        if low.startswith("uptime"):
            return f" {time.strftime('%H:%M:%S')} up 128 days,  3:21,  2 users,  load average: 3.42, 2.87, 1.95"
        code, out = self.run(cmd, timeout)
        return out or f"(exit {code})"

    def aliyun(self, cmd: str) -> str:
        """执行阿里云 OpenAPI（沙箱模拟），例：lab.aliyun("ecs DescribeInstances")"""
        return self.sh(cmd if cmd.startswith("aliyun ") else f"aliyun {cmd}")

    def _du(self, cmd: str) -> str:
        target = cmd.split()[-1].rstrip("/")
        if target.endswith("/*"):
            base = self.path(target[:-2])
            children = sorted(base.iterdir()) if base.exists() else []
            if not children:
                return f"du: cannot access '{target}': No such file or directory"
            lines = []
            for child in children:
                size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) if child.is_dir() else child.stat().st_size
                lines.append(f"{size / 1024 / 1024:.1f}M\t{target[:-2].rstrip('/')}/{child.name}")
            return "\n".join(lines)
        base = self.path(target)
        if not base.exists():
            return f"du: cannot access '{target}': No such file or directory"
        lines = []
        if base.is_dir():
            for child in sorted(base.iterdir()):
                size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) if child.is_dir() else child.stat().st_size
                lines.append(f"{size / 1024 / 1024:.1f}M\t{target.rstrip('/')}/{child.name}")
            total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
            lines.append(f"{total / 1024 / 1024:.1f}M\t{target}")
        else:
            lines.append(f"{base.stat().st_size / 1024:.1f}K\t{target}")
        return "\n".join(lines)

    def _find(self, cmd: str) -> str:
        parts = cmd.split()
        target = self.path(parts[1])
        type_f = "-type" in parts and "f" in parts
        count = 0
        biggest = []
        for root, _dirs, names in os.walk(target):
            for n in names:
                f = Path(root) / n
                if type_f and not f.is_file():
                    continue
                count += 1
                if len(biggest) < 5:
                    biggest.append(f)
        if "wc -l" in cmd:
            return str(count)
        biggest.sort(key=lambda f: f.stat().st_size, reverse=True)
        return "\n".join(f"{f} ({f.stat().st_size} bytes)" for f in biggest)

    def _systemctl(self, cmd: str) -> str:
        parts = cmd.split()
        verb = parts[1] if len(parts) > 1 else "status"
        unit = parts[2].replace(".service", "") if len(parts) > 2 else "xhs-note-api"
        svc = self.state.get("services", {}).get(unit)
        if not svc:
            return f"Unit {unit}.service could not be found."
        if verb == "status":
            alive = self._alive(svc.get("pid"))
            if unit == "logtail":
                alive = self._alive(self._procs.get("logagent").pid if self._procs.get("logagent") else
                                    self.state.get("services", {}).get("logtail", {}).get("pid"))
            state_txt = "active (running)" if alive else "failed"
            notes = "日志采集 Agent" if unit == "logtail" else "XHS note-api service"
            return (f"● {unit}.service - {notes}\n"
                    f"   Loaded: loaded (/etc/systemd/system/{unit}.service; enabled)\n"
                    f"   Active: {state_txt} since {time.strftime('%Y-%m-%d %H:%M:%S')}; 12s ago\n"
                    f" Main PID: {svc.get('pid')} (python3)\n"
                    f"   Memory: {self.api_stats().get('rss_mb', '-')}M\n"
                    f"   Restarts: {svc.get('restarts', 0)}\n"
                    f"   LimitNOFILE: {svc.get('nofile', 256)}")
        if verb == "cat":
            f = self.fs / f"etc/systemd/system/{unit}.service"
            return f.read_text() if f.exists() else "No such unit"
        if verb in ("is-active",):
            alive = self._procs.get("api") and self._procs["api"].poll() is None
            return "active" if alive else "inactive"
        if verb in ("restart", "start", "stop"):
            if unit == "logtail":
                if verb == "stop":
                    self.stop("logagent")
                    self.state["logstore"]["Status"] = "agent_stopped"
                else:
                    self.start_logagent()
                self._save()
                return ""
            if verb == "restart":
                self.stop("api")
                time.sleep(0.2)
                svc["restarts"] = svc.get("restarts", 0) + 1
                self.start_api(port=svc.get("port", self.ports["api"]))
            elif verb == "stop":
                self.stop("api")
            elif verb == "start":
                self.start_api(port=svc.get("port", self.ports["api"]))
            return ""
        if verb == "daemon-reload":
            return ""
        return f"Unknown operation '{verb}'"

    def _journalctl(self, cmd: str) -> str:
        unit = "xhs-note-api"
        n = 20
        if "-u" in cmd.split():
            unit = cmd.split()[cmd.split().index("-u") + 1].replace(".service", "")
        if "-n" in cmd.split():
            try:
                n = int(cmd.split()[cmd.split().index("-n") + 1])
            except (ValueError, IndexError):
                n = 20
        f = self.logdir / ("note-api.log" if unit != "logtail" else "logtail.log")
        if not f.exists():
            return f"-- No entries --"
        lines = f.read_text("utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:]) or "-- No entries --"

    def _redis_cli(self, cmd: str) -> str:
        body = cmd[len("redis-cli"):].strip()
        if not body:
            return "(error) ERR wrong number of arguments"
        return self.redis(body)

    def _mysql_cli(self, cmd: str) -> str:
        import urllib.parse
        q = ""
        if "-e" in cmd.split():
            idx = cmd.split().index("-e")
            q = " ".join(cmd.split()[idx + 1:]).strip("'\"")
        elif "SHOW" in cmd.upper() or "show" in cmd:
            q = cmd.split("'")[1] if "'" in cmd else cmd
        if not q:
            return "(empty query)"
        code, body = _http(f"http://127.0.0.1:{self.ports['mysql_ctrl']}/sql?q={urllib.parse.quote(q)}")
        try:
            data = json.loads(body)
        except Exception:
            return body
        rows = data.get("rows")
        if rows is None:
            return json.dumps(data, ensure_ascii=False, indent=2)
        if not rows:
            return "Empty set"
        cols = list(rows[0].keys())
        widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
        out = ["  ".join(c.ljust(widths[c]) for c in cols),
               "  ".join("-" * widths[c] for c in cols)]
        out += ["  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) for r in rows]
        out.append(f"{len(rows)} rows in set (0.00 sec)")
        return "\n".join(out)

    def _ss(self) -> str:
        """ss 等价输出：基于 lsof 监听列表（无 root 也可用），失败时回退端口探测。"""
        rows = ["State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process"]
        seen = set()
        code, out = self.run("lsof -nP -iTCP -sTCP:LISTEN", timeout=10)
        if code == 0:
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 9:
                    continue
                name, pid = parts[0], parts[1]
                addr = parts[8]
                if addr in seen:
                    continue
                seen.add(addr)
                rows.append(f"LISTEN 0      128    {addr:<22} 0.0.0.0:*  users:((\"{name}\",pid={pid}))")
        if len(rows) == 1:
            for name, port in self.ports.items():
                with socket.socket() as s:
                    s.settimeout(0.2)
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        rows.append(f"LISTEN 0      128    127.0.0.1:{port:<10} 0.0.0.0:*  users:((\"{name}\"))")
        return "\n".join(rows)

    def _cpu_snapshot(self) -> dict[int, float]:
        snap = {}
        for p in psutil.process_iter(["pid"]):
            try:
                t = p.cpu_times()
                snap[p.pid] = t.user + t.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return snap

    def _ps(self, cmd: str) -> str:
        first = self._cpu_snapshot()
        time.sleep(0.4)
        second = self._cpu_snapshot()
        total_mem = psutil.virtual_memory().total / 1024 / 1024
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline", "username"]):
            try:
                delta = second.get(p.pid, 0) - first.get(p.pid, 0)
                cpu = max(delta / 0.4 * 100, 0)
                rss = (p.info["memory_info"].rss / 1024 / 1024) if p.info["memory_info"] else 0
                mem_pct = rss / total_mem * 100
                cmdline = " ".join(p.info["cmdline"] or [])[:100] or p.info["name"]
                rows.append((p.pid, p.info["username"], cpu, mem_pct, cmdline))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: -r[2])
        header = "USER       PID  %CPU  %MEM  COMMAND"
        lines = [header] + [f"{u[:8]:<9} {pid:>5} {cpu:>5.1f} {mem:>5.1f}  {cmd}" for pid, u, cpu, mem, cmd in rows[:12]]
        return "\n".join(lines)

    def _top(self) -> str:
        out = ["top - " + time.strftime("%H:%M:%S") + " up 128 days,  3:21,  load average: 3.42, 2.87, 1.95",
               "Tasks: 214 total,   1 running, 213 sleeping", "%Cpu(s): 96.3 us,  2.1 sy,  0.0 ni,  1.6 id",
               "MiB Mem :  32105.0 total,   1024.3 free,  18234.6 used,  12846.1 buff/cache", "",
               "   PID USER      PR  NI    VIRT    RES  %CPU  %MEM     TIME+ COMMAND"]
        if self._procs.get("api"):
            out.append(f"{self._procs['api'].pid:>6} xhs       20   0  512304 182340  96.3   0.6   1:23.41 python3")
        for pid in self.state.get("faults", {}).get("cpu", {}).get("pids", []):
            out.append(f"{pid:>6} xhs       20   0  102216  12408  93.7   0.0   0:48.12 python3")
        return "\n".join(out)

    def _lsof(self, cmd: str) -> str:
        target_pid = None
        for token in cmd.split():
            if token.isdigit():
                target_pid = int(token)
        if "+L1" in cmd:
            svc = self.state.get("services", {}).get("xhs-note-api", {})
            lines = ["COMMAND    PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME"]
            for f in self.state.get("faults", {}).get("disk", {}).get("deleted_open", []):
                lines.append(f"python3  {svc.get('pid')} xhs    7w   REG  253,1 {f['size']:>8}    0 {f['name']} (deleted)")
            return "\n".join(lines)
        if target_pid is None:
            target_pid = self.state.get("services", {}).get("xhs-note-api", {}).get("pid")
        try:
            p = psutil.Process(target_pid)
            lines = [f"COMMAND    PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME"]
            files = p.open_files()
            conns = p.net_connections()
            for i, f in enumerate(files[:40]):
                lines.append(f"{p.name()[:9]:<10} {p.pid} xhs    {i + 3}r   REG  253,1  1024 0 {f.path}")
            for c in conns[:40]:
                lines.append(f"{p.name()[:9]:<10} {p.pid} xhs    9u  IPv4 0x8f2a      0t0 TCP {c.laddr.ip}:{c.laddr.port}->{c.raddr.ip if c.raddr else '*'}:{c.raddr.port if c.raddr else '*'}")
            return "\n".join(lines)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            return f"lsof: {exc}"

    def _file_cmd(self, cmd: str) -> str:
        parts = cmd.split()
        op = parts[0]
        if op == "ls":
            target = self.path(parts[-1]) if len(parts) > 1 and not parts[-1].startswith("-") else self.data
            if not target.exists():
                return f"ls: {parts[-1]}: No such file or directory"
            items = sorted(target.iterdir())
            return "\n".join(f"{'d' if i.is_dir() else '-'}rw-r--r-- 1 xhs xhs {i.stat().st_size:>10} {i.name}" for i in items) or "(empty)"
        target = self.path(parts[-1])
        if not target.exists():
            return f"{op}: {parts[-1]}: No such file or directory"
        lines = target.read_text("utf-8", errors="ignore").splitlines()
        if op == "cat":
            return "\n".join(lines)
        n = 10
        if "-n" in parts:
            try:
                n = int(parts[parts.index("-n") + 1])
            except (ValueError, IndexError):
                n = 10
        return "\n".join(lines[-n:] if op == "tail" else lines[:n])

    def _dns(self, cmd: str) -> str:
        domain = cmd.split()[-1]
        return (";; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 42117\n"
                f";{domain}. 60 IN A\n\n;; ANSWER SECTION:\n(empty)")

    def _ping(self, cmd: str) -> str:
        return ("PING 10.10.1.12 (10.10.1.12): 56 data bytes\n"
                "Request timeout for icmp_seq 0\nRequest timeout for icmp_seq 1\n"
                "--- 10.10.1.12 ping statistics ---\n3 packets transmitted, 0 packets received, 100.0% packet loss")

    def _tcpdump(self, cmd: str) -> str:
        return ("14:22:31.204185 IP 10.10.1.11.52134 > 10.10.1.12.3306: Flags [S], seq 0, win 64240, options [mss 1460], length 0\n"
                "14:22:32.207118 IP 10.10.1.11.52134 > 10.10.1.12.3306: Flags [S], seq 0, win 64240, options [mss 1460], length 0\n"
                "3 packets captured\n3 packets received by filter")

    def tcp_probe(self, ip: str, port: int, timeout: float = 3.0) -> dict:
        """按安全组规则裁决连通性（云网络策略模拟器），返回 open/timeout/refused。"""
        allowed = False
        for sg in self.state.get("security_groups", []):
            for r in sg.get("Rules", []):
                if r.get("Direction") != "ingress":
                    continue
                pr = r.get("PortRange", "")
                lo, _, hi = pr.partition("/")
                try:
                    if not (int(lo) <= port <= int(hi or lo)):
                        continue
                except ValueError:
                    continue
                cidr = r.get("SourceCidrIp", "")
                if cidr.startswith("10.10.") or cidr == "0.0.0.0/0":
                    allowed = True
        if not allowed:
            time.sleep(min(timeout, 1.0))
            return {"result": "timeout", "detail": "安全组未放行，SYN 无响应"}
        # 策略放通后，探测沙箱内等价实例（127.0.0.1）的业务端口
        with socket.socket() as s:
            s.settimeout(1.0)
            rc = s.connect_ex(("127.0.0.1", port))
        return {"result": "open" if rc == 0 else "refused", "detail": f"connect_ex={rc}"}

    def curl(self, url: str, extra: str = "") -> str:
        cmd = f"curl -sS -m 8 -w '\\nHTTP_CODE=%{{http_code}}' {extra} {url}"
        code, out = self.run(cmd)
        return out

    # ---------------- 故障注入 ---------------- #
    def inject_cpu_runaway(self, procs: int = 4) -> None:
        pids = []
        for _ in range(procs):
            p = subprocess.Popen([PY, "-c", "while True: pass"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids.append(p.pid)
        self.state.setdefault("faults", {})["cpu"] = {
            "pids": pids, "job": "ranking_precompute.py", "cpu": 96,
            "since": time.strftime("%F %T"),
        }
        self.write("/etc/cron.d/xhs-ranking",
                   "# 榜单预计算任务（未加锁、未限流）\n*/1 * * * * xhs cd /srv/xhs && python3 jobs/ranking_precompute.py\n")
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} WARN [note-api] feed latency p99 820ms, queue depth 312\n")
        self._save()

    def inject_memory_leak(self, limit_mb: int = 220) -> None:
        req = urllib.request.Request(f"{self.api}/__control",
                                    data=json.dumps({"leak_mb": limit_mb}).encode(),
                                    headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        self.state.setdefault("faults", {})["memory"] = {"limit_mb": limit_mb, "oom": 0}
        self._save()

        def watchdog():
            while True:
                st = self.api_stats()
                if st.get("rss_mb", 0) > limit_mb:
                    pid = self.state["services"]["xhs-note-api"]["pid"]
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except OSError:
                        pass
                    with open(self.logdir / "dmesg.log", "a", encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%a %b %e %T %Y')}] Out of memory: Killed process {pid} "
                                f"(python3) total-vm:2048000kB, anon-rss:{limit_mb * 1024}kB\n")
                    self.state["faults"]["memory"]["oom"] += 1
                    svc = self.state["services"]["xhs-note-api"]
                    svc["restarts"] = svc.get("restarts", 0) + 1
                    self._save()
                    time.sleep(1)
                    self.start_api(port=svc["port"])
                    break
                time.sleep(0.5)

        t = threading.Thread(target=watchdog, daemon=True)
        t.start()
        self._threads = getattr(self, "_threads", []) + [t]
        # 等待第一次 OOM 与自动拉起完成，便于后续取证
        self.wait_until(lambda: self.state.get("faults", {}).get("memory", {}).get("oom", 0) >= 1, timeout=40)
        self.wait_until(lambda: self.api_stats().get("rss_mb", 0) > 0, timeout=10)

    def inject_slb_backend_unhealthy(self) -> None:
        self.state["slb"]["BackendServers"][0]["HealthStatus"] = "abnormal"
        self._save()
        req = urllib.request.Request(f"{self.api}/__control",
                                     data=json.dumps({"health": "db_timeout"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()

    def inject_sg_misconfig(self, port: int | None = None) -> None:
        port = port or self.ports["api"]
        sg = self.state["security_groups"][0]
        sg["Rules"] = [r for r in sg["Rules"] if str(r.get("PortRange")) != f"{port}/{port}"]
        self.state.setdefault("faults", {})["network"] = {"blocked_port": port}
        self._save()

    def inject_cert_expired(self) -> None:
        from .sims.certs import make_cert
        tls = self.dir / "tls"
        tls.mkdir(parents=True, exist_ok=True)
        make_cert(tls / "server.crt", tls / "server.key", expired=True)
        # 用 HTTPS 模式重启应用（端口复用）
        self.stop("api")
        svc = self.state["services"]["xhs-note-api"]
        self.start_api(port=svc["port"], tls=True)
        self.state["certs"][0]["NotAfter"] = "2026-11-01"
        self.state["certs"][0]["Status"] = "expired"
        self._save()

    def inject_disk_full(self, ratio: float = 0.96) -> None:
        target = self.quota_mb * ratio
        i = 0
        while self.disk_usage()[0] < target:
            try:
                self.write_data(f"logs/app-{i:03d}.log", 8)
            except OSError:
                break
            i += 1
        svc_pid = self.state["services"]["xhs-note-api"]["pid"]
        self.state.setdefault("faults", {})["disk"] = {
            "deleted_open": [{"name": "/data/logs/app.log.1", "size": 19 * 1024 * 1024, "pid": svc_pid}]
        }
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} ERROR [note-api] OSError: [Errno 28] No space left on device, "
                    "path=/data/logs/app.log\n")
        self._save()

    def inject_inode_exhaustion(self, count: int | None = None) -> None:
        count = count or int(self.inode_limit * 0.96)
        d = self.path("/data/sessions")
        d.mkdir(parents=True, exist_ok=True)
        old = time.time() - 86400 * 3
        created = 0
        for i in range(count):
            f = d / f"sess_{i:06d}.tmp"
            if not f.exists():
                f.write_text("", "utf-8")
                created += 1
                if i % 7 == 0:
                    os.utime(f, (old, old))
        self.state.setdefault("faults", {})["inode"] = {"created": created, "dir": "/data/sessions"}
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} ERROR [note-api] OSError: [Errno 28] No space left on device "
                    "(inode exhausted), path=/data/sessions\n")
        self._save()

    def inject_rds_pool_leak(self, connections: int = 60) -> None:
        import socket as _socket
        socks = getattr(self, "_leak_socks", [])
        for _ in range(connections):
            s = _socket.create_connection(("127.0.0.1", self.ports["mysql"]), timeout=2)
            s.settimeout(0.2)
            try:
                s.recv(64)
            except OSError:
                pass
            socks.append(s)
        self._leak_socks = socks
        self.state.setdefault("faults", {})["rds"] = {"leaked": len(socks)}
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} ERROR [note-api] pymysql.err.OperationalError: (1040, "
                    "'Too many connections')\n")
        self._save()

    def inject_redis_memory(self) -> None:
        self.redis("SET feed:rank:global:all 6291456")
        self.redis("SET note:detail:8f2a41c9 4194304")
        for i in range(10):
            self.redis(f"SET feed:rank:cat:{i} 2097152")
        self.state["redis"]["UsedMemoryMB"] = 32
        self.state["redis"]["Policy"] = "noeviction"
        self.state["redis"]["HitRate"] = 0.71
        self.state.setdefault("faults", {})["redis"] = {"hotkey": "feed:rank:global:all"}
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} WARN [note-api] redis SET failed: OOM command not allowed "
                    "when used memory > 'maxmemory'.\n")
        self._save()

    def inject_fd_leak(self, requests: int = 320) -> None:
        req = urllib.request.Request(f"{self.api}/__control",
                                     data=json.dumps({"fd_leak": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        last = ""
        for _ in range(requests):
            last = self.curl(f"{self.api}/api/v1/note/8f2a41c9")
            if "500" in last or "1007" in last:
                break
        self.state.setdefault("faults", {})["fd"] = {"requests": requests}
        self._save()

    def inject_bad_release(self, version: str = "v2026.11.20-1") -> None:
        bad_port = self.ports["api"] + 10
        self.write("/etc/xhs/note-api.conf",
                   "# xhs note-api 应用配置（v2026.11.20-1 发布引入错误端口）\n"
                   f"listen_host = 127.0.0.1\nlisten_port = {bad_port}\n"
                   "worker_processes = 4\n")
        self.stop("api")
        time.sleep(0.3)
        self._spawn("api", ["-m", "labs.common.sims.service", str(bad_port), "127.0.0.1"],
                    "note-api.log", nofile=256)
        self._wait_port(bad_port, 5)
        self.state["deploy"]["CurrentVersion"] = version
        self.state["deploy"]["Status"] = "failed"
        self.state["deploy"]["History"].insert(0, {"Version": version, "Time": time.strftime("%F %H:%M"), "Result": "failed"})
        self.state["services"]["xhs-note-api"]["port"] = self.ports["api"]
        with open(self.logdir / "note-api.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%FT%T%z')} INFO [deploy] release {version} applied, listen_port={bad_port}\n")
        self._save()

    def inject_alert_storm(self, total: int = 240) -> None:
        self.stop("logagent")
        self.state["logstore"]["Status"] = "agent_stopped"
        self.state["logstore"]["IngestLagSec"] = 600
        rule = self.state["alarm_rules"][0]
        rule.update({"Threshold": 1, "Period": 10, "For": 0, "Silence": 0})
        for i in range(total):
            self.state["alarms"].append({
                "AlarmId": f"alarm-{i:04d}", "RuleName": rule["Name"], "Metric": "cpu_total",
                "Value": 0.02, "Level": "P2", "Time": time.strftime("%F %H:%M:%S"),
                "Status": "ALARM",
            })
        self.state.setdefault("faults", {})["alert"] = {"total": total, "rule": "rule-api-cpu"}
        self._save()

    # ---------------- 修复 / 断言 ---------------- #
    def recovery_cpu(self) -> None:
        pids = self.state.get("faults", {}).get("cpu", {}).get("pids", [])
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.write("/etc/cron.d/xhs-ranking",
                   "# 已改造：加锁 + 限流 + 错峰，且由 systemd timer 串行触发\n"
                   "*/5 * * * * xhs flock -n /var/lock/ranking.lock nice -n 10 python3 /srv/xhs/jobs/ranking_precompute.py --limit 2000\n")
        self.state["faults"].pop("cpu", None)
        self._save()

    def assert_service_healthy(self, tries: int = 6) -> bool:
        for _ in range(tries):
            st = self.api_stats()
            if st.get("rss_mb") and st.get("requests") is not None:
                return True
            time.sleep(1)
        return False

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
        return ok

    def summary(self) -> None:
        print(f"[lab] case={self.case_id} runtime={self.dir}")
        print(f"[lab] ports={self.ports}")
        print(f"[lab] deploy={self.state.get('deploy', {}).get('CurrentVersion')} "
              f"alarms={len(self.state.get('alarms', []))}")

    def auto(self, script: str, *args: str) -> str:
        cmd = [PY, str(REPO_DIR / "automation" / script), "--case", self.case_id, *args]
        r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=180)
        return (r.stdout + r.stderr).strip()
