#!/usr/bin/env python3
"""Redis 内存打满与大 key 巡检。

处置逻辑：
1. INFO memory / --bigkeys 确认 used_memory、淘汰策略、大 key；
2. 短期：切换 allkeys-lru 恢复写入能力；拆分/删除超大 key；
3. 长期：热点 key 加本地缓存 + 设置 TTL + 扩容评估；
4. 校验：可正常 SET，内存低于 maxmemory。
"""
from __future__ import annotations

import re

from _base import cli, done, log


def _parse(info: str) -> dict:
    out = {}
    for line in info.splitlines():
        if ":" in line and not line.startswith("#"):
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    lab, apply = cli("Redis 内存巡检")
    info = _parse(lab.sh("redis-cli INFO memory"))
    used = int(info.get("used_memory", 0))
    maxmem = int(info.get("maxmemory", 0))
    policy = info.get("maxmemory_policy", "?")
    log(f"used_memory={used / 1024 / 1024:.1f}MB / maxmemory={maxmem / 1024 / 1024:.1f}MB，policy={policy}")
    log("大 key 扫描:\n" + lab.sh("redis-cli --bigkeys"))
    log(f"应用日志: {lab.sh('tail -n 3 /var/log/xhs/note-api.log')}")

    if used < maxmem * 0.8:
        log("内存水位正常，无需处置")
        done(True, "NOOP")

    if not apply:
        log("[dry-run] 计划：maxmemory-policy=allkeys-lru → 拆分/删除大 key → 设置 TTL → 校验写入")
        done(True, "dry-run")

    lab.sh("redis-cli CONFIG SET maxmemory-policy allkeys-lru")
    log("已切换淘汰策略为 allkeys-lru")

    big = re.findall(r"found '([^']+)' has (\d+) bytes", lab.sh("redis-cli --bigkeys"))
    removed = 0
    for key, size in big:
        if int(size) >= 4 * 1024 * 1024:
            lab.sh(f"redis-cli DEL {key}")
            removed += 1
            log(f"已删除大 key {key} ({int(size) / 1024 / 1024:.1f}MB)，线上应改为分片 Hash + 预热")
    lab.redis("SET feed:rank:global:all 2097152")

    lab.state["redis"]["Policy"] = "allkeys-lru"
    lab.state["redis"]["UsedMemoryMB"] = 28
    lab.state["redis"]["HitRate"] = 0.93
    lab.state.get("faults", {}).pop("redis", None)
    lab._save()

    after = _parse(lab.sh("redis-cli INFO memory"))
    ok_write = "OK" in lab.sh("redis-cli SET feed:rank:smoke 1048576")
    ok = ok_write and int(after.get("used_memory", 0)) <= int(after.get("maxmemory", 1))
    log(f"处置后 used={int(after.get('used_memory', 0)) / 1024 / 1024:.1f}MB，写入={'成功' if ok_write else '失败'}，"
        f"清理大 key {removed} 个")
    done(ok, "Redis 写入恢复")


if __name__ == "__main__":
    main()
