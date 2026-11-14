# -*- coding: utf-8 -*-
"""价格前置查数与流式收尾判空的单元测试（标准库 unittest）。

覆盖：
  · 命中：款 + 规格都能定位时，注入准确价格
  · 未命中：款不在表里 / 规格缺失 / 该档标「无」/ 非价格问法 / 对比活动类问法
  · 注入快照不出现在前端可见文本（模型回显时剥除）
  · 连续两轮问价各带各的数字，不串款
  · 流式收尾只收到空白时走兜底，不吐空气泡
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from kb_price_lookup import build_price_snapshot, load_price_table, match_price  # noqa: E402
from tests.test_server import FakeSse, _sse_lines  # noqa: E402


class PriceTableMatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = load_price_table()

    def test_hit_returns_exact_price(self):
        hit = match_price("暖阳四件套1.8米多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["price"], 349)
        self.assertEqual(hit["spec"], "1.8m")

    def test_hit_support_colloquial_spec(self):
        hit = match_price("暖阳1米8多少钱", self.table)
        self.assertEqual(hit["price"], 349)

    def test_hit_support_quilt_size(self):
        hit = match_price("夏凉被200×230多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["price"], 199)

    def test_unknown_product_is_not_injected(self):
        self.assertIsNone(match_price("云舒四件套1.8米多少钱", self.table))

    def test_missing_spec_injects_all_priced_specs(self):
        hit = match_price("云柔四件套多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["specs"], {"1.2m": 199, "1.5m": 259, "1.8m": 299})
        snapshot = build_price_snapshot(hit)
        for price in ("199", "259", "299"):
            self.assertIn(price, snapshot)
        # 表里没有的档不能出现（云柔没有 2.0m）
        self.assertNotIn("2.0m", snapshot)

    def test_absent_spec_is_not_injected(self):
        # 暖阳没有 1.2m，表里标「无」，不能就近拿 1.5m 顶替
        self.assertIsNone(match_price("暖阳四件套1.2米多少钱", self.table))

    def test_non_price_question_is_not_injected(self):
        self.assertIsNone(match_price("暖阳四件套1.8米是什么面料", self.table))

    def test_comparison_or_promo_question_is_not_injected(self):
        self.assertIsNone(match_price("暖阳四件套1.8米和云柔比哪个便宜", self.table))
        self.assertIsNone(match_price("暖阳四件套1.8米满减后多少钱", self.table))

    def test_snapshot_has_no_user_facing_price_wording(self):
        hit = match_price("暖阳四件套1.8米多少钱", self.table)
        snapshot = build_price_snapshot(hit)
        self.assertIn("349", snapshot)
        self.assertNotIn("参考价", snapshot)
        self.assertTrue(snapshot.startswith("[PRICE_REF]"))


class PriceHintSwitchTest(unittest.TestCase):
    def setUp(self):
        self._enabled = server.PRICE_HINT_ENABLED

    def tearDown(self):
        server.PRICE_HINT_ENABLED = self._enabled

    def test_switch_off_disables_injection(self):
        server.PRICE_HINT_ENABLED = False
        self.assertIsNone(server.lookup_price_hint("暖阳四件套1.8米多少钱"))

    def test_switch_on_injects(self):
        server.PRICE_HINT_ENABLED = True
        hit = server.lookup_price_hint("暖阳四件套1.8米多少钱")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["price"], 349)
        self.assertIn("349", hit["snapshot"])

    def test_server_lookup_passes_recent_context(self):
        server.PRICE_HINT_ENABLED = True
        hit = server.lookup_price_hint("1.8米多少钱", ["暖阳四件套多少钱"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["price"], 349)


class ConsecutivePriceHintTest(unittest.TestCase):
    def setUp(self):
        self._enabled = server.PRICE_HINT_ENABLED
        server.PRICE_HINT_ENABLED = True

    def tearDown(self):
        server.PRICE_HINT_ENABLED = self._enabled

    def test_two_turns_do_not_cross(self):
        first = server.lookup_price_hint("云柔四件套1.8米多少钱")
        second = server.lookup_price_hint("暖阳四件套1.8米多少钱")
        self.assertIn("299", first["snapshot"])
        self.assertNotIn("349", first["snapshot"])
        self.assertIn("349", second["snapshot"])
        self.assertNotIn("299", second["snapshot"])


class CategoryPriceHintTest(unittest.TestCase):
    """品类 + 规格明确、款名未定：给该品类该规格所有款，价格升序，不串其他规格。"""

    @classmethod
    def setUpClass(cls):
        cls.table = load_price_table()

    def test_category_and_spec_injects_all_products(self):
        hit = match_price("四件套1.8米多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["category"], "四件套")
        self.assertEqual(hit["spec"], "1.8m")
        prices = dict(hit["prices"])
        # 四件套 1.8m 全部在售款都要出现
        self.assertEqual(prices, {
            "云柔·60支纯棉四件套": 299,
            "暖阳·磨毛纯棉四件套": 349,
            "法兰绒·加厚保暖四件套": 379,
            "清风·天丝四件套": 459,
            "亲肤·有机棉四件套": 499,
            "云裳·100支长绒棉四件套": 599,
            "尊享·100支埃及长绒棉四件套": 899,
        })

    def test_category_injection_sorted_by_price(self):
        hit = match_price("四件套1.8米多少钱", self.table)
        values = [price for _, price in hit["prices"]]
        self.assertEqual(values, sorted(values))

    def test_category_injection_does_not_mix_other_specs(self):
        hit = match_price("四件套1.8米多少钱", self.table)
        snapshot = build_price_snapshot(hit)
        # 199（云柔 1.2m）、399（清风 1.5m）、259（云柔 1.5m）都是别的规格
        for other in ("199", "399", "259"):
            self.assertNotIn(other, snapshot)
        self.assertNotIn("1.2m", snapshot)
        self.assertNotIn("1.5m", snapshot)

    def test_category_from_context_second_turn(self):
        # M02 原问法：第一轮只问品类尺寸，第二轮省略主语
        hit = match_price("1.8米多少钱", self.table, ["四件套有什么尺寸？"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["category"], "四件套")
        prices = dict(hit["prices"])
        self.assertEqual(prices["云柔·60支纯棉四件套"], 299)
        self.assertEqual(prices["暖阳·磨毛纯棉四件套"], 349)

    def test_category_injection_scope_text(self):
        hit = match_price("四件套1.8米多少钱", self.table)
        snapshot = build_price_snapshot(hit)
        self.assertIn("不要罗列其他规格", snapshot)
        self.assertIn("不要推荐", snapshot)

    def test_quilt_category_uses_quilt_sizes_only(self):
        hit = match_price("被芯200×230多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["category"], "被芯")
        self.assertEqual([p for _, p in hit["prices"]], [199, 249, 699, 999, 1099])

    def test_mattress_protector_category(self):
        hit = match_price("床垫保护套1.8米多少钱", self.table)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["category"], "床垫保护套")
        self.assertEqual(sorted(p for _, p in hit["prices"]), [199, 199, 249])

    def test_unknown_product_name_is_not_replaced_by_category(self):
        # 点名了表里没有的款，不能拿整个品类顶替
        self.assertIsNone(match_price("云舒四件套1.8米多少钱", self.table))

    def test_category_without_spec_is_not_injected(self):
        self.assertIsNone(match_price("四件套多少钱", self.table))

    def test_explicit_product_still_wins_over_category(self):
        hit = match_price("暖阳四件套1.8米多少钱", self.table)
        self.assertEqual(hit["price"], 349)
        self.assertNotIn("prices", hit)


class ContextPriceHintTest(unittest.TestCase):
    """多轮省略问法：只从现有会话的用户原话补主语，不猜、不串款。"""

    @classmethod
    def setUpClass(cls):
        cls.table = load_price_table()

    def test_ellipsis_uses_previous_product(self):
        hit = match_price("1.8米多少钱", self.table, ["暖阳四件套多少钱"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["price"], 349)

    def test_ellipsis_uses_latest_product_after_switch(self):
        context = ["暖阳四件套多少钱", "云柔四件套多少钱"]
        hit = match_price("1.8米多少钱", self.table, context)
        self.assertEqual(hit["price"], 299)
        self.assertEqual(hit["product"], "云柔·60支纯棉四件套")
        self.assertNotEqual(hit["price"], 349)   # 不能沿用上一款的 349

    def test_explicit_product_beats_context(self):
        hit = match_price("暖阳1.8米多少钱", self.table, ["云柔四件套多少钱"])
        self.assertEqual(hit["price"], 349)

    def test_no_context_no_injection(self):
        self.assertIsNone(match_price("1.8米多少钱", self.table, []))

    def test_ambiguous_context_no_injection(self):
        # 最近一条同时点名两款，指代不明，不能跳过它去用更早的款
        self.assertIsNone(match_price("1.8米多少钱", self.table, ["暖阳四件套和云柔四件套"]))

    def test_absent_spec_with_context_still_not_injected(self):
        self.assertIsNone(match_price("1.2米多少钱", self.table, ["暖阳四件套多少钱"]))

    def test_cross_product_prices_do_not_mix(self):
        soft = match_price("云柔四件套多少钱", self.table)
        warm = match_price("暖阳四件套多少钱", self.table)
        self.assertEqual(soft["specs"], {"1.2m": 199, "1.5m": 259, "1.8m": 299})
        self.assertEqual(warm["specs"], {"1.5m": 299, "1.8m": 349})  # 无 1.2m
        self.assertNotIn("1.2m", warm["specs"])


class PriceSnapshotEchoTest(unittest.TestCase):
    def test_snapshot_echo_is_stripped_from_reply(self):
        """模型把 [PRICE_REF] 快照原样回显时，不能出现在前端可见文本。"""
        fake = FakeSse()
        echoed = build_price_snapshot(
            {"product": "暖阳·磨毛纯棉四件套", "spec": "1.8m", "price": 349}
        )
        part1 = json.dumps({"conversation_id": "c1", "role": "assistant", "type": "answer",
                            "content": "亲，暖阳四件套1.8米是349元。\n" + echoed[:12]},
                           ensure_ascii=False)
        part2 = json.dumps({"conversation_id": "c1", "role": "assistant", "type": "answer",
                            "content": echoed[12:]}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", part1),
            ("conversation.message.delta", part2),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        deltas = [json.loads(d)["content"] for e, d in fake.events if e == "delta"]
        visible = "".join(deltas)
        self.assertIn("349元", visible)
        self.assertNotIn("PRICE_REF", visible)
        self.assertNotIn("scope", visible)


class EmptyWhitespaceTailTest(unittest.TestCase):
    def test_whitespace_only_stream_tail_falls_back(self):
        """上游只吐一个换行时，不能把空白 emit 给前端。"""
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "assistant",
                              "type": "answer", "content": "\n"}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", payload),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        deltas = [json.loads(d)["content"] for e, d in fake.events if e == "delta"]
        self.assertEqual("".join(deltas), server.NO_ANSWER_REPLY)


if __name__ == "__main__":
    unittest.main()
