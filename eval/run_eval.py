#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目一 · v1.7 效果评测
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
import http.cookiejar
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

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "dataset.json"
REPORT = ROOT / "eval" / "report.md"
MULTI_REPORT = ROOT / "eval" / "report-multi.md"

# 单次请求最长等待（秒）。知识库类问题偶尔较慢。
REQUEST_TIMEOUT = 120
# 失败重试次数。限流（429）单独等一个完整窗口，避免 3 秒后重试仍然被拒。
RETRY = 2
RATE_LIMIT_WAIT = 65


def is_rate_limited(error: str) -> bool:
    """代理把限流原因写成人话，这里同时兼容状态码与提示文案。"""
    return any(word in error for word in ("429", "太快", "上限", "额度"))


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


def iter_sse_events(resp):
    """逐行解析 SSE 流，产出 (event, data) 元组。

    与前端 index.html 的解析逻辑保持一致：空行分隔事件，data 多行拼接。
    代理只下发 meta / delta / error / done 四类事件，这里只负责拆包，不做业务判断。
    """
    event = ""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:            # 空行 = 一个事件结束
            if event:
                yield event, "\n".join(data_lines)
            event = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def ask(base_url: str, message: str, opener=None) -> tuple[bool, str, float, str, float, str]:
    """调用一次代理接口，返回 (是否成功, 回复文本, 总耗时秒, 错误信息, 首字时间秒, 分类标签)。

    不传 opener 时不带 cookie，每次调用在服务端都是新会话 —— 单轮用例靠这个保证互不影响。
    多轮用例传入带 CookieJar 的 opener，让同一组的两句落在同一个会话里。

    首字时间（TTFT）= 从发出请求到收到第一个 delta 的间隔，用于区分「等答案」和「生成答案」。
    拿不到任何 delta 时记为 0，统计时排除。

    分类标签来自代理下发的 branch 事件（源头是对话流代码节点判断出的真实分支）。
    服务端未提供时为空字符串，由调用方退回关键词反推。
    """
    payload = json.dumps({"message": message}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    first_delta = 0.0
    branch = ""
    try:
        if opener is None:
            resp_cm = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT)
        else:
            resp_cm = opener.open(req, timeout=REQUEST_TIMEOUT)
        with resp_cm as resp:
            answer = ""
            for event, data in iter_sse_events(resp):
                if event == "delta" and data:
                    if not first_delta:
                        first_delta = time.perf_counter() - started
                    try:
                        answer += json.loads(data).get("content", "")
                    except json.JSONDecodeError:
                        pass
                elif event == "branch" and data:
                    # 代理下发的真实分类标签（对话流代码节点判断的结果）
                    try:
                        branch = json.loads(data).get("branch", "") or ""
                    except json.JSONDecodeError:
                        pass
                elif event == "error" and data:
                    try:
                        return (False, "", time.perf_counter() - started,
                                json.loads(data).get("message", "error"), first_delta, branch)
                    except json.JSONDecodeError:
                        return False, "", time.perf_counter() - started, "error", first_delta, branch
            elapsed = time.perf_counter() - started
            if not answer:
                return False, "", elapsed, "返回内容为空", first_delta, branch
            return True, answer, elapsed, "", first_delta, branch
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", "")
        except json.JSONDecodeError:
            msg = ""
        return False, "", time.perf_counter() - started, msg or f"HTTP {exc.code}", first_delta, branch
    except Exception as exc:  # 网络中断 / 超时等
        return False, "", time.perf_counter() - started, f"{type(exc).__name__}: {exc}", first_delta, branch


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
    ttft = 0.0
    for attempt in range(RETRY + 1):
        ok, reply, elapsed, err, ttft, label = ask(base_url, case["input"])
        if ok:
            break
        if attempt < RETRY:
            # 被限流时等一个完整窗口；其他错误短重试即可
            time.sleep(RATE_LIMIT_WAIT if is_rate_limited(err) else 3)

    result = {
        "id": case_id,
        "input": case["input"],
        "expect": case["expect_branch"],
        "ok": ok,
        "error": err,
        "elapsed": round(elapsed, 1),
        "ttft": round(ttft, 1),
        "reply": reply,
        "branch": "",
        "branch_source": "",
        "branch_correct": False,
        "keyword_hit": False,
        "forbidden_hits": [],
        "length_ok": False,
    }

    if not ok:
        return result

    if label:
        branch = label
        result["branch_source"] = "服务端标签"
    else:
        branch, _ = infer_branch(reply, branch_keywords)
        result["branch_source"] = "关键词反推"
    result["branch"] = branch
    result["branch_correct"] = branch == case["expect_branch"]
    result["keyword_hit"] = any(kw in reply for kw in case["expect_any"])
    result["forbidden_hits"] = [w for w in forbidden if w in reply]
    result["length_ok"] = 15 <= len(reply) <= 800
    return result


# ----------------------------------------------------------------------
# 多轮用例：同一会话连发两句，只看第二句有没有读懂上文
# ----------------------------------------------------------------------
# 两句之间的间隔（秒）。给会话落库留时间，也避开「单 IP 每分钟 6 次」。
MULTI_TURN_GAP = 8


def build_session_opener():
    """带 CookieJar 的 opener：服务端通过 HttpOnly Cookie 认会话。"""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    )


def run_multi_case(case: dict, base_url: str, forbidden: list[str]) -> dict:
    """连发两轮，第二轮命中 expect_any 即视为读懂了上下文。"""
    opener = build_session_opener()
    replies: list[str] = []
    latencies: list[float] = []
    ttfts: list[float] = []
    error = ""

    for idx, question in enumerate(case["turns"]):
        ok, reply, elapsed, err, ttft, _label = False, "", 0.0, "", 0.0, ""
        for attempt in range(RETRY + 1):
            ok, reply, elapsed, err, ttft, _label = ask(base_url, question, opener=opener)
            if ok or attempt >= RETRY:
                break
            # 被限流时等一个完整窗口，其他错误短重试
            time.sleep(RATE_LIMIT_WAIT if is_rate_limited(err) else 3)
        replies.append(reply)
        latencies.append(round(elapsed, 1))
        ttfts.append(round(ttft, 1))
        if not ok:
            error = err or "调用失败"
            break
        if idx == 0:
            time.sleep(MULTI_TURN_GAP)

    second = replies[1] if len(replies) > 1 else ""
    return {
        "id": case["id"],
        "turns": case["turns"],
        "ok": not error,
        "error": error,
        "reply1": replies[0] if replies else "",
        "reply2": second,
        "context_hit": bool(second) and any(kw in second for kw in case["expect_any"]),
        "forbidden_hits": [w for w in forbidden if w in second],
        "elapsed2": latencies[1] if len(latencies) > 1 else 0,
        "ttft2": ttfts[1] if len(ttfts) > 1 else 0,
    }


def summarize_multi(results: list[dict]) -> dict:
    total = len(results)
    ok = [r for r in results if r["ok"]]
    lats = [r["elapsed2"] for r in ok if r["elapsed2"]]
    ttfts = [r["ttft2"] for r in ok if r["ttft2"]]

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "—"

    return {
        "total": total,
        "succeeded": len(ok),
        "failed": total - len(ok),
        "context_ok": sum(1 for r in ok if r["context_hit"]),
        "clean_ok": sum(1 for r in ok if not r["forbidden_hits"]),
        "latency_avg": round(statistics.mean(lats), 1) if lats else 0,
        "ttft_avg": round(statistics.mean(ttfts), 1) if ttfts else 0,
        "context_pct": pct(sum(1 for r in ok if r["context_hit"])),
        "clean_pct": pct(sum(1 for r in ok if not r["forbidden_hits"])),
    }


def write_multi_report(meta: dict, s: dict, results: list[dict]) -> None:
    lines = []
    a = lines.append

    a("# 项目一 · 多轮对话评测报告")
    a("")
    a(f"> 生成时间：{meta['time']}　|　接口：`{meta['base']}`")
    a(f"> 用例组数：{s['total']}（每组 2 句，共 {s['total'] * 2} 次请求）　|　成功：{s['succeeded']}　|　失败：{s['failed']}")
    a("")

    a("## 一、总体指标")
    a("")
    a("| 指标 | 结果 | 说明 |")
    a("|------|------|------|")
    a(f"| **多轮上下文正确率** | **{s['context_pct']}**（{s['context_ok']}/{s['succeeded']}） | 第二轮回复命中该组预期关键词 |")
    a(f"| 回复规范性 | {s['clean_pct']}（{s['clean_ok']}/{s['succeeded']}） | 第二轮未出现禁词 |")
    a(f"| 第二轮平均延迟 | {s['latency_avg']}s | 带上文后的响应耗时 |")
    a(f"| 第二轮平均首字时间 | {s['ttft_avg']}s | 收到第一个字的时间；与上一行接近说明内容在首字时整段到达 |")
    a("")

    a("## 二、逐组明细")
    a("")
    a("| ID | 第一句 | 第二句 | 上下文命中 | 规范性 | 第二轮用时 | 首字时间 | 第二轮回复摘要 |")
    a("|----|-------|-------|-----------|-------|-----------|---------|--------------|")
    for r in results:
        ctx = "✅" if r["context_hit"] else "❌"
        clean = "✅" if (r["ok"] and not r["forbidden_hits"]) else "❌"
        summary = (r["reply2"] or r["error"] or "—")[:60]
        a(f"| {r['id']} | {md_cell(r['turns'][0])} | {md_cell(r['turns'][1])} | {ctx} | {clean} "
          f"| {r['elapsed2']}s | {r['ttft2']}s | {md_cell(summary)} |")
    a("")

    a("## 三、方法与局限")
    a("")
    a("- **会话保持**：每组新建一个 `CookieJar`，同一组两句复用同一个 HttpOnly Cookie，"
      "服务端据此把第二轮接到同一个 `conversation_id` 上。")
    a(f"- **节奏**：组内两句之间、以及相邻两组之间均间隔 {MULTI_TURN_GAP} 秒，避开「单 IP 每分钟 6 次」限流。")
    a("- **判定**：只看第二轮是否命中该组预设关键词（如「那纯棉的呢」必须答纯棉洗护）；第一轮只作上下文铺垫，不单独计分。")
    a("- **局限**：关键词只能证明「还在同一条业务线上」，不能证明指代完全正确"
      "（例如把「1.8 米」错认成「1.5 米」时，只要出现价格仍会判过）。更严格的做法是把断言写成多关键词组合或人工抽检。")
    a("- **失败排查**：某组第二轮答偏时，先确认对应节点的「会话历史」是否开启、对话流与智能体是否都已发布。")
    a("")

    MULTI_REPORT.write_text("\n".join(lines), encoding="utf-8")


def summarize(results: list[dict]) -> dict:
    total = len(results)
    succeeded = [r for r in results if r["ok"]]
    latencies = [r["elapsed"] for r in succeeded]
    ttfts = [r["ttft"] for r in succeeded if r["ttft"]]
    # 首字之后到收完的时间差：接近 0 说明是「整段到达」，不是真流式
    gaps = [r["elapsed"] - r["ttft"] for r in succeeded if r["ttft"]]

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
        "label_count": sum(1 for r in succeeded if r["branch_source"] == "服务端标签"),
        "route_ok": sum(1 for r in results if r["branch_correct"]),
        "keyword_ok": sum(1 for r in results if r["keyword_hit"]),
        "forbidden_ok": sum(1 for r in results if r["ok"] and not r["forbidden_hits"]),
        "length_ok": sum(1 for r in results if r["length_ok"]),
        "latency_avg": round(statistics.mean(latencies), 1) if latencies else 0,
        "latency_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1) if len(latencies) >= 2 else (latencies[0] if latencies else 0),
        "latency_max": max(latencies) if latencies else 0,
        "ttft_avg": round(statistics.mean(ttfts), 1) if ttfts else 0,
        "ttft_p95": round(sorted(ttfts)[int(len(ttfts) * 0.95) - 1], 1) if len(ttfts) >= 2 else (ttfts[0] if ttfts else 0),
        "gap_avg": round(statistics.mean(gaps), 2) if gaps else 0,
        "ttft_count": len(ttfts),
        "per_branch": per_branch,
        "route_pct": pct(sum(1 for r in results if r["branch_correct"])),
        "keyword_pct": pct(sum(1 for r in results if r["keyword_hit"])),
        "forbidden_pct": pct(sum(1 for r in results if r["ok"] and not r["forbidden_hits"])),
        "length_pct": pct(sum(1 for r in results if r["length_ok"])),
    }


def md_cell(text) -> str:
    """转义 Markdown 表格单元格内的管道符与换行，防止回复内容撑破表格。"""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def write_report(meta: dict, s: dict, results: list[dict]) -> None:
    lines = []
    a = lines.append

    a("# 项目一 · v1.7 效果评测报告")
    a("")
    a(f"> 生成时间：{meta['time']}　|　模型：{meta['model']}　|　接口：`{meta['base']}`")
    a(f"> 用例总数：{s['total']}　|　成功返回：{s['succeeded']}　|　失败：{s['failed']}")
    a("")

    a("## 一、总体指标")
    a("")
    a("| 指标 | 结果 | 说明 |")
    a("|------|------|------|")
    a(f"| **意图路由准确率** | **{s['route_pct']}**（{s['route_ok']}/{s['total']}） | 实际分支 == 期望分支；"
      f"其中 {s['label_count']}/{s['succeeded']} 条为服务端真实标签 |")
    a(f"| 关键信息覆盖率 | {s['keyword_pct']}（{s['keyword_ok']}/{s['total']}） | 回复命中该分支的预期关键词 |")
    a(f"| 回复规范性 | {s['forbidden_pct']}（{s['forbidden_ok']}/{s['total']}） | 未出现禁词（冒充人工 / XML 标签 / 代码块） |")
    a(f"| 回复长度合理 | {s['length_pct']}（{s['length_ok']}/{s['total']}） | 长度在 15–800 字之间 |")
    a(f"| 平均响应延迟 | {s['latency_avg']}s | P95 = {s['latency_p95']}s，最长 {s['latency_max']}s |")
    a(f"| **平均首字时间（TTFT）** | **{s['ttft_avg']}s** | P95 = {s['ttft_p95']}s；发出请求到收到第一个字 |")
    a(f"| 首字后收尾耗时 | {s['gap_avg']}s | 总耗时 − 首字时间；接近 0 说明整段一次到达，不是真流式 |")
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
            a(f"| {md_cell(r['id'])} | {md_cell(r['input'])} | {md_cell(r['expect'])} | "
              f"{md_cell(r['branch'] or '—')} | {'✅' if r['keyword_hit'] else '❌'} | "
              f"{md_cell('；'.join(problems))} | {md_cell(reply)} |")
    a("")

    a("## 四、延迟分解（逐条）")
    a("")
    a("首字时间 = 发起请求到收到第一个 delta；生成耗时 = 总耗时 − 首字时间。"
      "两者接近时说明回复是一次性整段到达，用户在这段时间里看不到任何文字。")
    a("")
    a("| ID | 输入 | 首字时间 | 总耗时 | 生成耗时 | 实际分支 |")
    a("|----|------|---------|-------|---------|---------|")
    for r in results:
        if not r["ok"]:
            a(f"| {md_cell(r['id'])} | {md_cell(r['input'])} | — | — | — | 请求失败 |")
            continue
        a(f"| {md_cell(r['id'])} | {md_cell(r['input'])} | {r['ttft']}s | {r['elapsed']}s "
          f"| {round(r['elapsed'] - r['ttft'], 2)}s | {md_cell(r['branch'] or '—')} |")
    a("")

    a("## 五、验收结论")
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
    a(f"- 首字时间：平均 {s['ttft_avg']}s，其中首字之后只剩 {s['gap_avg']}s——"
      "说明时间几乎全部花在「等到第一句话」之前，优化方向是压缩前置链路而不是加快打字。")
    a("")

    a("## 六、评测方法与局限")
    a("")
    branch_order = ["商品咨询", "洗护指南", "物流发货", "退换售后", "优惠活动", "其他兜底"]
    dist = {b: 0 for b in branch_order}
    for r in results:
        dist[r["expect"]] = dist.get(r["expect"], 0) + 1
    dist_text = "、".join(f"{b} {c} 条" for b, c in dist.items() if c)
    a(f"- **数据来源**：`eval/dataset.json`，共 {s['total']} 条（{dist_text}），覆盖常见问法与边界问法（闲聊、议价、投诉）。")
    a("- **调用方式**：逐条请求 `POST /api/chat`，每条用例使用独立 `user_id` 且不复用会话，保证用例之间互不影响。")
    a("- **路由判定**：优先使用代理下发的 `branch` 事件——它来自对话流代码节点对六路输出的判断，是**真实标签**；"
      f"本次 {s['label_count']}/{s['succeeded']} 条拿到了真实标签，未拿到时退回关键词反推，报告里按来源分别统计。")
    a("- **关键词反推**（仅兜底）：按关键词长度加权打分，越长的短语越具体，得分最高者判为该分支。")
    a("- **局限**：关键词反推是代理指标，开场白会同时提到多条业务线，边界问法可能误判；真实标签依赖"
      "结束节点输出 `branch` 字段，若该字段被移除会自动退回反推。")
    a("- **延迟**：包含端到端耗时（浏览器 → 代理 → 扣子 → 模型生成），受网络与模型排队影响，不代表模型自身耗时。")
    a("- **首字时间（TTFT）**：由脚本记录收到第一个 `delta` 事件的时刻，与总耗时分开统计；"
      "若代理未下发任何 `delta`，该条记为 0 且不参与均值。当前链路首字时间 ≈ 总耗时，是架构限制而非测量误差。")
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
    parser = argparse.ArgumentParser(description="项目一 v1.7 效果评测")
    parser.add_argument("--base", default="http://localhost:3000", help="代理服务地址")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--workers", type=int, default=1, help="并发数，默认 1（避免触发单 IP 每分钟限流）")
    parser.add_argument("--multi", action="store_true", help="跑多轮用例：同一会话连发两句，结果写入 eval/report-multi.md")
    args = parser.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    forbidden = data["forbidden"]

    if args.multi:
        groups = data.get("multi_turn_cases", [])
        if args.limit:
            groups = groups[: args.limit]
        if not groups:
            print("dataset.json 里没有 multi_turn_cases，无法跑多轮评测")
            return 1

        print(f"▶ 多轮评测开始：{len(groups)} 组 × 2 句 | 接口 {args.base}")
        print("─" * 62)
        multi_results: list[dict] = []
        for idx, case in enumerate(groups, 1):
            r = run_multi_case(case, args.base, forbidden)
            multi_results.append(r)
            flag = "✅" if (r["ok"] and r["context_hit"]) else "❌"
            print(f"  [{idx}/{len(groups)}] {flag} {r['id']}  {case['turns'][1]:<14} {r['elapsed2']:>5.1f}s"
                  f"  首字 {r['ttft2']:>4.1f}s")
            if idx < len(groups):
                time.sleep(MULTI_TURN_GAP)

        s_multi = summarize_multi(multi_results)
        write_multi_report(
            {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "base": args.base},
            s_multi,
            multi_results,
        )
        print("─" * 62)
        print(f"多轮上下文正确率 : {s_multi['context_pct']}  ({s_multi['context_ok']}/{s_multi['total']})")
        print(f"回复规范性       : {s_multi['clean_pct']}  ({s_multi['clean_ok']}/{s_multi['total']})")
        print(f"第二轮平均延迟   : {s_multi['latency_avg']}s")
        print(f"第二轮平均首字   : {s_multi['ttft_avg']}s")
        print(f"报告已写入       : {MULTI_REPORT}")
        return 0

    cases = data["cases"]
    if args.limit:
        cases = cases[: args.limit]

    branch_keywords = data["branch_keywords"]

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
                     "error": str(exc), "elapsed": 0, "ttft": 0, "reply": "", "branch": "",
                     "branch_source": "", "branch_correct": False,
                     "keyword_hit": False, "forbidden_hits": [], "length_ok": False}
            results.append(r)
            done += 1
            flag = "✅" if (r["ok"] and r["branch_correct"] and r["keyword_hit"]) else "❌"
            print(f"  [{done:>2}/{len(cases)}] {flag} {r['id']:<4} {r['expect']:<6} "
                  f"→ {r['branch'] or r['error']:<8} {r['elapsed']:>5.1f}s  首字 {r['ttft']:>4.1f}s")

    results.sort(key=lambda x: x["id"])
    s = summarize(results)

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
    print(f"平均首字时间   : {s['ttft_avg']}s   (P95 {s['ttft_p95']}s，首字后 {s['gap_avg']}s)")
    print(f"报告已写入     : {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
