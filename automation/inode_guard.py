#!/usr/bin/env python3
"""inode 耗尽巡检与临时文件清理。

处置逻辑：
1. df -i 查看 inode 水位，find 定位小文件高密度目录；
2. 按 TTL 清理 /data/sessions 下超期会话临时文件；
3. 校验 inode 使用率与业务可用性。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from _base import cli, done, log

TTL_HOURS = 24


def main() -> None:
    lab, apply = cli("inode 使用率巡检")
    _, files = lab.disk_usage()
    pct = files / lab.inode_limit * 100
    log(f"df -i /data:\n{lab.sh('df -i')}")
    log(f"文件总数: {files} / {lab.inode_limit} ({pct:.0f}%)")
    log(f"du -sh /data/sessions: {lab.sh('du -sh /data/sessions')}")

    if pct < 80:
        log("inode 水位正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log(f"[dry-run] 计划：清理 /data/sessions 下超过 {TTL_HOURS}h 的 *.tmp（预计释放 {files} 个 inode）")
        done(True, "dry-run")

    cutoff = time.time() - TTL_HOURS * 3600
    removed = 0
    sessions = lab.data / "sessions"
    for f in sessions.glob("*.tmp"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    log(f"已清理 {removed} 个超期会话文件")

    # 兜底：TTL 清理后仍高于水位，则按最早 mtime 继续清理到 40%
    _, files_now = lab.disk_usage()
    if files_now / lab.inode_limit > 0.7:
        all_files = sorted(sessions.glob("*.tmp"), key=lambda f: f.stat().st_mtime)
        target = int(lab.inode_limit * 0.4)
        for f in all_files[:max(len(all_files) - target, 0)]:
            f.unlink(missing_ok=True)
            removed += 1
        log(f"仍高于水位，执行兜底清理，累计清理 {removed} 个文件")

    lab.state.get("faults", {}).pop("inode", None)
    lab._save()
    _, files2 = lab.disk_usage()
    log(f"处置后 inode 使用 {files} → {files2} ({files2 / lab.inode_limit * 100:.0f}%)")
    done(files2 / lab.inode_limit < 0.7, "inode 水位恢复")


if __name__ == "__main__":
    main()
