# -*- coding: utf-8 -*-
"""G2 防复发闸门的单元测试（2026-11-09）。

只验证「心跳可观测」逻辑，不依赖扣子接口：
  · /api/health 必须暴露 last_activity_at / keepalive_ok 等字段
  · 心跳超过窗口后 keepalive_ok 必须变 false
  · 真实入站流量会刷新心跳，健康检查自身不会
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class KeepaliveIntegrationTest(unittest.TestCase):
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

    def setUp(self):
        self._last = server._LAST_ACTIVITY
        server._LAST_ACTIVITY = time.time()

    def tearDown(self):
        server._LAST_ACTIVITY = self._last

    def _get(self, path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            try:
                return resp.status, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return resp.status, {}
        finally:
            conn.close()

    def _health(self) -> dict:
        status, data = self._get("/api/health")
        self.assertEqual(status, 200)
        return data

    def test_health_exposes_keepalive_fields(self):
        data = self._health()
        for key in ("last_activity_at", "seconds_since_activity", "keepalive_window_s", "keepalive_ok"):
            self.assertIn(key, data)
        self.assertEqual(data["version"], f"YoumianProxy/{server.__version__}")

    def test_keepalive_false_when_stale(self):
        server._LAST_ACTIVITY = time.time() - server.KEEPALIVE_MAX_GAP_S - 5
        self.assertFalse(self._health()["keepalive_ok"])

    def test_keepalive_true_after_traffic(self):
        server._LAST_ACTIVITY = time.time() - server.KEEPALIVE_MAX_GAP_S - 5
        status, _ = self._get("/")
        self.assertEqual(status, 200)
        self.assertTrue(self._health()["keepalive_ok"])

    def test_health_request_does_not_refresh_heartbeat(self):
        stale = time.time() - server.KEEPALIVE_MAX_GAP_S - 5
        server._LAST_ACTIVITY = stale
        self._health()
        self.assertAlmostEqual(server._LAST_ACTIVITY, stale, places=3)

    def test_snapshot_shape(self):
        snap = server.keepalive_snapshot()
        self.assertIsInstance(snap["keepalive_ok"], bool)
        self.assertGreaterEqual(snap["seconds_since_activity"], 0.0)


if __name__ == "__main__":
    unittest.main()
