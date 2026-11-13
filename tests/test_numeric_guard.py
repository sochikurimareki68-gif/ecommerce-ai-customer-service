# -*- coding: utf-8 -*-
"""数值守卫与占位符守卫的单元测试。

覆盖：
  · 清单与知识库同步、关键指标值
  · 水温/价格/克重/缩水率/物流时效的拦截与放行
  · 流式暂扣：含数字句段到边界才校验，错数字不漏字
  · 占位符：已知标记、未知 [[...]] 标记、流式半截标记
  · _pump_stream 集成：错数字 → 兜底话术 + handoff 事件
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from kb_numeric_guard import build_manifest, find_violation, load_manifest  # noqa: E402
from tests.test_server import FakeSse, _sse_lines  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIR = ROOT / "docs" / "knowledge-base" / "upload"

# 生成脚本不是包，按文件路径加载，用于测试 --check 的差异输出
_spec = importlib.util.spec_from_file_location(
    "gen_kb_numeric_manifest", ROOT / "scripts" / "gen_kb_numeric_manifest.py"
)
gen_manifest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_manifest)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(ROOT / "data" / "kb_numeric_manifest.json")

    def test_manifest_is_in_sync_with_knowledge_base(self):
        self.assertEqual(self.manifest, build_manifest(UPLOAD_DIR))

    def test_manifest_covers_all_knowledge_files(self):
        self.assertEqual(self.manifest["file_count"], 14)
        self.assertEqual(len(self.manifest["categories"]), 14)

    def test_mattress_protector_wash_temperature(self):
        category = next(c for c in self.manifest["categories"] if c["id"] == "05")
        allowed = set(category["shared_metrics"].get("wash_temp_c", {}).get("values", []))
        for product in category["products"]:
            allowed.update(product["metrics"].get("wash_temp_c", {}).get("values", []))
        self.assertIn(40, allowed)
        self.assertNotIn(30, allowed)


class ManifestCheckTest(unittest.TestCase):
    """--check 失败时要能指出是哪个文件、哪个数值不一致。"""

    def setUp(self):
        self.manifest = load_manifest(ROOT / "data" / "kb_numeric_manifest.json")

    def test_identical_manifests_have_no_diff(self):
        self.assertEqual(gen_manifest.diff_manifest(self.manifest, self.manifest), [])

    def test_changed_value_reports_file_and_values(self):
        stale = json.loads(json.dumps(self.manifest))
        category = next(cat for cat in stale["categories"] if cat["id"] == "05")
        category["shared_metrics"]["wash_temp_c"]["values"] = [30, 40]
        lines = gen_manifest.diff_manifest(stale, self.manifest)
        self.assertTrue(
            any(
                "05-床垫保护套系列.md" in line
                and "wash_temp_c" in line
                and "清单 [30, 40] → 知识库 [40]" in line
                for line in lines
            ),
            lines,
        )


class FindViolationTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(ROOT / "data" / "kb_numeric_manifest.json")

    def assert_blocked(self, reply, context, metric, value):
        violation = find_violation(reply, context, self.manifest)
        self.assertIsNotNone(violation, reply)
        self.assertEqual(violation.metric, metric)
        self.assertEqual(violation.value, value)

    def test_blocks_wrong_wash_temperature(self):
        self.assert_blocked("床垫保护套水温 30℃。", "床垫保护套怎么洗", "wash_temp_c", 30)

    def test_allows_knowledge_wash_temperature(self):
        self.assertIsNone(find_violation("床垫保护套水温 40℃。", "床垫保护套怎么洗", self.manifest))

    def test_blocks_wrong_weight(self):
        self.assert_blocked(
            "纯棉防水床垫保护套克重 130g/㎡。", "克重多少", "weight_g_m2", 130
        )

    def test_allows_knowledge_weight(self):
        self.assertIsNone(find_violation(
            "纯棉防水床垫保护套克重 128g/㎡。", "克重多少", self.manifest
        ))

    def test_blocks_wrong_price(self):
        self.assert_blocked("床垫保护套 1.5 米 ¥149。", "床垫保护套多少钱", "price_cny", 149)

    def test_product_price_is_scoped(self):
        self.assert_blocked("暖阳四件套 1.5m ¥259。", "暖阳多少钱", "price_cny", 259)
        self.assertIsNone(find_violation(
            "暖阳四件套 1.5m ¥299。", "暖阳多少钱", self.manifest
        ))

    def test_blocks_wrong_shrink_rate(self):
        self.assert_blocked("四件套缩水率 6%。", "四件套缩水吗", "shrink_pct", 6)

    def test_blocks_invented_ship_hours(self):
        self.assert_blocked("24 小时内发货。", "什么时候发货", "ship_hours", 24)

    def test_allows_knowledge_ship_hours(self):
        self.assertIsNone(find_violation("48 小时内发货。", "什么时候发货", self.manifest))

    def test_skips_promotion_amount(self):
        self.assertIsNone(find_violation("满 300 减 30 元。", "床垫保护套有优惠吗", self.manifest))

    def test_skips_without_category_context(self):
        self.assertIsNone(find_violation("水温 30℃。", "怎么洗", self.manifest))

    def test_other_category_can_explain_the_number(self):
        # 「优惠」会命中活动品类，但云柔四件套的商品价也应当能解释 199，不能误杀
        self.assertIsNone(find_violation(
            "优惠活动：云柔四件套 1.5m ¥199。", "有什么优惠", self.manifest
        ))


class NumericGuardStreamTest(unittest.TestCase):
    # 守卫默认关闭（一致优先），这里显式打开，保证机制本身仍被覆盖。
    def setUp(self):
        self._enabled = server.NUMERIC_GUARD_ENABLED
        server.NUMERIC_GUARD_ENABLED = True

    def tearDown(self):
        server.NUMERIC_GUARD_ENABLED = self._enabled

    def test_plain_text_is_not_buffered(self):
        guard = server.NumericGuardStream("床垫保护套怎么洗")
        self.assertEqual(guard.feed("你好"), ["你好"])
        self.assertEqual(guard.feed("世界"), ["世界"])
        self.assertFalse(guard.blocked)

    def test_wrong_number_is_held_until_boundary(self):
        guard = server.NumericGuardStream("床垫保护套怎么洗")
        self.assertEqual(guard.feed("床垫保护套水温 3"), [])
        out = guard.feed("0℃，下一句")
        self.assertTrue(guard.blocked)
        self.assertEqual(out, [server.NO_ANSWER_REPLY])
        self.assertNotIn("30", "".join(out))
        self.assertEqual(guard.violation.value, 30)

    def test_correct_number_is_released(self):
        guard = server.NumericGuardStream("床垫保护套怎么洗")
        self.assertEqual(guard.feed("床垫保护套水温 40℃。"), ["床垫保护套水温 40℃。"])
        self.assertFalse(guard.blocked)

    def test_disabled_guard_passes_through(self):
        with unittest.mock.patch.object(server, "NUMERIC_GUARD_ENABLED", False):
            guard = server.NumericGuardStream("床垫保护套怎么洗")
            self.assertEqual(guard.feed("水温 30℃"), ["水温 30℃"])


class PlaceholderTest(unittest.TestCase):
    def test_known_marker_replaced(self):
        self.assertEqual(server.replace_no_answer_marker("[[NO_ANSWER]]"), server.NO_ANSWER_REPLY)
        self.assertEqual(
            server.replace_no_answer_marker("[[NO_ANSWER_INVOICE]]"),
            server.NO_ANSWER_INVOICE_REPLY,
        )

    def test_unknown_marker_does_not_leak(self):
        self.assertEqual(server.replace_no_answer_marker("[[UNKNOWN_MARK]]"), server.NO_ANSWER_REPLY)
        self.assertEqual(server.replace_no_answer_marker("答复正文[[UNKNOWN_MARK]]"), "答复正文")

    def test_stream_holds_partial_marker(self):
        self.assertEqual(server.split_no_answer_stream("[[UNK"), ("", "[[UNK"))
        self.assertEqual(server.split_no_answer_stream("你好[[UNK"), ("你好", "[[UNK"))

    def test_stream_drops_complete_unknown_marker(self):
        self.assertEqual(server.split_no_answer_stream("[[UNKNOWN_MARK]]"), ("", "[[UNKNOWN_MARK]]"))
        self.assertEqual(server.split_no_answer_stream("正文[[UNKNOWN_MARK]]"), ("正文", ""))


class PumpStreamGuardTest(unittest.TestCase):
    def setUp(self):
        self._enabled = server.NUMERIC_GUARD_ENABLED
        server.NUMERIC_GUARD_ENABLED = True

    def tearDown(self):
        server.NUMERIC_GUARD_ENABLED = self._enabled

    def _run(self, content: str, message: str, structured: bool = False):
        token, _, _, _ = server.SESSIONS.resolve("")
        fake = FakeSse()
        fake._pump_stream = server.Handler._pump_stream.__get__(fake, FakeSse)
        if structured:
            # 结构化模式：delta 的 content 本身是一段 JSON，攒齐后再拆 reply
            payload = json.dumps({
                "conversation_id": "conv-guard",
                "role": "assistant",
                "type": "answer",
                "content": json.dumps(
                    {"reply": content, "branch": "商品咨询"}, ensure_ascii=False
                ),
            }, ensure_ascii=False)
            resp = _sse_lines(
                ("conversation.message.delta", payload),
                ("done", "[DONE]"),
            )
        else:
            payload = json.dumps({
                "conversation_id": "conv-guard",
                "role": "assistant",
                "type": "answer",
                "content": content,
            }, ensure_ascii=False)
            resp = _sse_lines(
                ("conversation.message.delta", payload),
                ("conversation.message.completed", payload),
                ("done", "[DONE]"),
            )
        try:
            fake._pump_stream(iter(resp), token, None, message)
        finally:
            server.PENDING_HANDOFF.pop(token, None)
            server.SESSIONS.reset(token)
        return fake

    def test_wrong_number_emits_fallback_and_handoff(self):
        fake = self._run("床垫保护套水温 30℃。", "床垫保护套怎么洗")
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual(deltas, [server.NO_ANSWER_REPLY])
        self.assertNotIn("30", "".join(deltas))
        handoffs = [data for event, data in fake.events if event == "handoff"]
        self.assertEqual(len(handoffs), 1)
        self.assertTrue(fake._chat_guard_blocked)
        self.assertEqual(fake._chat_guard_metric, "wash_temp_c")

    def test_correct_number_passes_through(self):
        fake = self._run("床垫保护套水温 40℃。", "床垫保护套怎么洗")
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual(deltas, ["床垫保护套水温 40℃。"])
        self.assertFalse(fake._chat_guard_blocked)
        self.assertEqual([e for e, _ in fake.events if e == "handoff"], [])

    def test_structured_wrong_number_is_guarded(self):
        fake = self._run("床垫保护套水温 30℃。", "床垫保护套怎么洗", structured=True)
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual(deltas, [server.NO_ANSWER_REPLY])
        self.assertTrue(fake._chat_guard_blocked)

    def test_structured_correct_reply_is_not_duplicated(self):
        fake = self._run("床垫保护套水温 40℃。", "床垫保护套怎么洗", structured=True)
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual("".join(deltas), "床垫保护套水温 40℃。")

    def test_unknown_placeholder_emits_fallback(self):
        fake = self._run("[[UNKNOWN_MARK]]", "你好")
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual(deltas, [server.NO_ANSWER_REPLY])
        self.assertNotIn("UNKNOWN_MARK", "".join(deltas))

    def test_partial_placeholder_is_dropped(self):
        fake = self._run("你好[[UNK", "你好")
        deltas = [json.loads(data)["content"] for event, data in fake.events if event == "delta"]
        self.assertEqual("".join(deltas), "你好")


if __name__ == "__main__":
    unittest.main()
