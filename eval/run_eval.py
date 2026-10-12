#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目一 · v1.3 效果评测
======================

对 eval/dataset.json 中的用例逐条调用智能体，量化四个指标：

  1. 意图路由准确率 —— 由回复关键词反推实际命中分支，与期望分支比对
  2. 关键信息覆盖率 —— 回复是否答到点上（expect_any 至少命中一个）
  3. 回复规范性     —— 是否出现禁词（冒充人工客服 / XML 标签 / 代码块等）
  4. 响应延迟       —— 平均 / P95 / 最长

用法：
    python3 eval/run_eval.py                    # 跑全部用例（默认调本地代理）
    python3 eval/run_eval.py --limit 6          # 只跑前 6 条，快速验证
    python3 eval/run_eval.py --workers 1        # 单线程，最稳
    python3 eval/run_eval.py --base https://youmian-customer-service.onrender.com

输出：eval/report.md（同时打印终端摘要）

说明：对话流的分类结果不对外暴露，所以「实际分支」是用回复关键词推断的，
      属于代理指标；报告里会如实标注。
"""

from __future__ import annotations

import argparse
import json
import statistics
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "dataset.json"
REPORT = ROOT / "eval" / "report.md"

# 单次请求最长等待（秒）。知识库类问题偶尔较慢。
REQUEST_TIMEOUT = 120
# 失败重试次数
RETRY = 1


def build_ssl_context() -> ssl.SSLContext:
    """python.org 的 Python 不自带 CA 包，这里按系统路径找一个可用的根证书。"""
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(path).is_file():
            try:
                return ssl.create_default_context(cafile=path)
            except ssl.SSLError:
                continue
    return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


def ask(base_url: str, message: str, user_id: str) -> tuple[bool, str, float, str]:
    """调用一次代理接口，返回 (是否成功, 回复文本, 耗时秒, 错误信息)。"""
    payload = json.dumps({"message": message, "user_id": user_id}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
            answer = ""
            event = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    event = ""
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if event == "delta" and data:
                    try:
                        answer += json.loads(data).get("content", "")
                    except json.JSONDecodeError:
                        pass
                elif event == "error" and data:
                    try:
                        return False, "", time.perf_counter() - started, json.loads(data).get("message", "error")
                    except json.JSONDecodeError:
                        return False, "", time.perf_counter() - started, "error"
            elapsed = time.perf_counter() - started
            if not answer:
                return False, "", elapsed, "返回内容为空"
            return True, answer, elapsed, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", "")
        except json.JSONDecodeError:
            msg = ""
        return False, "", time.perf_counter() - started, msg or f"HTTP {exc.code}"
    except Exception as exc:  # 网络中断 / 超时等
        return False, "", time.perf_counter() - started, f"{type(exc).__name__}: {exc}"


def infer_branch(reply: str, branch_keywords: dict[str, list[str]]) -> tuple[str, dict[str, int]]:
    """用关键词命中情况推断实际走了哪条分支。

    打分按关键词长度加权：越长的短语越具体（例如「欢迎来到优眠家居」比「物流」更能说明问题），
    否则开场白里顺带提到的「发货物流」「换货」会把分堆到别的分支上，造成误判。
    """
    scores = {branch: sum(len(kw) for kw in kws if kw in reply) for branch, kws in branch_keywords.items()}
    best = max(scores.values()) if scores else 0
    if best == 0:
        return "未识别", scores
    winners = [b for b, s in scores.items() if s == best]
    return (winners[0] if len(winners) == 1 else "歧义"), scores


def run_case(case: dict, base_url: str, branch_keywords: dict, forbidden: list[str]) -> dict:
    """跑单条用例并打分。"""
    case_id = case["id"]
    for attempt in range(RETRY + 1):
        ok, reply, elapsed, err = ask(base_url, case["input"], f"eval_{case_id}")
        if ok:
            break
        if attempt < RETRY:
            time.sleep(3)

    result = {
        "id": case_id,
        "input": case["input"],
        "expect": case["expect_branch"],
        "ok": ok,
        "error": err,
        "elapsed": round(elapsed, 1),
        "reply": reply,
        "branch": "",
        "branch_correct": False,
        "keyword_hit": False,
        "forbidden_hits": [],
        "length_ok": False,
    }

    if not ok:
        return result

    branch, _ = infer_branch(reply, branch_keywords)
    result["branch"] = branch
    result["branch_correct"] = branch == case["expect_branch"]
    result["keyword_hit"] = any(kw in reply for kw in case["expect_any"])
    result["forbidden_hits"] = [w for w in forbidden if w in reply]
    result["length_ok"] = 15 <= len(reply) <= 800
    return result


def summarize(results: list[dict], branch_keywords: dict) -> dict:
    total = len(results)
    succeeded = [r for r in results if r["ok"]]
    latencies = [r["elapsed"] for r in succeeded]

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "—"

    per_branch = {}
    for r in results:
        b = per_branch.setdefault(r["expect"], {"total": 0, "route": 0, "keyword": 0})
        b["total"] += 1
        if r["branch_correct"]:
            b["route"] += 1
        if r["keyword_hit"]:
            b["keyword"] += 1

    return {
        "total": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "route_ok": sum(1 for r in results if r["branch_correct"]),
        "keyword_ok": sum(1 for r in results if r["keyword_hit"]),
        "forbidden_ok": sum(1 for r in results if r["ok"] and not r["forbidden_hits"]),
        "length_ok": sum(1 for r in results if r["length_ok"]),
        "latency_avg": round(statistics.mean(latencies), 1) if latencies else 0,
        "latency_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1) if len(latencies) >= 2 else (latencies[0] if latencies else 0),
        "latency_max": max(latencies) if latencies else 0,
        "per_branch": per_branch,
        "route_pct": pct(sum(1 for r in results if r["branch_correct"])),
        "keyword_pct": pct(sum(1 for r in results if r["keyword_hit"])),
        "forbidden_pct": pct(sum(1 for r in results if r["ok"] and not r["forbidden_hits"])),
        "length_pct": pct(sum(1 for r in results if r["length_ok"])),
    }


def write_report(meta: dict, s: dict, results: list[dict]) -> None:
    lines = []
    a = lines.append

    a("# 项目一 · v1.3 效果评测报告")
    a("")
    a(f"> 生成时间：{meta['time']}　|　模型：{meta['model']}　|　接口：`{meta['base']}`")
    a(f"> 用例总数：{s['total']}　|　成功返回：{s['succeeded']}　|　失败：{s['failed']}")
    a("")

    a("## 一、总体指标")
    a("")
    a("| 指标 | 结果 | 说明 |")
    a("|------|------|------|")
    a(f"| **意图路由准确率** | **{s['route_pct']}**（{s['route_ok']}/{s['total']}） | 回复反推分支 == 期望分支 |")
    a(f"| 关键信息覆盖率 | {s['keyword_pct']}（{s['keyword_ok']}/{s['total']}） | 回复命中该分支的预期关键词 |")
    a(f"| 回复规范性 | {s['forbidden_pct']}（{s['forbidden_ok']}/{s['total']}） | 未出现禁词（冒充人工 / XML 标签 / 代码块） |")
    a(f"| 回复长度合理 | {s['length_pct']}（{s['length_ok']}/{s['total']}） | 长度在 15–800 字之间 |")
    a(f"| 平均响应延迟 | {s['latency_avg']}s | P95 = {s['latency_p95']}s，最长 {s['latency_max']}s |")
    a("")

    a("## 二、分支维度")
    a("")
    a("| 期望分支 | 用例数 | 路由正确 | 路由准确率 | 关键信息命中 |")
    a("|---------|-------|---------|-----------|-------------|")
    for branch, v in s["per_branch"].items():
        rate = f"{v['route'] / v['total'] * 100:.0f}%" if v["total"] else "—"
        a(f"| {branch} | {v['total']} | {v['route']} | {rate} | {v['keyword']}/{v['total']} |")
    a("")

    bad = [r for r in results if not (r["ok"] and r["branch_correct"] and r["keyword_hit"] and not r["forbidden_hits"])]
    a(f"## 三、未完全通过的用例（{len(bad)} 条）")
    a("")
    if not bad:
        a("全部用例通过。")
    else:
        a("| ID | 输入 | 期望分支 | 实际推断 | 关键信息 | 问题 | 回复摘要 |")
        a("|----|------|---------|---------|---------|------|---------|")
        for r in bad:
            problems = []
            if not r["ok"]:
                problems.append(f"请求失败（{r['error']}）")
            else:
                if not r["branch_correct"]:
                    problems.append("路由不符")
                if not r["keyword_hit"]:
                    problems.append("未命中关键信息")
                if r["forbidden_hits"]:
                    problems.append("出现禁词：" + "/".join(r["forbidden_hits"]))
            reply = (r["reply"] or "").replace("\n", " ")[:60] or "—"
            a(f"| {r['id']} | {r['input']} | {r['expect']} | {r['branch'] or '—'} | "
              f"{'✅' if r['keyword_hit'] else '❌'} | {'；'.join(problems)} | {reply} |")
    a("")

    a("## 四、验收结论")
    a("")
    targets = [
        ("意图路由准确率", s["route_ok"], 0.90),
        ("关键信息覆盖率", s["keyword_ok"], 0.95),
        ("回复规范性", s["forbidden_ok"], 1.00),
    ]
    for name, ok, target in targets:
        rate = ok / s["total"] if s["total"] else 0
        mark = "✅ 达标" if rate >= target else "⚠️ 未达标"
        a(f"- {name}：{rate * 100:.1f}%（目标 ≥ {target * 100:.0f}%）{mark}")
    weak = [b for b, v in s["per_branch"].items() if v["route"] < v["total"]]
    if weak:
        a(f"- 需跟进的分支：{'、'.join(weak)}（详见第三章）")
    else:
        a("- 六条分支路由全部正确")
    a("- 延迟：平均 "
      f"{s['latency_avg']}s、P95 {s['latency_p95']}s。客服场景可接受，但长回答（知识库检索类）明显偏慢，"
      "后续可考虑精简知识库条目、或把模型换成更快档位。")
    a("")

    a("## 五、评测方法与局限")
    a("")
    a("- **数据来源**：`eval/dataset.json`，6 类意图各 5 条，共 30 条，覆盖常见问法与边界问法（闲聊、议价、投诉）。")
    a("- **调用方式**：逐条请求 `POST /api/chat`，每条用例使用独立 `user_id` 且不复用会话，保证用例之间互不影响。")
    a("- **路由判定**：扣子对话流的分类结果不对外暴露，因此由**回复文本中的分支关键词**反推实际分支；"
      "打分按关键词长度加权（越长的短语越具体），得分最高者判为该分支。")
    a("- **局限**：这是**代理指标**而非真实标签。开场白之类会同时提到多条业务线，"
      "即使加权后仍可能出现边界误判；要拿到真实标签，需要在对话流里额外输出分类结果。")
    a("- **延迟**：包含端到端耗时（浏览器 → 代理 → 扣子 → 模型生成），受网络与模型排队影响，不代表模型自身耗时。")
    a("")
    a("### 复现方式")
    a("")
    a("```bash")
    a("python3 server.py            # 另开一个终端启动代理")
    a("python3 eval/run_eval.py     # 跑全部用例，结果写入 eval/report.md")
    a("```")
    a("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="项目一 v1.3 效果评测")
    parser.add_argument("--base", default="http://localhost:3000", help="代理服务地址")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--workers", type=int, default=3, help="并发数，默认 3")
    args = parser.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.limit:
        cases = cases[: args.limit]

    branch_keywords = data["branch_keywords"]
    forbidden = data["必须避免出现"]

    print(f"▶ 评测开始：{len(cases)} 条用例 | 并发 {args.workers} | 接口 {args.base}")
    print("─" * 62)

    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_case, c, args.base, branch_keywords, forbidden): c for c in cases}
        for fut in futures:
            c = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"id": c["id"], "input": c["input"], "expect": c["expect_branch"], "ok": False,
                     "error": str(exc), "elapsed": 0, "reply": "", "branch": "", "branch_correct": False,
                     "keyword_hit": False, "forbidden_hits": [], "length_ok": False}
            results.append(r)
            done += 1
            flag = "✅" if (r["ok"] and r["branch_correct"] and r["keyword_hit"]) else "❌"
            print(f"  [{done:>2}/{len(cases)}] {flag} {r['id']:<4} {r['expect']:<6} "
                  f"→ {r['branch'] or r['error']:<8} {r['elapsed']:>5.1f}s")

    results.sort(key=lambda x: x["id"])
    s = summarize(results, branch_keywords)

    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": "豆包 2.0 pro（扣子平台）",
        "base": args.base,
    }
    write_report(meta, s, results)

    print("─" * 62)
    print(f"意图路由准确率 : {s['route_pct']}  ({s['route_ok']}/{s['total']})")
    print(f"关键信息覆盖率 : {s['keyword_pct']}  ({s['keyword_ok']}/{s['total']})")
    print(f"回复规范性     : {s['forbidden_pct']}  ({s['forbidden_ok']}/{s['total']})")
    print(f"平均延迟       : {s['latency_avg']}s   (P95 {s['latency_p95']}s)")
    print(f"报告已写入     : {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
