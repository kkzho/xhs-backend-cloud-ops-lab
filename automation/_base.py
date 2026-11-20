"""自动化脚本公共入口。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from labs.common.xhs_lab import Lab  # noqa: E402


def cli(description: str) -> tuple[Lab, bool]:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--case", required=True, help="案例 ID，例如 case-01-cpu-runaway")
    ap.add_argument("--apply", action="store_true", help="执行修复（缺省仅 dry-run 输出计划）")
    a = ap.parse_args()
    return Lab(a.case), a.apply


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def done(ok: bool, detail: str = "") -> None:
    print(f"RESULT: {'REMEDIATED' if ok else 'FAILED'}" + (f" | {detail}" if detail else ""), flush=True)
    sys.exit(0 if ok else 1)
