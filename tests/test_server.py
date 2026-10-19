"""server.py 的离线单元测试（零第三方依赖，只用标准库 unittest）。

覆盖范围：
  · RateLimiter：每分钟 / 每日 / 全站限额、IP 隔离、窗口过期、字典清理
  · SessionStore：服务端生成身份、会话复用、conversation 持久化、TTL 与上限
  · Handler 小工具：来源校验、Cookie 解析、真实 IP、错误信息提取
  · _pump_stream：扣子 SSE → 前端 meta / delta / error / done 的翻译
  · 集成：/api/health、静态目录穿越、未配置令牌时的 /api/chat

不调用扣子接口，不依赖网络，可在 CI 里直接跑：
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, key, default=""):
        return dict.get(self, key, default)


class FakeHandler:
    """只保留 Handler 小工具需要的属性，不碰 socket。"""

    def __init__(self, headers=None, client=("9.9.9.9", 12345)):
        self.headers = FakeHeaders(headers or {})
        self.client_address = client


class FakeSse:
    """收集 _pump_stream 下发的事件，方便断言。"""

    _error_message = staticmethod(server.Handler._error_message)

    def __init__(self):
        self.events = []

    def _sse_send(self, event, data):
        self.events.append((event, data))


def _sse_lines(*events):
    """把 (event, data) 组装成扣子风格的 SSE 行。"""
    out = []
    for event, data in events:
        out.append(f"event: {event}\n".encode("utf-8"))
        out.append(f"data: {data}\n".encode("utf-8"))
        out.append(b"\n")
    return out


# ----------------------------------------------------------------------
# RateLimiter
# ----------------------------------------------------------------------
class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.limiter = server.RateLimiter()

    def test_allows_within_per_minute(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 2), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100):
            self.assertTrue(self.limiter.check("1.1.1.1")[0])
            self.assertTrue(self.limiter.check("1.1.1.1")[0])

    def test_blocks_over_per_minute(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 2), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100):
            self.limiter.check("1.1.1.1")
            self.limiter.check("1.1.1.1")
            allowed, reason = self.limiter.check("1.1.1.1")
            self.assertFalse(allowed)
            self.assertIn("快", reason)

    def test_blocks_over_daily_ip_limit(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 100), \
             mock.patch.object(server, "IP_PER_DAY", 2), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100):
            self.limiter.check("1.1.1.1")
            self.limiter.check("1.1.1.1")
            allowed, reason = self.limiter.check("1.1.1.1")
            self.assertFalse(allowed)
            self.assertIn("上限", reason)

    def test_blocks_when_global_limit_reached(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 100), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 2):
            self.limiter.check("1.1.1.1")
            self.limiter.check("2.2.2.2")
            allowed, reason = self.limiter.check("3.3.3.3")
            self.assertFalse(allowed)
            self.assertIn("额度", reason)

    def test_different_ips_are_independent(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 1), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100):
            self.assertTrue(self.limiter.check("1.1.1.1")[0])
            self.assertFalse(self.limiter.check("1.1.1.1")[0])
            self.assertTrue(self.limiter.check("2.2.2.2")[0])

    def test_sliding_window_expires(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 1), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100), \
             mock.patch.object(server.time, "time", return_value=1000.0):
            self.assertTrue(self.limiter.check("1.1.1.1")[0])
            self.assertFalse(self.limiter.check("1.1.1.1")[0])
            with mock.patch.object(server.time, "time", return_value=1061.0):
                self.assertTrue(self.limiter.check("1.1.1.1")[0])

    def test_stale_ip_entries_are_cleaned(self):
        with mock.patch.object(server, "IP_PER_MINUTE", 100), \
             mock.patch.object(server, "IP_PER_DAY", 100), \
             mock.patch.object(server, "GLOBAL_PER_DAY", 100), \
             mock.patch.object(server.time, "time", return_value=1000.0):
            self.limiter.check("old-ip")
        with mock.patch.object(server.time, "time", return_value=1100.0):
            self.limiter.check("new-ip")
        self.assertNotIn("old-ip", self.limiter._hits)


# ----------------------------------------------------------------------
# SessionStore
# ----------------------------------------------------------------------
class SessionStoreTest(unittest.TestCase):
    def test_new_session_creates_server_side_identity(self):
        store = server.SessionStore()
        token, user_id, conversation_id, fresh = store.resolve("")
        self.assertTrue(fresh)
        self.assertTrue(token)
        self.assertTrue(user_id.startswith("web_"))
        self.assertEqual(conversation_id, "")

    def test_existing_token_keeps_identity(self):
        store = server.SessionStore()
        token, user_id, _, _ = store.resolve("")
        token2, user_id2, _, fresh2 = store.resolve(token)
        self.assertEqual(token, token2)
        self.assertEqual(user_id, user_id2)
        self.assertFalse(fresh2)

    def test_set_conversation_persists(self):
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.set_conversation(token, "conv-123")
        _, _, conversation_id, fresh = store.resolve(token)
        self.assertEqual(conversation_id, "conv-123")
        self.assertFalse(fresh)

    def test_unknown_token_creates_new_session(self):
        store = server.SessionStore()
        token, _, _, fresh = store.resolve("not-a-real-token")
        self.assertTrue(fresh)
        self.assertNotEqual(token, "not-a-real-token")

    def test_expired_session_is_replaced(self):
        store = server.SessionStore()
        with mock.patch.object(server.time, "time", return_value=1000.0):
            token, _, _, _ = store.resolve("")
        with mock.patch.object(server.time, "time", return_value=1000.0 + server.SESSION_TTL + 1):
            token2, _, _, fresh = store.resolve(token)
        self.assertTrue(fresh)
        self.assertNotEqual(token, token2)

    def test_session_store_respects_max_size(self):
        store = server.SessionStore()
        with mock.patch.object(server, "MAX_SESSIONS", 2):
            for i in range(3):
                with mock.patch.object(server.time, "time", return_value=1000.0 + i):
                    store.resolve("")
        self.assertLessEqual(len(store._data), 2)


# ----------------------------------------------------------------------
# Handler 小工具
# ----------------------------------------------------------------------
class HandlerHelpersTest(unittest.TestCase):
    def test_origin_same_host_http_and_https(self):
        with mock.patch.object(server, "EXTRA_ORIGINS", []):
            handler = FakeHandler({"Host": "demo.example.com", "Origin": "https://demo.example.com"})
            self.assertTrue(server.Handler._origin_allowed(handler))
            handler = FakeHandler({"Host": "demo.example.com", "Origin": "http://demo.example.com"})
            self.assertTrue(server.Handler._origin_allowed(handler))

    def test_origin_cross_site_rejected(self):
        with mock.patch.object(server, "EXTRA_ORIGINS", []):
            handler = FakeHandler({"Host": "demo.example.com", "Origin": "https://evil.example.com"})
            self.assertFalse(server.Handler._origin_allowed(handler))

    def test_origin_missing_allowed_for_scripts(self):
        handler = FakeHandler({"Host": "demo.example.com"})
        self.assertTrue(server.Handler._origin_allowed(handler))

    def test_extra_origins_are_allowed(self):
        handler = FakeHandler({"Host": "demo.example.com", "Origin": "https://custom.example.com"})
        with mock.patch.object(server, "EXTRA_ORIGINS", ["https://custom.example.com"]):
            self.assertTrue(server.Handler._origin_allowed(handler))

    def test_read_cookie(self):
        handler = FakeHandler({"Cookie": "a=1; ym_session=abc123; b=2"})
        self.assertEqual(server.Handler._read_cookie(handler, "ym_session"), "abc123")
        self.assertEqual(server.Handler._read_cookie(handler, "missing"), "")

    def test_client_ip_prefers_forwarded(self):
        handler = FakeHandler({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        self.assertEqual(server.Handler._client_ip(handler), "1.2.3.4")

    def test_client_ip_falls_back_to_socket(self):
        handler = FakeHandler({}, client=("10.0.0.7", 1234))
        self.assertEqual(server.Handler._client_ip(handler), "10.0.0.7")

    def test_error_message_known_code(self):
        message = server.Handler._error_message('{"code": 4013}', "兜底")
        self.assertIn("令牌", message)

    def test_error_message_unknown_code_uses_msg(self):
        message = server.Handler._error_message('{"code": 9999, "msg": "自定义错误"}', "兜底")
        self.assertEqual(message, "自定义错误")

    def test_error_message_invalid_json_falls_back(self):
        self.assertEqual(server.Handler._error_message("not-json", "兜底"), "兜底")
        self.assertEqual(server.Handler._error_message("[1,2]", "兜底"), "兜底")


# ----------------------------------------------------------------------
# SSE 翻译
# ----------------------------------------------------------------------
class PumpStreamTest(unittest.TestCase):
    def test_translates_delta_and_emits_meta_once(self):
        fake = FakeSse()
        first = json.dumps({"conversation_id": "c1", "role": "assistant",
                            "type": "answer", "content": "你好"}, ensure_ascii=False)
        second = json.dumps({"conversation_id": "c1", "role": "assistant",
                             "type": "answer", "content": "世界"}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", first),
            ("conversation.message.delta", second),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["meta", "delta", "delta", "done"])
        self.assertEqual(json.loads(fake.events[0][1])["conversation_id"], "c1")
        self.assertEqual(json.loads(fake.events[1][1])["content"], "你好")

    def test_completed_message_falls_back_to_single_delta(self):
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "assistant",
                              "type": "answer", "content": "整段回答"}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.completed", payload),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["meta", "delta", "done"])
        self.assertEqual(json.loads(fake.events[1][1])["content"], "整段回答")

    def test_error_event_is_translated(self):
        fake = FakeSse()
        resp = _sse_lines(("error", '{"code": 4013}'))
        server.Handler._pump_stream(fake, resp, "token")
        self.assertEqual(len(fake.events), 1)
        event, data = fake.events[0]
        self.assertEqual(event, "error")
        self.assertIn("令牌", json.loads(data)["message"])

    def test_non_assistant_messages_are_ignored(self):
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "user",
                              "type": "question", "content": "用户问题"}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", payload),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["meta", "done"])


# ----------------------------------------------------------------------
# 集成：起一个真实 HTTP 服务
# ----------------------------------------------------------------------
class ServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"} if body else {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_health_endpoint(self):
        status, body = self._request("GET", "/api/health")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("max_message_chars", data)

    def test_path_traversal_is_blocked(self):
        status, body = self._request("GET", "/../server.py")
        self.assertEqual(status, 403)

    def test_chat_without_token_returns_clear_error(self):
        with mock.patch.object(server, "COZE_API_TOKEN", ""):
            status, body = self._request("POST", "/api/chat",
                                         json.dumps({"message": "测试"}))
        data = json.loads(body)
        self.assertEqual(status, 500)
        self.assertIn("COZE_API_TOKEN", data["error"])


if __name__ == "__main__":
    unittest.main()
