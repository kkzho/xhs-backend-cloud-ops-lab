#!/usr/bin/env python3
"""批量执行 12 个案例 Notebook 并汇总结果。

用法：
    .venv/bin/python tools/run_all_labs.py            # 全部执行
    .venv/bin/python tools/run_all_labs.py case-01    # 只跑匹配的案例
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

import nbformat
from nbclient import NotebookClient

REPO = Path(__file__).resolve().parent.parent
VENV_BIN = REPO / ".venv" / "bin"

if VENV_BIN.exists():
    os.environ["PATH"] = f"{VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}"


def run_one(path: Path) -> tuple[bool, str]:
    nb = nbformat.read(path, as_version=4)
    client = NotebookClient(
        nb,
        timeout=300,
        kernel_name="python3",
        resources={"metadata": {"path": str(path.parent)}},
        allow_errors=False,
    )
    started = time.time()
    try:
        client.execute()
        return True, f"{time.time() - started:.1f}s"
    except Exception as exc:  # noqa: BLE001 - 需要保留完整报错
        detail = str(exc).strip().splitlines()
        head = detail[-1] if detail else exc.__class__.__name__
        return False, f"{time.time() - started:.1f}s | {head}"


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    notebooks = sorted(p for p in REPO.glob("labs/case-*/lab.ipynb") if pattern in p.as_posix())
    if not notebooks:
        print("未找到 Notebook")
        return 2

    results = []
    for path in notebooks:
        print(f"▶ {path.parent.name} ...", flush=True)
        ok, detail = run_one(path)
        results.append((path.parent.name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'} {detail}", flush=True)

    failed = [r for r in results if not r[1]]
    print("\n===== 汇总 =====")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    if failed:
        print("失败案例：", ", ".join(f[0] for f in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
