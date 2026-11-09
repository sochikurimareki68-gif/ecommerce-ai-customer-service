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
import time
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

    def __init__(self, headers=None, client=("9.9.9.9", 12345), path="/api/pending"):
        self.headers = FakeHeaders(headers or {})
        self.client_address = client
        self.path = path


class FakeSse:
    """收集 _pump_stream 下发的事件，方便断言。"""

    _error_message = staticmethod(server.Handler._error_message)
    _split_structured = staticmethod(server.Handler._split_structured)

    def __init__(self):
        self.events = []
        # v2.0 起 _pump_stream 会在首字时记录 TTFT，需要一个请求起始时间戳
        self._req_start = time.time()

    def _sse_send(self, event, data):
        self.events.append((event, data))

    def _sse_start(self):
        pass

    def _sse_end(self):
        pass

    def _emit_chat_log(self, *args, **kwargs):
        pass

    def _client_ip(self):
        return "9.9.9.9"


class FakeJsonHandler:
    """只保留 _send_json 的假 Handler，用来直接测管理接口的处理器。"""

    def __init__(self):
        self.status = None
        self.payload = None

    def _send_json(self, status, obj):
        self.status = status
        self.payload = obj


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
# HealthLimiter
# ----------------------------------------------------------------------
class HealthLimiterTest(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        limiter = server.HealthLimiter()
        with mock.patch.object(server, "HEALTH_PER_MINUTE", 2):
            self.assertTrue(limiter.allow("1.1.1.1"))
            self.assertTrue(limiter.allow("1.1.1.1"))
            self.assertFalse(limiter.allow("1.1.1.1"))

    def test_different_ips_are_independent(self):
        limiter = server.HealthLimiter()
        with mock.patch.object(server, "HEALTH_PER_MINUTE", 1):
            self.assertTrue(limiter.allow("1.1.1.1"))
            self.assertFalse(limiter.allow("1.1.1.1"))
            self.assertTrue(limiter.allow("2.2.2.2"))

    def test_window_expires_after_one_minute(self):
        limiter = server.HealthLimiter()
        with mock.patch.object(server, "HEALTH_PER_MINUTE", 1), \
             mock.patch.object(server.time, "time", return_value=1000.0):
            self.assertTrue(limiter.allow("1.1.1.1"))
            self.assertFalse(limiter.allow("1.1.1.1"))
            with mock.patch.object(server.time, "time", return_value=1061.0):
                self.assertTrue(limiter.allow("1.1.1.1"))

    def test_stale_entries_are_cleaned_when_blocked(self):
        """被刷时也要清理过期 IP，避免字典无限增长。"""
        limiter = server.HealthLimiter()
        with mock.patch.object(server, "HEALTH_PER_MINUTE", 1), \
             mock.patch.object(server.time, "time", return_value=1000.0):
            limiter.allow("old-ip")
        with mock.patch.object(server, "HEALTH_PER_MINUTE", 1), \
             mock.patch.object(server.time, "time", return_value=1100.0):
            limiter.allow("new-ip")
            limiter.allow("new-ip")      # 触发拒绝分支
        self.assertNotIn("old-ip", limiter._hits)


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

    def test_reset_clears_conversation_and_bumps_epoch(self):
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.set_conversation(token, "conv-123")
        before = store.epoch(token)
        after = store.reset(token)
        self.assertEqual(after, before + 1)
        _, _, conversation_id, _ = store.resolve(token)
        self.assertEqual(conversation_id, "")
        self.assertEqual(store.epoch(token), after)

    def test_stale_epoch_cannot_overwrite_conversation(self):
        """清空后，在途请求晚到的 conversation_id 必须被丢弃。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        stale = store.epoch(token)
        store.reset(token)
        store.set_conversation(token, "conv-old", stale)
        _, _, conversation_id, _ = store.resolve(token)
        self.assertEqual(conversation_id, "")

        # 新世代的写入应当正常生效
        store.set_conversation(token, "conv-new", store.epoch(token))
        _, _, conversation_id, _ = store.resolve(token)
        self.assertEqual(conversation_id, "conv-new")

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

    def test_new_session_starts_at_zero_turns(self):
        """首问判定看 turn_count，不看 epoch。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        self.assertEqual(store.turn_count(token), 0)

    def test_record_turn_increments_turn_count(self):
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.record_turn(token, "你好", "您好", store.epoch(token))
        self.assertEqual(store.turn_count(token), 1)

    def test_reset_clears_turn_count_and_history(self):
        """点过「清空对话」后要重新算首问，否则 FAQ 缓存永远不再命中。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.record_turn(token, "你好", "您好")
        store.reset(token)
        self.assertEqual(store.turn_count(token), 0)
        self.assertEqual(store.history(token), [])

    def test_stale_epoch_turn_is_discarded(self):
        """清空后在途请求晚到的问答不能写进新世代。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        stale = store.epoch(token)
        store.reset(token)
        store.record_turn(token, "旧问题", "旧回答", stale)
        self.assertEqual(store.turn_count(token), 0)
        self.assertEqual(store.history(token), [])

    def test_history_keeps_only_latest_rounds(self):
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        total = server.HANDOFF_CONTEXT_ROUNDS + 2
        for i in range(total):
            store.record_turn(token, f"问{i}", f"答{i}")
        history = store.history(token)
        self.assertEqual(len(history), server.HISTORY_MAX_MESSAGES)
        # 保留的是最后 N 轮，最早的两轮应被丢弃
        self.assertEqual(history[0]["text"], "问2")
        self.assertEqual(history[-2]["text"], f"问{total - 1}")
        self.assertEqual(history[-1]["text"], f"答{total - 1}")

    def test_handoff_state_survives_and_reset_clears_it(self):
        """方案 B：转人工状态由服务端保存，刷新后仍在；reset 后彻底复位。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        self.assertIsNone(store.handoff(token))
        store.enter_handoff(token, token[:12])
        self.assertEqual(store.handoff(token)["ticket"], token[:12])
        self.assertTrue(store.append_user_message(token, "刷新后这条还在"))
        self.assertEqual(store.history(token)[-1]["text"], "刷新后这条还在")
        store.reset(token)
        self.assertIsNone(store.handoff(token))
        self.assertEqual(store.history(token), [])


# ----------------------------------------------------------------------
# I-4 人工接管：入队时带上最近若干轮上下文
# ----------------------------------------------------------------------
class HandoffContextTest(unittest.TestCase):
    def tearDown(self):
        with server.PENDING_HANDOFF_LOCK:
            server.PENDING_HANDOFF.clear()

    def _prepare(self):
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.record_turn(token, "四件套是什么面料", "这款是纯棉的")
        store.record_turn(token, "那会缩水吗", "正在为您转接人工客服")
        server.record_handoff(token, "9.9.9.9", "正在为您转接人工客服", store.history(token))
        return token

    def test_handoff_keeps_previous_rounds(self):
        token = self._prepare()
        entry = server.PENDING_HANDOFF[token]
        self.assertEqual(
            [m["text"] for m in entry["messages"]],
            ["四件套是什么面料", "这款是纯棉的", "那会缩水吗", "正在为您转接人工客服"],
        )

    def test_pending_list_previews_latest_user_message(self):
        """列表预览不能取历史里最早那句，要取触发转人工的这一句。"""
        self._prepare()
        fake = FakeJsonHandler()
        server.Handler._handle_pending_list(fake)
        item = fake.payload["items"][0]
        self.assertEqual(item["last_user"], "那会缩水吗")
        self.assertEqual(item["msg_count"], 4)

    def test_pending_detail_reports_context_rounds(self):
        token = self._prepare()
        fake = FakeJsonHandler()
        server.Handler._handle_pending_detail(fake, token)
        self.assertEqual(fake.payload["context_rounds"], server.HANDOFF_CONTEXT_ROUNDS)
        self.assertEqual(len(fake.payload["messages"]), 4)

    def test_repeat_handoff_keeps_first_position(self):
        """连续两次「转人工」不应重复建单，排位保持第一次入队时的位置。"""
        token = self._prepare()
        store = server.SessionStore()
        token2, _, _, _ = store.resolve("")
        store.record_turn(token2, "转人工", "正在为您转接人工客服")
        pos2 = server.record_handoff(token2, "8.8.8.8", "正在为您转接人工客服", store.history(token2))
        self.assertEqual(pos2, 2)
        self.assertEqual(server.record_handoff(
            token, "9.9.9.9", "正在为您转接人工客服", []), 1)
        self.assertEqual(len(server.PENDING_HANDOFF), 2)

    def test_handoff_message_goes_to_human_without_upstream_call(self):
        """方案 B：已转人工后发消息不调上游、不出机器人回复，只落同一工单。"""
        token, _, _, _ = server.SESSIONS.resolve("")
        server.SESSIONS.enter_handoff(token, token[:12])
        server.record_handoff(token, "9.9.9.9", "正在为您转接人工客服", [])
        fake = FakeSse()
        with mock.patch.object(server.urllib.request, "urlopen",
                               side_effect=AssertionError("转人工后不应调用上游")):
            server.Handler._handle_handoff_message(
                fake, token, "订单到哪了", {"ticket": token[:12]},
                server.SESSIONS.epoch(token))
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["handoff", "done"])          # 没有 delta / branch
        info = json.loads(fake.events[0][1])
        self.assertTrue(info["delivered"])
        self.assertEqual(info["ticket"], token[:12])
        self.assertEqual(server.PENDING_HANDOFF[token]["messages"][-1]["text"], "订单到哪了")
        self.assertIn("ts", server.PENDING_HANDOFF[token]["messages"][-1])
        self.assertEqual(server.SESSIONS.history(token)[-1]["text"], "订单到哪了")

    def test_handoff_state_endpoint_reports_ticket_after_refresh(self):
        token, _, _, _ = server.SESSIONS.resolve("")
        server.SESSIONS.enter_handoff(token, token[:12])
        server.record_handoff(token, "9.9.9.9", "正在为您转接人工客服", [])
        fake = FakeJsonHandler()
        fake._read_cookie = lambda name: token
        fake._session_cookie = lambda t: ""
        server.Handler._handle_handoff_state(fake)
        self.assertEqual(fake.status, 200)
        self.assertTrue(fake.payload["handoff"])
        self.assertEqual(fake.payload["ticket"], token[:12])
        self.assertEqual(fake.payload["queue_position"], 1)

    def test_repeat_refresh_keeps_ticket_and_history_without_new_handoff(self):
        """刷新恢复必须幂等：工单号不变、队列不增加、历史照常返回。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.record_turn(token, "转人工", "亲~马上为您转接人工客服")
        server.record_handoff(token, "9.9.9.9", "亲~马上为您转接人工客服",
                              store.history(token))
        store.enter_handoff(token, token[:12])
        payloads = []
        with mock.patch.object(server, "SESSIONS", store):
            for _ in range(3):
                fake = FakeJsonHandler()
                fake._read_cookie = lambda name: token
                fake._session_cookie = lambda t: ""
                server.Handler._handle_handoff_state(fake)
                payloads.append(fake.payload)
        self.assertTrue(all(p["handoff"] for p in payloads))
        self.assertEqual({p["ticket"] for p in payloads}, {token[:12]})
        self.assertEqual(len(server.PENDING_HANDOFF), 1)
        self.assertEqual([m["text"] for m in payloads[-1]["messages"]],
                         ["转人工", "亲~马上为您转接人工客服"])

    def test_handoff_state_without_active_ticket_is_cleared(self):
        """工单已处理（队列里没有活动工单）时不能再报「等待人工接入」。"""
        store = server.SessionStore()
        token, _, _, _ = store.resolve("")
        store.record_turn(token, "转人工", "亲~马上为您转接人工客服")
        server.record_handoff(token, "9.9.9.9", "亲~马上为您转接人工客服", [])
        store.enter_handoff(token, token[:12])
        with server.PENDING_HANDOFF_LOCK:
            server.PENDING_HANDOFF[token]["resolved"] = True
        with mock.patch.object(server, "SESSIONS", store):
            fake = FakeJsonHandler()
            fake._read_cookie = lambda name: token
            fake._session_cookie = lambda t: ""
            server.Handler._handle_handoff_state(fake)
        self.assertFalse(fake.payload["handoff"])
        self.assertIsNone(store.handoff(token))       # 幽灵状态被清掉
        self.assertEqual(len(fake.payload["messages"]), 2)

    def test_handoff_state_never_creates_session_or_ticket(self):
        """全新浏览器打开页面（无 cookie）不能建会话、建单。"""
        store = server.SessionStore()
        with mock.patch.object(server, "SESSIONS", store):
            fake = FakeJsonHandler()
            fake._read_cookie = lambda name: ""
            fake._session_cookie = lambda t: ""
            server.Handler._handle_handoff_state(fake)
        self.assertEqual(fake.payload, {"handoff": False, "messages": []})
        self.assertEqual(len(store._data), 0)
        self.assertEqual(len(server.PENDING_HANDOFF), 0)


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

    def test_session_cookie_is_lax_on_local_http(self):
        """本地 HTTP 调试不能加 Secure，只能保持 SameSite=Lax。"""
        handler = FakeHandler({})
        with mock.patch.object(server, "COOKIE_SECURE", False):
            cookie = server.Handler._session_cookie(handler, "tok")
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)

    def test_session_cookie_is_none_secure_behind_https(self):
        """HTTPS 下挂件 iframe 跨站，Cookie 必须是 SameSite=None; Secure。"""
        handler = FakeHandler({"X-Forwarded-Proto": "https"})
        with mock.patch.object(server, "COOKIE_SECURE", False):
            cookie = server.Handler._session_cookie(handler, "tok")
        self.assertIn("SameSite=None", cookie)
        self.assertIn("Secure", cookie)

    def test_session_cookie_forced_secure_by_env(self):
        handler = FakeHandler({})
        with mock.patch.object(server, "COOKIE_SECURE", True):
            cookie = server.Handler._session_cookie(handler, "tok")
        self.assertIn("SameSite=None", cookie)
        self.assertIn("Secure", cookie)

    def test_client_ip_prefers_forwarded(self):
        handler = FakeHandler({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        self.assertEqual(server.Handler._client_ip(handler), "1.2.3.4")

    def test_client_ip_prefers_cloudflare_header(self):
        # CF-Connecting-IP 由 Cloudflare 覆写、客户端伪造不了，优先级高于 XFF
        handler = FakeHandler({
            "CF-Connecting-IP": "8.8.8.8",
            "X-Forwarded-For": "1.2.3.4, 5.6.7.8",
        })
        self.assertEqual(server.Handler._client_ip(handler), "8.8.8.8")

    def test_client_ip_falls_back_to_socket(self):
        handler = FakeHandler({}, client=("10.0.0.7", 1234))
        self.assertEqual(server.Handler._client_ip(handler), "10.0.0.7")

    def test_admin_rejects_when_token_not_configured(self):
        handler = FakeHandler({"X-Admin-Token": "anything"})
        with mock.patch.object(server, "ADMIN_TOKEN", ""):
            ok, _ = server.Handler._admin_allowed(handler)
            self.assertFalse(ok)

    def test_admin_rejects_wrong_token(self):
        handler = FakeHandler({"X-Admin-Token": "wrong"})
        with mock.patch.object(server, "ADMIN_TOKEN", "s3cret"):
            ok, _ = server.Handler._admin_allowed(handler)
            self.assertFalse(ok)

    def test_admin_accepts_header_token(self):
        handler = FakeHandler({"X-Admin-Token": "s3cret"})
        with mock.patch.object(server, "ADMIN_TOKEN", "s3cret"):
            ok, _ = server.Handler._admin_allowed(handler)
            self.assertTrue(ok)

    def test_admin_accepts_query_token(self):
        handler = FakeHandler({}, path="/api/pending?token=s3cret")
        with mock.patch.object(server, "ADMIN_TOKEN", "s3cret"):
            ok, _ = server.Handler._admin_allowed(handler)
            self.assertTrue(ok)

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
        # v2.0 起流末尾固定补一个 branch 事件（按关键词推断），前端忽略、评测统计用
        self.assertEqual(kinds, ["meta", "delta", "delta", "branch", "done"])
        self.assertEqual(json.loads(fake.events[0][1])["conversation_id"], "c1")
        self.assertEqual(json.loads(fake.events[1][1])["content"], "你好")
        self.assertEqual(json.loads(fake.events[3][1])["branch"], "其他兜底")

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
        self.assertEqual(kinds, ["meta", "delta", "branch", "done"])
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
        # 用户消息不算回答，流里没有可展示内容时应当报错而不是静默结束
        self.assertEqual(kinds, ["meta", "error"])

    def test_empty_stream_reports_error_instead_of_blank_done(self):
        """上游既没有增量也没有完成事件时，不能只发 done 让前端留下空气泡。"""
        fake = FakeSse()
        resp = _sse_lines(("done", "[DONE]"))
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["error"])
        self.assertIn("超时", json.loads(fake.events[0][1])["message"])

    def test_handoff_reply_emits_handoff_event(self):
        """命中转接话术时要下发 handoff 事件，前端才能展示排队状态。"""
        with server.PENDING_HANDOFF_LOCK:
            server.PENDING_HANDOFF.clear()
        try:
            fake = FakeSse()
            payload = json.dumps({
                "conversation_id": "c1", "role": "assistant", "type": "answer",
                "content": "亲~好的，马上为您转接人工客服，请您耐心等待哦~",
            }, ensure_ascii=False)
            resp = _sse_lines(
                ("conversation.message.delta", payload),
                ("done", "[DONE]"),
            )
            server.Handler._pump_stream(fake, resp, "token-1234567890")
            kinds = [e for e, _ in fake.events]
            self.assertEqual(kinds, ["meta", "delta", "branch", "handoff", "done"])
            info = json.loads(fake.events[3][1])
            self.assertEqual(info["ticket"], "token-123456")
            self.assertEqual(info["queue_position"], 1)
            self.assertIn("token-1234567890", server.PENDING_HANDOFF)
        finally:
            with server.PENDING_HANDOFF_LOCK:
                server.PENDING_HANDOFF.clear()


class StructuredOutputTest(unittest.TestCase):
    """结束节点返回 {"reply","branch"} 时的拆分与透传。"""

    def test_split_reply_and_branch(self):
        reply, branch = server.Handler._split_structured(
            '{"reply": "亲，这款是纯棉的", "branch": "商品咨询"}')
        self.assertEqual(reply, "亲，这款是纯棉的")
        self.assertEqual(branch, "商品咨询")

    def test_missing_reply_is_rejected(self):
        # 没有 reply 就不能当答案用，否则会把 JSON 原样显示给用户
        self.assertEqual(server.Handler._split_structured('{"branch": "商品咨询"}'), ("", ""))
        self.assertEqual(server.Handler._split_structured('{"reply": "   "}'), ("", ""))

    def test_invalid_json_is_rejected(self):
        self.assertEqual(server.Handler._split_structured("普通文本回复"), ("", ""))
        self.assertEqual(server.Handler._split_structured("[1, 2]"), ("", ""))

    def test_structured_stream_is_buffered_then_emitted(self):
        """JSON 片段不能边收边吐，必须攒齐后只下发 reply。"""
        fake = FakeSse()
        part1 = json.dumps({"conversation_id": "c1", "role": "assistant", "type": "answer",
                            "content": '{"reply": "你好'}, ensure_ascii=False)
        part2 = json.dumps({"conversation_id": "c1", "role": "assistant", "type": "answer",
                            "content": '，请稍等", "branch": "商品咨询"}'}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", part1),
            ("conversation.message.delta", part2),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertEqual(kinds, ["meta", "delta", "branch", "done"])
        self.assertEqual(json.loads(fake.events[1][1])["content"], "你好，请稍等")
        self.assertEqual(json.loads(fake.events[2][1])["branch"], "商品咨询")

    def test_structured_stream_without_reply_reports_error(self):
        fake = FakeSse()
        payload = json.dumps({"conversation_id": "c1", "role": "assistant", "type": "answer",
                              "content": '{"branch": "商品咨询"}'}, ensure_ascii=False)
        resp = _sse_lines(
            ("conversation.message.delta", payload),
            ("done", "[DONE]"),
        )
        server.Handler._pump_stream(fake, resp, "token")
        kinds = [e for e, _ in fake.events]
        self.assertIn("error", kinds)
        self.assertNotIn("delta", kinds)


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

    def test_health_reports_uptime(self):
        """I-2：health 返回 uptime（进程存活秒数），配合定时探活证明服务在线。"""
        status, body = self._request("GET", "/api/health")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("uptime", data)
        self.assertIsInstance(data["uptime"], (int, float))
        self.assertGreaterEqual(data["uptime"], 0.0)

    def test_health_is_rate_limited_per_ip(self):
        server.HEALTH_LIMITER._hits.clear()
        try:
            with mock.patch.object(server, "HEALTH_PER_MINUTE", 1):
                first, _ = self._request("GET", "/api/health")
                second, body = self._request("GET", "/api/health")
        finally:
            server.HEALTH_LIMITER._hits.clear()
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        self.assertIn("频繁", json.loads(body)["error"])

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

    def test_reset_endpoint_works_without_token(self):
        """清空会话不依赖扣子令牌，后端不可用时也应能重置本地会话。"""
        with mock.patch.object(server, "COZE_API_TOKEN", ""):
            status, body = self._request("POST", "/api/reset")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("epoch", data)


# ----------------------------------------------------------------------
# 接管页前端约定（静态检查，防止再退回内联拼接 / 错误混报）
# ----------------------------------------------------------------------
class TakeoverPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "admin" / "takeover.html"
        cls.html = path.read_text(encoding="utf-8")

    def test_no_server_data_in_inline_handlers(self):
        """会话 ID 等数据不能再拼进内联 onclick 字符串。"""
        self.assertNotIn("onclick=\"selectItem('", self.html)
        self.assertNotIn("onclick=\"resolve('", self.html)

    def test_queue_uses_dataset_and_event_delegation(self):
        self.assertIn("div.dataset.sid = item.session_id", self.html)
        self.assertIn('addEventListener("click"', self.html)

    def test_auth_errors_are_separated_from_network_errors(self):
        """401/403 提示口令错误，网络失败单独提示，不能混成一句后端故障。"""
        self.assertIn("class AdminError", self.html)
        self.assertIn("r.status === 401 || r.status === 403", self.html)
        self.assertIn("r.status === 503", self.html)
        self.assertIn("管理口令不正确", self.html)
        self.assertIn('new AdminError("network"', self.html)
        self.assertNotIn("请确认 server.py 正在运行", self.html)

    def test_scroll_containers_are_height_constrained(self):
        """左右两栏必须锁在可用高度内，否则内容撑开容器、滚动条永远不出现。"""
        self.assertIn("grid-template-rows: minmax(0, 1fr)", self.html)
        self.assertIn("grid-template-columns: 380px minmax(0, 1fr)", self.html)
        self.assertIn("min-height: 0;", self.html)
        # 详情区头尾不参与压缩，滚动只发生在 .chat-flow
        self.assertIn("flex: 0 0 auto;", self.html)
        self.assertIn(".chat-flow {\n    flex: 1 1 auto; overflow-y: auto; padding: 28px;\n    min-height: 0;", self.html)

    def test_narrow_viewport_stacks_and_keeps_scrolling(self):
        """窄窗口改成上下堆叠，两块区域仍各自滚动。"""
        self.assertIn("@media (max-width: 900px)", self.html)
        self.assertIn("grid-template-rows: minmax(0, 34%) minmax(0, 1fr)", self.html)


# ----------------------------------------------------------------------
# 扫码弹窗前端口径（静态检查：库方法名错会让画布空白且无降级）
# ----------------------------------------------------------------------
class QRCodePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "index.html"
        cls.html = path.read_text(encoding="utf-8")

    def test_uses_existing_qrcode_generator_api(self):
        """vendor 里是 qrcode-generator@1.4.4，只有 renderTo2dContext，没有 draw。"""
        self.assertIn("renderTo2dContext(ctx, cellSize)", self.html)
        self.assertNotIn("qr.draw(", self.html)

    def test_generation_errors_fall_back_to_clickable_link(self):
        self.assertIn("catch (err)", self.html)
        self.assertIn("document.createElement('a')", self.html)


class HandoffStatusPageTest(unittest.TestCase):
    """转人工后右上角徽标必须同步变化，不能和聊天区提示互相矛盾。"""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "index.html"
        cls.html = path.read_text(encoding="utf-8")

    def test_status_badge_tracks_handoff_and_resets(self):
        self.assertIn(".status-pill.is-handoff", self.html)
        self.assertIn("statusText.textContent = '等待人工接入'", self.html)
        # 收到 handoff 事件时置位并立即重绘徽标
        self.assertIn("handoffActive = true", self.html)
        # 清空对话时复位，徽标回到「在线」
        self.assertIn("handoffActive = false", self.html)
        self.assertIn("renderStatusPill();", self.html)

    def test_handoff_messages_show_delivery_and_restore_state(self):
        """方案 B：转人工后只显示送达回执、不生成机器人气泡；刷新可恢复状态。"""
        self.assertIn("addDeliveredNotice(", self.html)
        self.assertIn("info.delivered", self.html)
        self.assertIn("!errored && !delivered", self.html)
        self.assertIn("/api/handoff", self.html)
        self.assertIn("restoreHandoffState();", self.html)

    def test_refresh_restores_history_without_posting(self):
        """刷新恢复走 GET /api/handoff 并回填历史，不能借恢复流程再发消息建单。"""
        self.assertIn("renderHistory(data.messages || [])", self.html)
        self.assertIn("function renderHistory(messages)", self.html)
        self.assertIn("let historyRendered = false;", self.html)
        self.assertIn("addMessageAt(text, m.role === 'assistant' ? 'bot' : 'user', m.ts)", self.html)
        # 恢复函数里不能有写请求（POST），刷新本身绝不能建单
        restore_block = self.html.split("async function restoreHandoffState()")[1].split("function showTyping")[0]
        self.assertNotIn("POST", restore_block)


if __name__ == "__main__":
    unittest.main()
