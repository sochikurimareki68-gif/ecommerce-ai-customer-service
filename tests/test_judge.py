# -*- coding: utf-8 -*-
"""判定函数单测：expect_any 可选、must_not 语义匹配（标准库 unittest）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.judge import judge_fact, judge_multi, must_not_hits  # noqa: E402


class JudgeMultiExpectAnyTest(unittest.TestCase):
    def test_multi_without_expect_any_only_checks_expect_all(self):
        # M07 口径：只配 expect_all，命中 349 即通过
        asrt = {"expect_all": ["349"], "must_not": ["399", "459"]}
        passed, detail = judge_multi("暖阳 1.8 米是 349 元。", asrt)
        self.assertTrue(passed)
        self.assertEqual(detail["hit"], [])

    def test_multi_without_expect_any_still_fails_on_must_not(self):
        asrt = {"expect_all": ["349"], "must_not": ["399"]}
        passed, _ = judge_multi("暖阳 1.8 米是 349 元，和 399 差不多。", asrt)
        self.assertFalse(passed)

    def test_multi_with_expect_any_still_requires_one_hit(self):
        asrt = {"expect_all": ["349"], "expect_any": ["暖阳"]}
        passed, _ = judge_multi("1.8 米是 349 元。", asrt)
        self.assertFalse(passed)
        passed, _ = judge_multi("暖阳 1.8 米是 349 元。", asrt)
        self.assertTrue(passed)


class MustNotSemanticTest(unittest.TestCase):
    def test_negated_threshold_is_not_a_hit(self):
        # FC04 原始误伤句：否定式正确回答不能命中 must_not 的「满199」
        reply = "目前不存在满 199 包邮的活动，店铺是不设金额门槛包邮的。"
        self.assertEqual(
            must_not_hits(reply, ["满199", "满 199", "199 元包邮"]), [])

    def test_affirmative_threshold_with_conclusion_is_a_hit(self):
        reply = "亲，满 199 包邮的，可以放心下单。"
        self.assertEqual(must_not_hits(reply, ["满 199"]), ["满 199"])

    def test_bare_threshold_without_conclusion_is_not_a_hit(self):
        # 只是提到门槛、没给出包邮结论，不算错误报价
        reply = "满 199 是活动门槛，具体以活动页为准。"
        self.assertEqual(must_not_hits(reply, ["满 199"]), [])

    def test_full_reduction_is_not_shipping_claim(self):
        # 满减活动常写成「满199减20」，不能被当成「满199包邮」
        reply = "不设门槛包邮；满 199 减 20 是满减活动，两者不是一回事。"
        self.assertEqual(must_not_hits(reply, ["满 199"]), [])

    def test_unrelated_words_starting_with_bu_do_not_negate(self):
        # 「不同系列」只是普通措辞，不能当成否定，否则真编造会漏判
        reply = "不同系列1.8米四件套价格：云柔款399元，暖阳款459元。"
        self.assertEqual(must_not_hits(reply, ["399"]), ["399"])

    def test_negated_product_claim_is_not_a_hit(self):
        reply = "我们目前没有实体店，只有线上店铺。"
        self.assertEqual(must_not_hits(reply, ["有实体店"]), [])

    def test_affirmative_product_claim_is_a_hit(self):
        reply = "我们在杭州有实体店的。"
        self.assertEqual(must_not_hits(reply, ["有实体店"]), ["有实体店"])

    def test_term_containing_negation_still_hits(self):
        # 「不支持叠加」本身含否定词，但前面没有否定，仍是错误口径
        reply = "满减和优惠券不支持叠加哦。"
        self.assertEqual(must_not_hits(reply, ["不支持叠加"]), ["不支持叠加"])

    def test_judge_fact_passes_negated_correct_answer(self):
        asrt = {
            "expect_all": ["包邮"],
            "expect_any": ["无门槛", "不设金额门槛", "没有门槛"],
            "must_not": ["满199", "满 199", "199 元包邮"],
        }
        passed, detail = judge_fact(
            "目前不存在满 199 包邮，店铺是不设金额门槛包邮的。", asrt)
        self.assertTrue(passed)
        self.assertEqual(detail["must_not_hits"], [])

    def test_judge_fact_still_fails_on_real_fabrication(self):
        asrt = {"expect_all": ["349"], "must_not": ["399"]}
        passed, detail = judge_fact("暖阳 1.8 米是 399 元。", asrt)
        self.assertFalse(passed)
        self.assertEqual(detail["must_not_hits"], ["399"])


if __name__ == "__main__":
    unittest.main()
