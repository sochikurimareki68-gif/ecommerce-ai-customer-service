# -*- coding: utf-8 -*-
"""确定性回答层（L1）与兜底（L3）的测试。

覆盖：
  · 价格按档、非在售规格、尺寸、洗护、时效、政策类事实命中
  · 对比、组合计算、推荐、颜色、模糊指代不拦
  · 原文答复逐字返回，不改数、不取中间价
  · 扣子空回复 / 只回标记 → 用知识库原文兜底；发票标记不覆盖
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kb_fact_answer as K  # noqa: E402
import server  # noqa: E402
from tests.test_server import FakeSse, _sse_lines  # noqa: E402


class FactMatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = K.load_index(ROOT / "data" / "kb_fact_index.json")

    def hit(self, message: str) -> dict:
        result = K.match_fact(message, self.index)
        self.assertIsNotNone(result, message)
        return result

    def assert_miss(self, message: str) -> None:
        self.assertIsNone(K.match_fact(message, self.index), message)

    def test_price_returns_all_tiers_verbatim(self):
        reply = self.hit("云柔四件套1.8米多少钱")["reply"]
        self.assertIn("1.2m ¥199", reply)
        self.assertIn("1.5m ¥259", reply)
        self.assertIn("1.8m ¥299", reply)
        self.assertNotIn("279", reply)

    def test_out_of_stock_size_returns_original_entry(self):
        reply = self.hit("暖阳有1.2米的吗")["reply"]
        self.assertIn("规格 1.5m、1.8m", reply)

    def test_bed_size_question_hits_size_table(self):
        entry = self.hit("1.5米床买多大四件套")["entry"]
        self.assertEqual(entry["section"], "四件套尺寸对照")

    def test_wash_temperature_hits_product_entry(self):
        entry = self.hit("法兰绒四件套怎么洗")["entry"]
        self.assertEqual(entry["file"], "02-床品套件.md")

    def test_shipping_time_uses_48_hours(self):
        reply = self.hit("多久发货")["reply"]
        self.assertIn("48 小时", reply)

    def test_return_window_uses_7_days(self):
        reply = self.hit("退换货要几天")["reply"]
        self.assertIn("7 天无理由", reply)

    def test_store_answer_is_complete_verbatim(self):
        """实体店答复曾出现「无线下门店」少字，整段断言防回归。"""
        reply = self.hit("你们有实体店吗")["reply"]
        self.assertEqual(
            reply,
            "亲，优眠家居主营床品套件、被芯、枕芯、床垫保护套等家居床品，"
            "提供 1.2m、1.5m、1.8m 等常见床型规格；目前只有线上店铺，"
            "无线下门店，也不做加盟。",
        )

    def test_policy_answers_are_complete(self):
        """口径类条目逐段核对，防止索引/拼装阶段掉字。"""
        cases = {
            "你们有实体店吗": "无线下门店，也不做加盟",
            "支持货到付款吗": "不支持货到付款，也不支持到付",
            "包邮吗": "包邮",
            "能开发票吗": "发票",
            "产地在哪里": "浙江绍兴",
        }
        for message, fragment in cases.items():
            reply = self.hit(message)["reply"]
            self.assertIn(fragment, reply, message)
            self.assertNotIn("[[", reply, message)

    def test_policy_answers_match_index_verbatim(self):
        """拼装结果必须等于「亲，」+ 索引答复，不允许任何截断。"""
        for message in ("你们有实体店吗", "支持货到付款吗", "包邮吗", "能开发票吗", "产地在哪里"):
            hit = self.hit(message)
            self.assertEqual(hit["reply"], "亲，" + hit["entry"]["answer"], message)

    def test_policy_facts_are_covered(self):
        cases = {
            "门店在哪里": "品牌与经营范围",
            "支持货到付款吗": "支付方式与货到付款",
            "包邮吗": "运费与包邮规则",
            "能开发票吗": "开票类型与补开",
            "产地在哪里": "产品产地与工厂",
            "认证有哪些": "产品认证与检测报告",
        }
        for message, section in cases.items():
            self.assertEqual(self.hit(message)["entry"]["section"], section, message)

    def test_comparison_and_purchase_advice_are_not_intercepted(self):
        for message in ("云柔和暖阳哪个好", "推荐一款适合送人的", "性价比高的四件套多少钱"):
            self.assert_miss(message)

    def test_calculation_and_ambiguous_reference_are_not_intercepted(self):
        for message in ("两套一共多少钱", "299那款怎么样", "这个多少钱", "满减后到手价多少"):
            self.assert_miss(message)

    def test_quality_and_color_questions_are_not_intercepted(self):
        for message in ("质量好吗", "有蓝色的吗"):
            self.assert_miss(message)

    def test_unknown_product_price_is_not_guessed(self):
        self.assert_miss("买个被芯多少钱")


class FactServerIntegrationTest(unittest.TestCase):
    def setUp(self):
        # 测试稳定：不受本地环境变量影响
        self._answer = server.FACT_ANSWER_ENABLED
        self._fallback = server.FACT_FALLBACK_ENABLED
        server.FACT_ANSWER_ENABLED = True
        server.FACT_FALLBACK_ENABLED = True
        server.FACT_INDEX = K.load_index(ROOT / "data" / "kb_fact_index.json")

    def tearDown(self):
        server.FACT_ANSWER_ENABLED = self._answer
        server.FACT_FALLBACK_ENABLED = self._fallback

    def test_lookup_returns_kb_reply(self):
        hit = server.lookup_fact("云柔四件套1.8米多少钱")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["reply"].startswith("亲，"))

    def test_lookup_returns_none_when_disabled(self):
        server.FACT_ANSWER_ENABLED = False
        self.assertIsNone(server.lookup_fact("云柔四件套1.8米多少钱"))

    def test_empty_upstream_uses_fact_fallback(self):
        fake = FakeSse()
        server.Handler._pump_stream(fake, [], "token", 0, "多久发货")
        kinds = [e for e, _ in fake.events]
        self.assertIn("delta", kinds)
        content = next(json.loads(d)["content"] for e, d in fake.events if e == "delta")
        self.assertIn("48 小时", content)

    def test_marker_only_uses_fact_fallback(self):
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "assistant",
                              "type": "answer", "content": "[[NO_ANSWER]]"},
                             ensure_ascii=False)
        resp = _sse_lines(("conversation.message.completed", payload), ("done", "[DONE]"))
        server.Handler._pump_stream(fake, resp, "token", 0, "床垫保护套防水吗")
        content = next(json.loads(d)["content"] for e, d in fake.events if e == "delta")
        self.assertIn("纯棉防水款", content)
        self.assertNotIn("转接人工", content)

    def test_invoice_marker_is_not_overridden(self):
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "assistant",
                              "type": "answer", "content": "[[NO_ANSWER_INVOICE]]"},
                             ensure_ascii=False)
        resp = _sse_lines(("conversation.message.completed", payload), ("done", "[DONE]"))
        server.Handler._pump_stream(fake, resp, "token", 0, "能开发票吗")
        content = next(json.loads(d)["content"] for e, d in fake.events if e == "delta")
        self.assertIn("发票需要人工", content)

    def test_lookup_is_not_intercepted_for_comparison(self):
        self.assertIsNone(server.lookup_fact("云柔和暖阳哪个好"))


class FactChatHttpTest(unittest.TestCase):
    """HTTP 级验证：命中查表层时直接返回，不发起任何扣子请求。"""

    @classmethod
    def setUpClass(cls):
        cls._token = server.COZE_API_TOKEN
        cls._answer = server.FACT_ANSWER_ENABLED
        server.COZE_API_TOKEN = "test-token"
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        server.COZE_API_TOKEN = cls._token
        server.FACT_ANSWER_ENABLED = cls._answer

    def setUp(self):
        server.FACT_ANSWER_ENABLED = True
        server.FACT_INDEX = K.load_index(ROOT / "data" / "kb_fact_index.json")
        server.LIMITER._hits.clear()

    def _post_chat(self, message: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/chat", body=json.dumps({"message": message}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def test_fact_hit_answers_without_calling_coze(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("不应调用扣子")):
            status, body = self._post_chat("云柔四件套1.8米多少钱")
        self.assertEqual(status, 200)
        self.assertIn("1.8m ¥299", body)
        self.assertIn("event: done", body)

    def test_non_fact_question_still_goes_to_coze(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("应转发扣子")):
            status, body = self._post_chat("云柔和暖阳哪个好")
        self.assertEqual(status, 502)
        self.assertIn("无法连接扣子接口", body)

    def test_store_answer_is_not_truncated_on_wire(self):
        """SSE 线上传输不得少字：整句必须原样出现在响应体里。"""
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("不应调用扣子")):
            status, body = self._post_chat("你们有实体店吗")
        self.assertEqual(status, 200)
        self.assertIn(
            "目前只有线上店铺，无线下门店，也不做加盟。",
            body,
        )


if __name__ == "__main__":
    unittest.main()
