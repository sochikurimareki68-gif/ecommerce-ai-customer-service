#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G5：用 git 提交记录校验简历里的项目起止时间与仓库归属。

背景（真实事故）：简历写项目二「2026.08 - 2026.10」，仓库首个提交却是 2026-10-12，
8 月、9 月一条提交都没有；面试官点开 commit 列表当场就能看出问题。日期只能从
git 生成，不能手写。

规则：
  · 声明起始月必须等于首个提交所在月（早于 = 声明了空档期；晚于 = 隐瞒了早期提交）
  · 声明结束月不得晚于末个提交所在月（否则声明了不存在的月份）
  · 末个提交晚于声明结束月 → 警告（通常是投递后仍在维护，不算错）
  · origin 的 GitHub 账号必须与 resume_claims.json 的 github_owner 一致

用法：
    python3 scripts/resume_dates.py            # 打印真实区间并校验
    python3 scripts/resume_dates.py --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAIMS = ROOT / "resume_claims.json"


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def commit_range() -> dict:
    dates = [d for d in git("log", "--reverse", "--format=%cs").splitlines() if d]
    if not dates:
        raise SystemExit("当前仓库没有任何提交记录")
    return {
        "first_commit": dates[0],
        "last_commit": dates[-1],
        "first_month": dates[0][:7],
        "last_month": dates[-1][:7],
        "commit_count": len(dates),
    }


def remote_owner() -> str:
    url = git("remote", "get-url", "origin")
    # 兼容 git@github.com:owner/repo.git 与 ssh://git@ssh.github.com:443/owner/repo.git
    tail = url.rstrip("/").rsplit(":", 1)[-1] if url.startswith("git@") else url.rstrip("/")
    parts = tail.replace(":", "/").split("/")
    if len(parts) < 2:
        return ""
    return parts[-2]


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 git 日期 / 归属校验")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    info = commit_range()
    info["remote_owner"] = remote_owner()
    claims = json.loads(CLAIMS.read_text(encoding="utf-8")) if CLAIMS.is_file() else {}
    period = claims.get("period", {})

    problems: list[str] = []
    warnings: list[str] = []
    if period:
        start = period.get("start", "")
        end = period.get("end", "")
        if start != info["first_month"]:
            problems.append(
                f"简历起始 {start or '（空）'}，仓库首个提交 {info['first_commit']}（{info['first_month']}）"
            )
        if end and end < info["last_month"]:
            warnings.append(
                f"简历结束 {end} 早于仓库末次提交月份 {info['last_month']}（投递后仍有维护提交，"
                f"通常可接受；如需覆盖最新提交请改为 {info['last_month']}）"
            )
        elif end and end > info["last_month"]:
            problems.append(f"简历结束 {end} 晚于仓库末次提交月份 {info['last_month']}（声明了不存在的月份）")
    else:
        warnings.append("resume_claims.json 未填写 period，仅打印 git 区间")

    declared_owner = claims.get("github_owner", "")
    if declared_owner and info["remote_owner"] and declared_owner != info["remote_owner"]:
        problems.append(f"仓库归属 {info['remote_owner']}，声明为 {declared_owner}")

    info.update({"declared_period": period, "problems": problems, "warnings": warnings})
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"仓库：{ROOT.name}")
        print(f"首个提交：{info['first_commit']}　末个提交：{info['last_commit']}　提交数：{info['commit_count']}")
        print(f"真实区间：{info['first_month']} ~ {info['last_month']}　GitHub 账号：{info['remote_owner'] or '—'}")
        if period:
            print(f"简历声明：{period.get('start', '—')} ~ {period.get('end', '—')}")
        for w in warnings:
            print(f"⚠️  {w}")
        for p in problems:
            print(f"❌ {p}")
        if problems:
            print("\n请按上方 git 真实区间修改简历与 resume_claims.json（投递前必须通过）。")
        else:
            print("✅ 简历日期与 git 提交记录一致")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
