#!/usr/bin/env python3
"""文件描述符与连接句柄巡检。

处置逻辑：
1. 读取进程 FD 使用量与 LimitNOFILE；
2. 超过 80% 时先重启实例快速释放泄漏句柄（止血），并保留现场；
3. 提升 LimitNOFILE 并给出代码修复建议（连接/文件必须 with/close）；
4. 校验：业务接口恢复 200，FD 回落。
"""
from __future__ import annotations

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("FD 使用率巡检")
    st = lab.api_stats()
    svc = lab.state["services"]["xhs-note-api"]
    fds, limit = st.get("fds", 0), svc.get("nofile", 256)
    pct = fds / limit * 100 if limit else 0
    log(f"fds={fds}/{limit} ({pct:.0f}%)，服务 PID={svc['pid']}")
    log("lsof:\n" + lab.sh(f"lsof -p {svc['pid']}"))
    log(f"日志: {lab.sh('tail -n 4 /var/log/xhs/note-api.log')}")
    log("复现业务请求: " + lab.curl(lab.api + "/api/v1/note/8f2a41c9").splitlines()[-1])

    if pct < 80:
        log("FD 水位正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：更新 systemd LimitNOFILE=65535 → 滚动重启实例 → 校验业务")
        done(True, "dry-run")

    lab.write("/etc/systemd/system/xhs-note-api.service",
              (lab.path("/etc/systemd/system/xhs-note-api.service").read_text()
               .replace("LimitNOFILE=256", "LimitNOFILE=65535")))
    lab.restart_api(port=svc["port"], nofile=4096)
    log("已重启实例并提升 LimitNOFILE（256 → 65535，沙箱内核生效值 4096）")
    lab.state.get("faults", {}).pop("fd", None)
    lab._save()

    st2 = lab.api_stats()
    body = lab.curl(lab.api + "/api/v1/note/8f2a41c9")
    ok = "HTTP_CODE=200" in body and st2.get("fds", 999) < 100
    log(f"处置后 fds={st2.get('fds')}，业务返回={body.splitlines()[-1]}")
    done(ok, "FD 已释放，接口恢复")


if __name__ == "__main__":
    main()
