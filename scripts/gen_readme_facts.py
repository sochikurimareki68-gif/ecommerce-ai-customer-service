#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G4：生成 / 校验 README 的「自动生成事实区」。

背景：README 长期与现实不符 —— 测试条数、版本号、架构节点数、评测指标全是手写的，
改了代码没人记得改文档。这里把可计算的事实统一生成：

  · 版本号        ← server.py 的 __version__
  · 单元测试条数  ← unittest 发现结果
  · 评测用例条数  ← eval/dataset.json
  · 架构参数      ← docs/architecture-facts.json（LLM 节点数 / 意图域 / 上下文轮数）
  · 最新评测指标  ← eval/report.json（G3 的单一真源）
  · health 字段   ← 真的起一个本地服务打一次 /api/health

README 里只允许改 `<!-- FACTS:BEGIN -->` 与 `<!-- FACTS:END -->` 之间的内容，
由本脚本整体替换；其余「人工说明区」随意手写。

用法：
    python3 scripts/gen_readme_facts.py           # 重写事实区
    python3 scripts/gen_readme_facts.py --check   # 只校验，不一致时打印 diff 并退出 1
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SERVER = ROOT / "server.py"
DATASET = ROOT / "eval" / "dataset.json"
REPORT_JSON = ROOT / "eval" / "report.json"
ARCH_FACTS = ROOT / "docs" / "architecture-facts.json"

BEGIN = "<!-- FACTS:BEGIN -->"
END = "<!-- FACTS:END -->"


def read_version() -> str:
    text = SERVER.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("server.py 里找不到 __version__")
    return match.group(1)


def count_tests() -> int:
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    return suite.countTestCases()


def health_fields() -> list[str]:
    """真起一个本地服务打一次 /api/health，字段列表以接口实际返回为准。"""
    sys.path.insert(0, str(ROOT))
    import server  # noqa: E402  （脚本内延迟导入，避免影响 --help）

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    fields: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            fields.extend(f"{key}.{sub}" for sub in value)
        else:
            fields.append(key)
    return fields


def build_block() -> str:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    arch = json.loads(ARCH_FACTS.read_text(encoding="utf-8"))
    report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    metrics = report.get("metrics", {})

    version = read_version()
    tests = count_tests()
    fields = health_fields()
    counts = report.get("counts", {})

    lines = [
        BEGIN,
        "> **自动生成事实区** —— 由 `scripts/gen_readme_facts.py` 从代码、数据集与评测报告生成，**请勿手改**；"
        "校验命令：`python3 scripts/gen_readme_facts.py --check`。",
        ">",
        f"> - 版本：v{version}（`server.py` 的 `__version__`）",
        f"> - 单元测试：{tests} 条",
        f"> - 评测用例：单轮 {counts.get('single', len(data.get('cases', [])))} / "
        f"多轮 {counts.get('multi', 0)} / 事实 {counts.get('facts', 0)} / "
        f"安全 {counts.get('security', 0)} / 记忆 {counts.get('memory', 0)}",
        f"> - 对话流：LLM 节点 {arch['llm_nodes']} 个、意图域 {arch['intent_domains']} 类、"
        f"节点会话历史 {arch['history_rounds']} 轮、智能体上下文 {arch['agent_context_rounds']} 轮",
        f"> - 最新评测（v{report.get('version', '')}，{report.get('time', '')}）："
        f"关键信息覆盖率 {metrics.get('keyword_pct', 0):.1f}%、"
        f"回复规范性 {metrics.get('forbidden_pct', 0):.1f}%；"
        f"分支归属一致率 {metrics.get('route_pct', 0):.1f}%（关键词推断，仅供参考、不计入验收）",
        f"> - `/api/health` 字段：{', '.join(fields)}",
        END,
    ]
    return "\n".join(lines)


def apply_block(text: str, block: str) -> str:
    if BEGIN not in text or END not in text:
        raise SystemExit(f"README 缺少事实区标记 {BEGIN} / {END}，请先插入一对空标记")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    return pattern.sub(lambda _: block, text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 README 自动事实区")
    parser.add_argument("--check", action="store_true", help="只校验，不一致则退出 1")
    args = parser.parse_args()

    block = build_block()
    current = README.read_text(encoding="utf-8")
    expected = apply_block(current, block)

    if args.check:
        if current == expected:
            print("✅ README 事实区与代码/数据集/报告一致")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(), expected.splitlines(),
            fromfile="README.md（当前）", tofile="README.md（应为）", lineterm="",
        )
        print("❌ README 事实区已过期，运行 `python3 scripts/gen_readme_facts.py` 重新生成：")
        print("\n".join(diff))
        return 1

    README.write_text(expected, encoding="utf-8")
    print("✅ 已更新 README 事实区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
