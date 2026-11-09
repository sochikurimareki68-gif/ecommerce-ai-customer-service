#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3：校验「评测报告 ↔ 简历声明 ↔ README 事实区」三处数字一致。

背景（真实事故）：同一个可溯源率，README 写 99.5%、评测报告写 99.0%、简历写 99%，
三处互不一致；而且这些数字是在知识库被全部禁用、检索为 0 的状态下写下的。
根因是数字没有单一真源，靠人手抄。本脚本把 `eval/report.json` 定为唯一真源：

  eval/report.json  ──(自动生成)──> README 事实区（G4 的 gen_readme_facts.py）
          │
          └──(本脚本校验)──> resume_claims.json（简历里承诺的数字）

任何一处与报告不符，脚本退出 1 并打印差异。CI 与投递前都必须跑一次。

用法：
    python3 scripts/check_claims.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_JSON = ROOT / "eval" / "report.json"
CLAIMS = ROOT / "resume_claims.json"
README = ROOT / "README.md"
BEGIN = "<!-- FACTS:BEGIN -->"
END = "<!-- FACTS:END -->"


def fail(message: str) -> None:
    print(f"❌ {message}")
    problems.append(message)


def facts_block() -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, re.DOTALL)
    if not match:
        raise SystemExit(f"README 缺少事实区标记 {BEGIN} / {END}")
    return match.group(1)


def main() -> int:
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    claims = json.loads(CLAIMS.read_text(encoding="utf-8"))
    report_metrics = report.get("metrics", {})
    report_counts = report.get("counts", {})
    block = facts_block()

    for key, claimed in claims.get("metrics", {}).items():
        if key not in report_metrics:
            fail(f"resume_claims.json 的指标 {key} 不在 eval/report.json 中，无法核对")
            continue
        actual = float(report_metrics[key])
        if not math.isclose(float(claimed), actual, rel_tol=0, abs_tol=1e-9):
            fail(f"{key}：简历声明 {claimed}，评测报告 {actual}")
        elif f"{actual:.1f}%" not in block:
            fail(f"{key}={actual:.1f}% 未出现在 README 事实区（先跑 gen_readme_facts.py）")

    for key, claimed in claims.get("counts", {}).items():
        if key not in report_counts:
            fail(f"resume_claims.json 的计数 {key} 不在 eval/report.json 中，无法核对")
            continue
        actual = report_counts[key]
        if int(claimed) != int(actual):
            fail(f"{key}：简历声明 {claimed}，评测报告 {actual}")

    if problems:
        print("\n请以 eval/report.json 为准，同步修改 resume_claims.json 与 README 事实区；"
              "简历正文不在本仓库，需人工同步。")
        return 1
    print("✅ 三处数字一致：eval/report.json ↔ resume_claims.json ↔ README 事实区")
    return 0


problems: list[str] = []

if __name__ == "__main__":
    sys.exit(main())
