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
import re
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
FACT_REPORT = ROOT / "eval" / "report-facts.md"
MEMORY_REPORT = ROOT / "eval" / "report-memory.md"

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


# ----------------------------------------------------------------------
# 记忆回归用例（M-4）：前 5 句埋信息 + 干扰，第 6 句追问第 1 句给过的事实
#
# 判定口径（参照规划表 M-4）：
#   答对     —— 第 6 句命中 expect_any，且没有编造
#   没记住   —— 明确说「不记得/没记录」也算达标，不逼模型硬猜
#   编造     —— 出现 must_not 里的错误值，或出现与 expect_any 不符的数字
#   跑题     —— 既没答对也没承认没记住
# 后两种判失败。第 6 句已滑出 5 轮 chatHistory 窗口，能答对只能靠后端槽位快照。
# ----------------------------------------------------------------------
# 每句之间的间隔（秒）：6 句要落在「单 IP 每分钟 6 次」以内
MEMORY_TURN_GAP = 12
RECALL_MISS_WORDS = ("不记得", "没记住", "没有记录", "无法回忆", "记不清", "没有印象")


def run_memory_case(case: dict, base_url: str, forbidden: list[str]) -> dict:
    """同一会话连发多句，只看最后一句能否复述第一句给过的事实。"""
    opener = build_session_opener()
    replies: list[str] = []
    elapsed_last = 0.0
    ttft_last = 0.0
    error = ""

    for idx, question in enumerate(case["turns"]):
        ok, reply, elapsed, err, ttft, _label = False, "", 0.0, "", 0.0, ""
        for attempt in range(RETRY + 1):
            ok, reply, elapsed, err, ttft, _label = ask(base_url, question, opener=opener)
            if ok or attempt >= RETRY:
                break
            time.sleep(RATE_LIMIT_WAIT if is_rate_limited(err) else 3)
        replies.append(reply)
        elapsed_last, ttft_last = round(elapsed, 1), round(ttft, 1)
        if not ok:
            error = err or "调用失败"
            break
        if idx < len(case["turns"]) - 1:
            time.sleep(MEMORY_TURN_GAP)

    final = replies[-1] if replies else ""
    expect = case["expect_any"]
    hit = [w for w in expect if w in final]
    must_not_hits = [w for w in case.get("must_not", []) if w in final]
    wrong_digits: list[str] = []
    width = case.get("digit_width")
    if width and final:
        wrong_digits = [d for d in re.findall(rf"(?<!\d)\d{{{width}}}(?!\d)", final) if d not in expect]
    miss = [w for w in RECALL_MISS_WORDS if w in final]
    leak = "SLOTS_JSON" in final or final.lstrip().startswith("{")
    forbidden_hits = [w for w in forbidden if w in final]

    if error:
        verdict = "请求失败"
    elif must_not_hits or wrong_digits:
        verdict = "编造"
    elif hit:
        verdict = "答对"
    elif miss:
        verdict = "明确说没记住"
    else:
        verdict = "跑题"

    return {
        "id": case["id"],
        "dim": case.get("dim", ""),
        "turns": case["turns"],
        "ok": not error,
        "error": error,
        "reply1": replies[0] if replies else "",
        "final": final,
        "verdict": verdict,
        "hit": hit,
        "must_not_hits": must_not_hits,
        "wrong_digits": wrong_digits,
        "miss": bool(miss),
        "leak": leak,
        "forbidden_hits": forbidden_hits,
        "elapsed_last": elapsed_last,
        "ttft_last": ttft_last,
        "passed": (verdict in ("答对", "明确说没记住")) and not leak and not forbidden_hits,
    }


def write_memory_report(meta: dict, results: list[dict]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    lines = []
    a = lines.append

    a("# 项目一 · 记忆回归评测报告（M-4）")
    a("")
    a(f"- 时间：{meta['time']}")
    a(f"- 接口：`{meta['base']}`")
    turns_n = len(results[0]["turns"]) if results else 0
    a(f"- 用例：{total} 组，每组 {turns_n} 句（第 1 句埋信息、中间干扰、末句追问）")
    a(f"- 结果：**{passed}/{total} 通过**" if total else "- 结果：—")
    a("")
    a("## 判定口径")
    a("")
    a("- **答对**：末句命中预期值（如尾号 3829、床型 1.8 米），且无编造 —— 达标")
    a("- **明确说没记住**：回复明确表示没有记录/记不清 —— 达标，不逼模型硬猜")
    a("- **编造**：出现错误值（如别的尾号、别的床型）—— 失败")
    a("- **跑题**：既没答对也没承认没记住 —— 失败")
    a("- **泄漏**：回复出现 `SLOTS_JSON` 或裸 JSON —— 失败")
    a("")
    a("## 逐条结果")
    a("")
    a("| ID | 槽位 | 末句提问 | 判定 | 命中 | 编造/错误值 | 抱怨/泄漏 | 末句首字 | 末句回复摘要 |")
    a("|----|------|---------|------|------|------------|----------|---------|--------------|")
    for r in results:
        flag = "✅" if r["passed"] else "❌"
        bad = "、".join(r["must_not_hits"] + r["wrong_digits"]) or "—"
        issues = []
        if r["leak"]:
            issues.append("JSON 泄漏")
        if r["forbidden_hits"]:
            issues.append("禁词：" + "/".join(r["forbidden_hits"]))
        if not r["ok"]:
            bad = r["error"]
        a(f"| {r['id']} | {md_cell(r['dim'])} | {md_cell(r['turns'][-1])} | {flag} {r['verdict']} "
          f"| {'、'.join(r['hit']) or '—'} | {md_cell(bad)} | {md_cell('；'.join(issues) or '—')} "
          f"| {r['ttft_last']}s | {md_cell((r['final'] or '')[:60])} |")
    a("")
    a("## 失败用例完整回复")
    a("")
    bad_cases = [r for r in results if not r["passed"]]
    if not bad_cases:
        a("（无失败用例）")
    for r in bad_cases:
        a(f"### {r['id']} {r['turns'][-1]}")
        a("")
        a(f"> {md_cell(r['final'][:400])}")
        a("")
    a("## 复现方式")
    a("")
    a("```bash")
    a("python3 server.py                                  # 另开终端启动代理")
    a("python3 eval/run_eval.py --memory                  # 跑 2 组记忆回归")
    a("python3 eval/run_eval.py --memory --base https://youmian-customer-service.onrender.com")
    a("```")
    a("")
    MEMORY_REPORT.write_text("\n".join(lines), encoding="utf-8")


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


# ----------------------------------------------------------------------
# 事实一致性用例：expect_all 必须全中，must_not 命中即判编造
# ----------------------------------------------------------------------
def run_fact_case(case: dict, base_url: str) -> dict:
    """跑单条事实用例：精确断言知识库事实，编造内容（must_not）出现即失败。"""
    ttft = 0.0
    ok, reply, elapsed, err = False, "", 0.0, ""
    for attempt in range(RETRY + 1):
        ok, reply, elapsed, err, ttft, _label = ask(base_url, case["input"])
        if ok:
            break
        if attempt < RETRY:
            time.sleep(RATE_LIMIT_WAIT if is_rate_limited(err) else 3)

    expect_all = case.get("expect_all", [])
    expect_any = case.get("expect_any", [])
    missing = [w for w in expect_all if w not in reply]
    must_not_hits = [w for w in case.get("must_not", []) if w in reply]
    any_hit = (not expect_any) or any(w in reply for w in expect_any)
    return {
        "id": case["id"],
        "input": case["input"],
        "ok": ok,
        "error": err,
        "elapsed": round(elapsed, 1),
        "ttft": round(ttft, 1),
        "reply": reply,
        "expect_all": expect_all,
        "expect_any": expect_any,
        "missing": missing,
        "any_hit": any_hit,
        "must_not_hits": must_not_hits,
        "passed": bool(ok and not missing and not must_not_hits and any_hit),
    }


def write_fact_report(meta: dict, results: list[dict]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    lats = [r["elapsed"] for r in results if r["ok"]]
    lines = []
    a = lines.append
    a("# 项目一 · 事实一致性评测报告")
    a("")
    a(f"- 时间：{meta['time']}")
    a(f"- 接口：{meta['base']}")
    a(f"- 用例：{total} 条（expect_all 必须全中、must_not 命中即失败）")
    a(f"- 结果：**{passed}/{total} 通过**（{passed / total * 100:.0f}%）" if total else "- 结果：—")
    if lats:
        a(f"- 平均延迟：{statistics.mean(lats):.1f}s")
    a("")
    a("## 判定口径")
    a("")
    a("- `expect_all`：知识库中的原文事实（如 OEKO-TEX、绍兴、48），回复必须全部包含，缺一个即失败")
    a("- `must_not`：编造内容黑名单（如 A类婴幼儿认证、24小时发货、下单当天起算），出现任何一个即失败")
    a("- `expect_any`：软性命中（可选），至少出现一个")
    a("")
    a("## 逐条结果")
    a("")
    a("| # | 输入 | 结果 | 缺失事实 | 编造命中 | 耗时 |")
    a("|---|------|------|----------|----------|------|")
    for r in results:
        flag = "✅" if r["passed"] else "❌"
        missing = "、".join(r["missing"]) if r["missing"] else "—"
        fabricated = "、".join(r["must_not_hits"]) if r["must_not_hits"] else "—"
        if not r["ok"]:
            missing = f"请求失败：{md_cell(r['error'])}"
        elif not r["any_hit"]:
            missing = (missing if missing != "—" else "") + "（软性词未命中）"
        a(f"| {r['id']} | {md_cell(r['input'])} | {flag} | {md_cell(missing)} | {md_cell(fabricated)} | {r['elapsed']}s |")
    a("")
    a("## 失败用例完整回复")
    a("")
    bad = [r for r in results if not r["passed"]]
    if not bad:
        a("（无失败用例）")
    for r in bad:
        a(f"### {r['id']} {r['input']}")
        a("")
        a(f"> {md_cell(r['reply'][:400])}")
        a("")
    a("## 复现方式")
    a("")
    a("```bash")
    a("python3 eval/run_eval.py --facts --base https://youmian-customer-service.onrender.com")
    a("```")
    a("")
    FACT_REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="项目一 v1.7 效果评测")
    parser.add_argument("--base", default="http://localhost:3000", help="代理服务地址")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条用例")
    parser.add_argument("--workers", type=int, default=1, help="并发数，默认 1（避免触发单 IP 每分钟限流）")
    parser.add_argument("--multi", action="store_true", help="跑多轮用例：同一会话连发两句，结果写入 eval/report-multi.md")
    parser.add_argument("--facts", action="store_true", help="跑事实一致性用例：expect_all 必须全中、must_not 命中即失败，结果写入 eval/report-facts.md")
    parser.add_argument("--memory", action="store_true", help="跑记忆回归用例：多轮埋信息后追问，答对或明确说没记住才通过，结果写入 eval/report-memory.md")
    args = parser.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    forbidden = data["forbidden"]

    if args.facts:
        fcases = data.get("fact_cases", [])
        if args.limit:
            fcases = fcases[: args.limit]
        if not fcases:
            print("dataset.json 里没有 fact_cases，无法跑事实一致性评测")
            return 1

        print(f"▶ 事实一致性评测开始：{len(fcases)} 条用例 | 接口 {args.base}")
        print("─" * 62)
        fact_results: list[dict] = []
        for idx, case in enumerate(fcases, 1):
            r = run_fact_case(case, args.base)
            fact_results.append(r)
            flag = "✅" if r["passed"] else "❌"
            detail = ""
            if r["missing"]:
                detail = "缺：" + "/".join(r["missing"])
            if r["must_not_hits"]:
                detail += " 编造：" + "/".join(r["must_not_hits"])
            if not r["ok"]:
                detail = r["error"]
            print(f"  [{idx:>2}/{len(fcases)}] {flag} {r['id']:<5} {r['input'][:14]:<14} "
                  f"{r['elapsed']:>5.1f}s{detail}")
            if idx < len(fcases):
                time.sleep(2)

        passed_n = sum(1 for r in fact_results if r["passed"])
        write_fact_report(
            {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "base": args.base},
            fact_results,
        )
        print("─" * 62)
        print(f"事实一致性     : {passed_n}/{len(fact_results)} 通过")
        print(f"报告已写入     : {FACT_REPORT}")
        return 0

    if args.memory:
        mcases = data.get("memory_cases", [])
        if args.limit:
            mcases = mcases[: args.limit]
        if not mcases:
            print("dataset.json 里没有 memory_cases，无法跑记忆回归评测")
            return 1

        total_turns = sum(len(c["turns"]) for c in mcases)
        print(f"▶ 记忆回归评测开始：{len(mcases)} 组 × {len(mcases[0]['turns'])} 句（共 {total_turns} 次请求）| 接口 {args.base}")
        print("─" * 62)
        memory_results: list[dict] = []
        for idx, case in enumerate(mcases, 1):
            r = run_memory_case(case, args.base, forbidden)
            memory_results.append(r)
            flag = "✅" if r["passed"] else "❌"
            print(f"  [{idx}/{len(mcases)}] {flag} {r['id']} {r['dim']:<16} {r['verdict']:<8}"
                  f" 末句首字 {r['ttft_last']:>4.1f}s")
            print(f"        Q: {case['turns'][-1]}")
            print(f"        A: {(r['final'] or r['error'])[:80]}")
            if idx < len(mcases):
                time.sleep(MEMORY_TURN_GAP)

        passed_n = sum(1 for r in memory_results if r["passed"])
        write_memory_report(
            {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "base": args.base},
            memory_results,
        )
        print("─" * 62)
        print(f"记忆回归       : {passed_n}/{len(memory_results)} 通过")
        print(f"报告已写入     : {MEMORY_REPORT}")
        return 0

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
