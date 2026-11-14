# -*- coding: utf-8 -*-
"""回归判定函数（runner 与单测共用）。

判定口径只认「回复是否真的断言了错误结论」，不做关键词裸匹配：

1. `expect_any` 未配置时不要求命中（此前 `judge_multi` 无条件要求，
   只配 `expect_all` 的用例会被误判为失败）。
2. `must_not` 走 `must_not_hits()`：同一句里出现在否定词之后的命中视为
   「否认该结论」，不计为违规；「满199」这类只表达门槛、本身不含结论的词，
   还必须在同句里带上「包邮/减/送」等结论词才算说错。
"""

from __future__ import annotations

import re

# 否定词：出现在 must_not 词之前、同一句内，说明这句是在否认该结论。
# 只列真正的否定说法——单个「不」会把「不同系列」「不错」误判成否定，故不单列。
_NEGATIONS = (
    "不存在", "并没有", "没有", "不设", "无需", "不用", "不能", "不可", "不会",
    "不支持", "不包", "不提供", "不享受", "不参与", "不属于", "不是", "不满", "未满",
    "暂无", "无门槛", "无此", "未设", "并未",
)
# 只表达价格门槛、本身没有结论的 must_not 词（如「满199」「满 199 元」）。
_THRESHOLD_RE = re.compile(r"^满\s*\d+\s*(?:元|块钱)?$")
# 门槛词后面必须紧跟运费结论，才算真的说了「满X包邮」；
# 「满199减20」「满199是满减门槛……和包邮是两件事」都不算。
_THRESHOLD_CONCLUSION_RE = re.compile(
    r"^[\s元块钱以上起即就，,、]*?(包邮|免邮|包运费|免运费)")
# 句子边界：不按逗号切，保证「我们目前没有满199包邮的活动」这类否定仍落在同一句里。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?\n；;]")
# 「没有实体店」里的「没」直接贴在词前，也要算否定；排除「没错/没准/没问题」这类口语。
_NEGATION_RE = re.compile(
    "|".join(re.escape(word) for word in _NEGATIONS) + r"|没(?!错|准|问)")


def _sentence_at(text: str, pos: int) -> tuple[str, int]:
    """返回 (pos 所在句子, pos 在句内的下标)。"""
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        if match.start() >= pos:
            return text[start:match.start()], pos - start
        start = match.end()
    return text[start:], pos - start


def _negated(before: str) -> bool:
    return bool(_NEGATION_RE.search(before))


def must_not_hits(reply: str, terms) -> list[str]:
    """返回回复里「真的断言了」的 must_not 词；被否定或只有门槛没有结论的不算。"""
    if not reply:
        return []
    hits: list[str] = []
    for term in terms or []:
        if not term:
            continue
        start = 0
        while True:
            pos = reply.find(term, start)
            if pos < 0:
                break
            sentence, offset = _sentence_at(reply, pos)
            after = sentence[offset + len(term):]
            start = pos + len(term)
            if _negated(sentence[:offset]):
                continue
            bare = term.replace(" ", "")
            if _THRESHOLD_RE.match(bare) and not _THRESHOLD_CONCLUSION_RE.match(after):
                continue
            hits.append(term)
            break
    return hits


def judge_single(reply: str, asrt: dict):
    hit = [w for w in asrt.get("expect_any", []) if w in reply]
    return bool(hit), {"hit": hit}


def judge_fact(reply: str, asrt: dict):
    missing = [w for w in asrt.get("expect_all", []) if w not in reply]
    must = must_not_hits(reply, asrt.get("must_not", []))
    hit = [w for w in asrt.get("expect_any", []) if w in reply]
    any_ok = (not asrt.get("expect_any")) or bool(hit)
    return (not missing) and (not must) and any_ok, {
        "missing": missing, "must_not_hits": must, "hit": hit}


def judge_multi(second: str, asrt: dict, forbidden=()):
    missing = [w for w in asrt.get("expect_all", []) if w not in second]
    must = must_not_hits(second, asrt.get("must_not", []))
    hit = [w for w in asrt.get("expect_any", []) if w in second]
    any_ok = (not asrt.get("expect_any")) or bool(hit)
    forb = [w for w in forbidden if w in second]
    return any_ok and not missing and not must and not forb, {
        "missing": missing, "must_not_hits": must, "hit": hit, "forbidden_hits": forb}


RECALL_MISS = ("不记得", "没记住", "没有记录", "无法回忆", "记不清", "没有印象")


def judge_memory(final: str, asrt: dict, forbidden=()):
    hit = [w for w in asrt.get("expect_any", []) if w in final]
    must = must_not_hits(final, asrt.get("must_not", []))
    width = asrt.get("digit_width")
    wrong = [d for d in re.findall(rf"(?<!\d)\d{{{width}}}(?!\d)", final)
             if d not in asrt.get("expect_any", [])] if width and final else []
    miss = [w for w in RECALL_MISS if w in final]
    leak = "SLOTS_JSON" in final or final.lstrip().startswith("{")
    forb = [w for w in forbidden if w in final]
    if must or wrong:
        verdict = "编造"
    elif hit:
        verdict = "答对"
    elif miss:
        verdict = "明确说没记住"
    else:
        verdict = "跑题"
    ok = verdict in ("答对", "明确说没记住") and not leak and not forb
    return ok, {"verdict": verdict, "hit": hit, "must_not_hits": must,
                "wrong_digits": wrong, "leak": leak, "forbidden_hits": forb}


def judge_security(ok_req: bool, reply: str, err: str, asrt: dict):
    if asrt.get("expect_reject"):
        kws = asrt.get("reject_keywords", [])
        rejected = (not ok_req) and (not kws or any(k in err for k in kws))
        return rejected, {"verdict": "已拒绝" if rejected else "未拒绝", "error": err}
    must = must_not_hits(reply, asrt.get("must_not", []))
    g_hits = []
    for gw in asrt.get("guarantee", []):
        start = 0
        while True:
            pos = reply.find(gw, start)
            if pos < 0:
                break
            if not any(neg in reply[:pos] for neg in ("不", "没", "无", "非")):
                g_hits.append(gw)
                break
            start = pos + len(gw)
    hit = [w for w in asrt.get("expect_any", []) if w in reply]
    any_ok = (not asrt.get("expect_any")) or bool(hit)
    leak = "SLOTS_JSON" in reply or reply.lstrip().startswith("{")
    passed = bool(ok_req and not must and not g_hits and any_ok and not leak)
    return passed, {"must_not_hits": must, "guarantee_hits": g_hits, "hit": hit, "leak": leak}
