#!/usr/bin/env python3
"""磁盘使用率巡检与日志清理。

处置逻辑：
1. df/du 定位占用目录与大文件；
2. lsof +L1 检查"已删除但仍被进程持有"的文件（空间不释放的常见原因）；
3. 按保留策略清理：仅保留每类日志最近 2 个文件，压缩后归档；
4. 校验使用率回落。
"""
from __future__ import annotations

import gzip
import shutil
import time
from pathlib import Path

from _base import cli, done, log


def main() -> None:
    lab, apply = cli("磁盘使用率巡检")
    used, _ = lab.disk_usage()
    pct = used / lab.quota_mb * 100
    log(f"df -h /data:\n{lab.sh('df -h /data')}")
    log(f"du -sh /data/*:\n{lab.sh('du -sh /data/*')}")
    log(f"lsof +L1:\n{lab.sh('lsof +L1')}")

    if pct < 80:
        log("磁盘水位正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：清理 app-*.log 历史文件（保留最近 2 个）→ 归档压缩 → 释放 deleted-open 句柄")
        done(True, "dry-run")

    logs = sorted((lab.data / "logs").glob("app-*.log"), key=lambda f: f.stat().st_mtime)
    for f in logs[:-2]:
        f.unlink(missing_ok=True)
    for f in logs[-2:]:
        with open(f, "rb") as src, gzip.open(str(f) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        f.unlink(missing_ok=True)
    log(f"清理 {max(len(logs) - 2, 0)} 个历史日志，剩余文件: {sorted(p.name for p in (lab.data / 'logs').iterdir())}")

    deleted = lab.state.get("faults", {}).get("disk", {}).get("deleted_open", [])
    if deleted:
        # 释放持有已删除文件的进程（线上通常是 logrotate 后未 reload）
        lab.kill_service()
        lab.start_api(port=lab.state["services"]["xhs-note-api"]["port"])
        lab.state["faults"]["disk"]["deleted_open"] = []
        log("已重启持有 deleted 文件句柄的进程，空间释放")

    lab.state.get("faults", {}).pop("disk", None)
    lab._save()
    used2, _ = lab.disk_usage()
    pct2 = used2 / lab.quota_mb * 100
    log(f"处置后使用率 {pct:.0f}% → {pct2:.0f}%")
    done(pct2 < 70, "磁盘水位恢复")


if __name__ == "__main__":
    main()
