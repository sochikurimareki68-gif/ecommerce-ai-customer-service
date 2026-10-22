#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优眠小助手 · 轻量后端代理
=========================

作用：把"扣子智能体"包装成一个可以被浏览器安全调用的接口。

为什么需要它？
    扣子 API 需要携带个人访问令牌（PAT）。令牌一旦写进网页源码，
    任何人打开开发者工具就能抄走，等于把你的账号交出去。
    所以令牌只放在服务端（.env），浏览器只跟本服务打交道。

对外提供两个接口：
    GET  /api/health   →  告诉前端"后端是否就绪、令牌是否已配置"
    POST /api/chat     →  接收 {"message": "..."}，以 SSE 流式返回智能体的回复
                          （user_id / conversation_id 由服务端管理，客户端传了也不采信）

同时它也是个静态文件服务器，直接托管 index.html。

启动：
    python3 server.py
然后浏览器打开 http://localhost:3000

零第三方依赖，只用 Python 标准库。
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

COZE_API_BASE = os.environ.get("COZE_API_BASE", "https://api.coze.cn").rstrip("/")
COZE_BOT_ID = os.environ.get("COZE_BOT_ID", "7694865098007035940").strip()
COZE_API_TOKEN = os.environ.get("COZE_API_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")

# ----------------------------------------------------------------------
# 访问保护
#
# 这个 Demo 部署在公网，而每次 /api/chat 都会消耗扣子额度 —— 一个循环脚本
# 就能在几分钟内把额度刷光、让整个 Demo 挂掉。所以加四层基础防护：
#
#   1) 请求来源校验：挡住别的网站把它当免费接口用
#   2) 单 IP 限流：每分钟 / 每天各有上限
#   3) 全站每日总量上限：即使被换 IP 刷，也能兜底止损
#   4) 请求体大小与问题长度限制
#
# 都是单实例内存实现（Render 免费版只有一个实例，够用）。全部可用环境变量覆盖。
# ----------------------------------------------------------------------
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "4096"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "500"))
IP_PER_MINUTE = int(os.environ.get("IP_PER_MINUTE", "6"))
IP_PER_DAY = int(os.environ.get("IP_PER_DAY", "60"))
GLOBAL_PER_DAY = int(os.environ.get("GLOBAL_PER_DAY", "500"))
HEALTH_PER_MINUTE = int(os.environ.get("HEALTH_PER_MINUTE", "30"))
# 会话 Cookie 是否强制加 Secure；留空则按请求的 X-Forwarded-Proto 自动判断（HTTPS 部署加，本地不加）
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
EXTRA_ORIGINS = [
    o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]


class RateLimiter:
    """极简内存限流器：滑动窗口（每分钟）+ 计数（每天、全站）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}
        self._day = ""
        self._ip_day: dict[str, int] = {}
        self._global_day = 0

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._ip_day.clear()
            self._global_day = 0
        # 清理超过 60 秒没有活动的 IP，避免 _hits 字典无限增长（被公网扫描时尤其重要）
        now = time.time()
        for ip in [ip for ip, dq in self._hits.items() if not dq or now - dq[-1] > 60]:
            self._hits.pop(ip, None)

    def check(self, ip: str) -> tuple[bool, str]:
        """消耗一次配额。返回 (是否放行, 拒绝原因)。"""
        with self._lock:
            self._roll_day()
            now = time.time()

            if self._global_day >= GLOBAL_PER_DAY:
                return False, "今天的试用额度已经用完了，请明天再来～"

            # 取出该 IP 的滑动窗口；过期时间戳清理后若窗口已空，则从字典移除，
            # 否则只访问一次的 IP 会永久占一个条目，字典无限增长。
            hits = self._hits.get(ip)
            if hits is not None:
                while hits and now - hits[0] > 60:
                    hits.popleft()
                if not hits:
                    self._hits.pop(ip, None)
                    hits = None
            if hits is None:
                hits = deque()

            if len(hits) >= IP_PER_MINUTE:
                return False, "提问有点快哦，请等一分钟再试～"
            if self._ip_day.get(ip, 0) >= IP_PER_DAY:
                return False, "您今天的提问次数已达上限，请明天再来～"

            hits.append(now)
            self._hits[ip] = hits
            self._ip_day[ip] = self._ip_day.get(ip, 0) + 1
            self._global_day += 1
            return True, ""


LIMITER = RateLimiter()


class HealthLimiter:
    """健康检查专用限流：只按 IP 做每分钟窗口。

    单独一套，原因有两个：
    1. /api/health 不该占用「每日提问额度」（P0-3 的定时探活会一直打它）；
    2. 前端一次「重新连接」会连续探测约 30 次，正常使用不能因此被拒。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def allow(self, ip: str) -> bool:
        with self._lock:
            now = time.time()
            hits = self._hits.get(ip)
            if hits is not None:
                while hits and now - hits[0] > 60:
                    hits.popleft()
                if not hits:
                    self._hits.pop(ip, None)
                    hits = None
            if hits is None:
                hits = deque()

            if len(hits) >= HEALTH_PER_MINUTE:
                # 被刷时顺手清理过期条目，避免字典无限增长
                for other in [k for k, dq in self._hits.items() if not dq or now - dq[-1] > 60]:
                    self._hits.pop(other, None)
                return False

            hits.append(now)
            self._hits[ip] = hits
            return True


HEALTH_LIMITER = HealthLimiter()


# ----------------------------------------------------------------------
# 会话（由服务端持有）
#
# 如果把 user_id / conversation_id 交给客户端随便填，调用方就能伪造别人身份，
# 或者指定别人的会话 ID 去读写别人的对话。所以身份和会话都放在服务端，
# 客户端只拿一个不透明的 cookie。
# ----------------------------------------------------------------------
SESSION_COOKIE = "ym_session"
SESSION_TTL = 3 * 3600      # 3 小时不活动即回收
MAX_SESSIONS = 5000         # 上限，防止内存无限增长


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}

    def _sweep(self) -> None:
        now = time.time()
        for token in [t for t, s in self._data.items() if now - s["seen"] > SESSION_TTL]:
            self._data.pop(token, None)
        while len(self._data) > MAX_SESSIONS:
            self._data.pop(min(self._data, key=lambda t: self._data[t]["seen"]), None)

    def resolve(self, token: str) -> tuple[str, str, str, bool]:
        """返回 (token, user_id, conversation_id, 是否新建)。cookie 缺失/失效时新建。"""
        with self._lock:
            self._sweep()
            fresh = not token or token not in self._data
            if fresh:
                # _sweep 只能清理「插入前」已超限的情况，这里先腾出位置，保证不超过上限
                while self._data and len(self._data) >= MAX_SESSIONS:
                    self._data.pop(min(self._data, key=lambda t: self._data[t]["seen"]), None)
                token = secrets.token_urlsafe(24)
                self._data[token] = {
                    "user_id": f"web_{token[:12]}",   # 由服务端生成，客户端无法指定
                    "conversation_id": "",
                    "epoch": 0,                       # 会话世代：reset 后 +1，用于丢弃在途请求的晚到结果
                    "seen": time.time(),
                }
            session = self._data[token]
            session["seen"] = time.time()
            return token, session["user_id"], session["conversation_id"], fresh

    def reset(self, token: str) -> int:
        """清空会话上下文并让世代 +1，返回新世代。"""
        with self._lock:
            session = self._data.get(token)
            if not session:
                return 0
            session["epoch"] = session.get("epoch", 0) + 1
            session["conversation_id"] = ""
            session["seen"] = time.time()
            return session["epoch"]

    def epoch(self, token: str) -> int:
        with self._lock:
            session = self._data.get(token)
            return session.get("epoch", 0) if session else 0

    def set_conversation(self, token: str, conversation_id: str, epoch: int | None = None) -> None:
        with self._lock:
            session = self._data.get(token)
            if not session:
                return
            # 世代对不上，说明这轮对话期间用户点了「清空」，
            # 旧会话的 conversation_id 不能再写回去，否则新会话会被污染。
            if epoch is not None and session.get("epoch", 0) != epoch:
                return
            session["conversation_id"] = conversation_id


SESSIONS = SessionStore()

# 扣子常见错误码 → 人话
ERROR_HINTS = {
    4013: "访问令牌无效或已过期，请重新生成 PAT。",
    4014: "访问令牌缺少「Chat / 会话管理」权限，请在扣子重新生成并勾选。",
    4015: "智能体未发布，或与令牌不在同一个空间。",
    4016: "上一次对话还未结束，请稍后再发。",
    4030: "调用次数已达上限。",
    4100: "请求参数不合法。",
    4101: "访问令牌不正确，请核对 .env 中的 COZE_API_TOKEN。",
    4102: "访问令牌缺少调用该接口所需的权限。",
}


def build_ssl_context() -> ssl.SSLContext:
    """构造带可信根证书的 SSL 上下文。

    背景：python.org 安装的 Python 不自带 CA 证书包（openssl 目录下的 cert.pem 缺失），
    直接请求 https 会报 "certificate verify failed"。这里按优先级找一个可用的根证书包，
    不依赖任何第三方库。
    """
    candidates = [
        os.environ.get("COZE_CA_BUNDLE"),        # 允许手动指定
        "/etc/ssl/cert.pem",                     # macOS 系统根证书
        "/etc/ssl/certs/ca-certificates.crt",    # Debian / Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",      # CentOS / RHEL
    ]
    for path in candidates:
        if path and Path(path).is_file():
            try:
                return ssl.create_default_context(cafile=path)
            except ssl.SSLError:
                continue
    try:  # 兜底：如果环境里装了 certifi 就用它
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "YoumianProxy/1.9"   # 与项目当前版本对齐，方便线上排查

    # 本请求需要下发的 Set-Cookie（新建会话时才会设置）
    _pending_cookie = ""

    # ------------------------------------------------------------------
    # 基础响应工具
    # ------------------------------------------------------------------
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, error: str) -> None:
        """提前拒绝请求 —— 必须断开连接。

        这些分支在读取请求体之前就返回了。如果保持长连接，没读完的 body 会
        留在 socket 里，被当成下一个请求的起始行。反代（如 Render）会复用
        后端连接，于是后续请求全部损坏（实测报 "Bad request syntax"）。
        """
        self.close_connection = True
        self._send_json(status, {"error": error})

    # SSE 用 chunked 编码逐块下发，保证前端能实时看到文字
    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")  # 让 nginx 类反代不要缓冲
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
        self.end_headers()

    def _sse_send(self, event: str, data: str) -> None:
        block = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(f"{len(block):X}\r\n".encode("ascii") + block + b"\r\n")
        self.wfile.flush()

    def _sse_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ------------------------------------------------------------------
    # 访问保护
    # ------------------------------------------------------------------
    def _client_ip(self) -> str:
        """取真实客户端 IP。Render 之类平台会经反代，真实 IP 在 X-Forwarded-For 里。"""
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _read_cookie(self, name: str) -> str:
        """从 Cookie 头里取一个值（只取一个，够用）。"""
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _session_cookie(self, token: str) -> str:
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
        # HTTPS 部署下加 Secure（含反向代理场景）；本地 HTTP 不加，COOKIE_SECURE 强制开启
        if COOKIE_SECURE or self.headers.get("X-Forwarded-Proto") == "https":
            cookie += "; Secure"
        return cookie

    def _origin_allowed(self) -> bool:
        """校验请求来源。

        浏览器请求一定带 Origin；不带 Origin 的是脚本（curl / 评测脚本），
        由限流兜底，这里放行。所以这条规则拦的是「别的网站拿这个 Demo 当免费接口」。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"} | set(EXTRA_ORIGINS)
        return origin.rstrip("/") in allowed

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._pending_cookie = ""      # 同一连接会连续处理多个请求，进入时必须清空
        path = urlparse(self.path).path
        if path == "/api/health":
            if not HEALTH_LIMITER.allow(self._client_ip()):
                self._reject(429, "健康检查太频繁，请稍后再试")
                return
            self._send_json(200, {
                "ok": True,
                "bot_id": COZE_BOT_ID,
                "api_base": COZE_API_BASE,
                "token_ready": bool(COZE_API_TOKEN),
                "max_message_chars": MAX_MESSAGE_CHARS,
                "version": self.server_version,
            })
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        self._pending_cookie = ""      # 同上
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/reset":
            self._handle_reset()
            return
        self._reject(404, "接口不存在")

    # ------------------------------------------------------------------
    # 静态文件
    # ------------------------------------------------------------------
    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        target = (ROOT / rel).resolve()

        # 防目录穿越 & 防读取敏感文件
        if target != ROOT and ROOT not in target.parents:
            self._send_json(403, {"error": "forbidden"})
            return
        if any(part.startswith(".") for part in target.relative_to(ROOT).parts):
            self._send_json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # 清空会话
    # ------------------------------------------------------------------
    def _handle_reset(self) -> None:
        """清空当前会话上下文（前端「清空对话」）。

        只清服务端的 conversation_id，并让会话世代 +1：
        在途请求即使晚到，也会因为世代不匹配被 set_conversation 丢弃。
        """
        if not self._origin_allowed():
            self._reject(403, "请求来源不被允许")
            return

        token, _, _, _ = SESSIONS.resolve(self._read_cookie(SESSION_COOKIE))
        epoch = SESSIONS.reset(token)
        self._pending_cookie = self._session_cookie(token)
        self._send_json(200, {"ok": True, "epoch": epoch})

    # ------------------------------------------------------------------
    # 对话代理
    # ------------------------------------------------------------------
    def _handle_chat(self) -> None:
        if not COZE_API_TOKEN:
            self._reject(500, "服务端未配置 COZE_API_TOKEN，请先在 .env 中填写")
            return

        # ---- 第 1、2、3 层防护：来源校验 + 限流（先拦，避免白读请求体）----
        # 注意：这些分支都在读 body 之前返回，必须用 _reject 断开连接
        if not self._origin_allowed():
            self._reject(403, "请求来源不被允许")
            return
        allowed, reason = LIMITER.check(self._client_ip())
        if not allowed:
            self._reject(429, reason)
            return

        # ---- 第 4 层防护：请求体大小 ----
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reject(413, "请求体过大")
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reject(400, "请求体不是合法 JSON")
            return

        message = (payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"error": "message 不能为空"})
            return
        if len(message) > MAX_MESSAGE_CHARS:
            self._send_json(400, {"error": f"问题有点长，请控制在 {MAX_MESSAGE_CHARS} 字以内～"})
            return

        # 身份与会话都由服务端决定：客户端传什么都不采信。
        # 每次都下发 cookie —— 会话有效时相当于刷新有效期，用户持续使用就不会掉线。
        token, user_id, conversation_id, _ = SESSIONS.resolve(self._read_cookie(SESSION_COOKIE))
        epoch = SESSIONS.epoch(token)          # 记录本轮所处的会话世代
        self._pending_cookie = self._session_cookie(token)

        body = {
            "bot_id": COZE_BOT_ID,
            "user_id": user_id,
            "stream": True,              # 流式返回，前端可以逐字显示
            "auto_save_history": True,   # 保存会话，多轮才记得住上下文
            "additional_messages": [
                {"role": "user", "content": message, "content_type": "text"}
            ],
        }

        # 注意：conversation_id 是 query 参数，不是请求体字段
        url = f"{COZE_API_BASE}/v3/chat"
        if conversation_id:
            url += "?" + urlencode({"conversation_id": conversation_id})

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {COZE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except OSError:
                detail = ""
            self._send_json(exc.code, {
                "error": self._error_message(detail, f"扣子接口返回 HTTP {exc.code}"),
                "detail": detail[:500],
            })
            return
        except urllib.error.URLError as exc:
            self._send_json(502, {"error": f"无法连接扣子接口：{exc.reason}"})
            return

        # 扣子出错时会返回 HTTP 200 + JSON（而不是事件流），必须单独识别，
        # 否则前端会收到一个空流、只显示兜底话术，看不到真实原因。
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            detail = resp.read().decode("utf-8", "replace")
            resp.close()
            self._send_json(502, {
                "error": self._error_message(detail, "扣子接口没有返回流式数据"),
                "detail": detail[:500],
            })
            return

        self._sse_start()
        try:
            self._pump_stream(resp, token, epoch)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 用户中途关掉页面，正常情况
        except (OSError, http.client.IncompleteRead):
            # 其余异常（扣子侧超时、连接中断、TLS 错误等）：告知前端，
            # 避免页面一直挂在半截回复上等不到结束事件。发送失败说明连接也没了，忽略即可。
            try:
                self._sse_send("error", json.dumps({"message": "智能体连接中断，请稍后重试。"}, ensure_ascii=False))
            except OSError:
                pass
        finally:
            resp.close()
            try:
                self._sse_end()
            except OSError:
                pass

    def _pump_stream(self, resp, token: str, epoch: int | None = None) -> None:
        """把扣子的 SSE 事件翻译成前端更简单的三类事件：meta / delta / error。"""
        event = ""
        conversation_id = ""
        sent_meta = False
        sent_delta = False
        completed_answer = ""
        branch = ""
        # 结束节点有两种返回形态：纯文本（reply）与结构化 JSON（reply + branch）。
        # 结构化时 delta 里是 JSON 片段，必须先攒齐再解析，否则会把花括号吐给用户。
        structured = None
        pieces: list[str] = []

        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")

            if not line:            # 空行 = 一个事件的结束
                event = ""
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue

            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue

            if event == "error":
                self._sse_send("error", json.dumps({"message": self._error_message(data, "智能体返回了异常，请稍后再试。")}, ensure_ascii=False))
                return

            if event == "done":
                break

            if event not in ("conversation.message.delta", "conversation.message.completed"):
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            conversation_id = msg.get("conversation_id") or conversation_id
            if conversation_id and not sent_meta:
                # 记在服务端，下一轮由服务端自己带上，不依赖客户端回传
                SESSIONS.set_conversation(token, conversation_id, epoch)
                self._sse_send("meta", json.dumps({"conversation_id": conversation_id}, ensure_ascii=False))
                sent_meta = True

            if msg.get("role") != "assistant" or msg.get("type") != "answer":
                continue

            if event == "conversation.message.delta":
                piece = msg.get("content") or ""
                if not piece:
                    continue
                if structured is None:
                    structured = piece.lstrip().startswith("{")
                if structured:
                    pieces.append(piece)
                else:
                    self._sse_send("delta", json.dumps({"content": piece}, ensure_ascii=False))
                    sent_delta = True
            else:
                # 完成事件里带的是整段回复，作为兜底
                completed_answer = msg.get("content") or completed_answer

        if structured:
            reply, branch = self._split_structured(completed_answer or "".join(pieces))
            if not reply:
                # 解析失败不能把 JSON 原样丢给用户，给一句明确的提示
                self._sse_send("error", json.dumps({"message": "回复格式异常，请再发送一次～"}, ensure_ascii=False))
                return
            self._sse_send("delta", json.dumps({"content": reply}, ensure_ascii=False))
            sent_delta = True

        # 极端情况：没有增量（例如只给了完成事件），一次性补发
        if not sent_delta and completed_answer:
            self._sse_send("delta", json.dumps({"content": completed_answer}, ensure_ascii=False))
            sent_delta = True
        if conversation_id and not sent_meta:
            self._sse_send("meta", json.dumps({"conversation_id": conversation_id}, ensure_ascii=False))
        if branch:
            # 真实分类标签：前端忽略，评测脚本据此统计路由，不再靠关键词反推
            self._sse_send("branch", json.dumps({"branch": branch}, ensure_ascii=False))

        # 上游流异常结束：既没有增量也没有完成事件。直接发 done 会让前端留下空白气泡，
        # 这里改为下发 error，让用户看到明确提示而不是一段沉默。
        if not sent_delta:
            self._sse_send("error", json.dumps({"message": "响应超时了，请再发送一次～"}, ensure_ascii=False))
            return

        self._sse_send("done", "[DONE]")

    @staticmethod
    def _split_structured(raw: str) -> tuple[str, str]:
        """把结束节点返回的 {"reply": "...", "branch": "..."} 拆成回复与分类标签。

        解析不出 reply 时返回 ("", "")，由调用方给出提示——
        绝不能把 JSON 原文当答案显示给用户。
        """
        try:
            obj = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return "", ""
        if not isinstance(obj, dict):
            return "", ""
        reply = obj.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            return "", ""
        branch = obj.get("branch")
        return reply.strip(), branch.strip() if isinstance(branch, str) else ""

    @staticmethod
    def _error_message(detail: str, fallback: str) -> str:
        """从扣子的错误 JSON 里提取一句人话。"""
        try:
            obj = json.loads(detail)
        except (ValueError, json.JSONDecodeError):
            return fallback
        if not isinstance(obj, dict):
            return fallback
        try:
            code = int(obj.get("code"))
        except (TypeError, ValueError):
            code = None
        if code in ERROR_HINTS:
            return ERROR_HINTS[code]
        return obj.get("msg") or fallback

    def log_message(self, fmt: str, *args) -> None:  # 精简访问日志
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    if not COZE_API_TOKEN:
        print("⚠️  未检测到 COZE_API_TOKEN —— /api/chat 会返回错误。")
        print("   请在本目录的 .env 中填写你的扣子个人访问令牌（pat_ 开头）。\n")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"▶ 优眠小助手代理已启动： http://localhost:{PORT}")
    print(f"  bot_id = {COZE_BOT_ID}")
    print(f"  api    = {COZE_API_BASE}")
    print(f"  token  = {'已配置 ✅' if COZE_API_TOKEN else '未配置 ❌'}")
    print("  按 Ctrl+C 停止\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
