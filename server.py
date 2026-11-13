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
import hashlib
import json
import mimetypes
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent

from kb_numeric_guard import (  # noqa: E402  （同目录模块，必须在 ROOT 之后导入）
    find_violation as find_numeric_violation,
    load_manifest as load_numeric_manifest,
)
from kb_fact_answer import (  # noqa: E402  （同目录模块，必须在 ROOT 之后导入）
    load_index as load_fact_index,
    match_fact,
)


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
# P2-4 成本指标：扣子 v3 流式不返回 token 用量，按字符数本地估算（中文 1 字≈1 token，英文≈4 字符 1 token）。
# 价格单位：元 / 千 token，默认按豆包 Pro 输入 0.0008、输出 0.002，可在 .env 覆盖。
COZE_IN_PRICE = float(os.environ.get("COZE_IN_PRICE", "0.0008"))
COZE_OUT_PRICE = float(os.environ.get("COZE_OUT_PRICE", "0.002"))
# 扣子系统提示词的固定 token 开销估算（助手快照注入后，模型实际输入还含 bot 自带 prompt）
PROMPT_OVERHEAD_TOK = int(os.environ.get("PROMPT_OVERHEAD_TOK", "300"))
# P2-4 FAQ 缓存：只缓存会话首问（turn_count==0），相同问题直接回放，省一次模型调用。TTL 1 小时，最多 200 条。
# 首问判定用轮次而不是 epoch：epoch 在 reset 后会持续 +1，用它判首问会导致「清空对话」后再也不命中缓存。
FAQ_CACHE_TTL = int(os.environ.get("FAQ_CACHE_TTL", "3600"))
FAQ_CACHE_MAX = int(os.environ.get("FAQ_CACHE_MAX", "200"))

# 数值守卫：默认开启，NUMERIC_GUARD_ENABLED=0 可单独关闭灰度。
NUMERIC_GUARD_ENABLED = os.environ.get("NUMERIC_GUARD_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
NUMERIC_MANIFEST = None
_NUMERIC_MANIFEST_PATH = Path(
    os.environ.get("KB_NUMERIC_MANIFEST", str(ROOT / "data" / "kb_numeric_manifest.json"))
)
if not _NUMERIC_MANIFEST_PATH.is_absolute():
    _NUMERIC_MANIFEST_PATH = ROOT / _NUMERIC_MANIFEST_PATH
try:
    NUMERIC_MANIFEST = load_numeric_manifest(_NUMERIC_MANIFEST_PATH)
except (OSError, ValueError) as exc:
    print(json.dumps(
        {"level": "guard", "kind": "manifest_load_failed", "error": str(exc)},
        ensure_ascii=False,
    ))

# 确定性回答层（L1）：价格、规格、克重、水温、时限等结构化事实直接查本地索引返回，
# 命中时不调用扣子。FACT_ANSWER_ENABLED=0 关闭命中直返，FACT_FALLBACK_ENABLED=0 关闭兜底。
FACT_ANSWER_ENABLED = os.environ.get("FACT_ANSWER_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
FACT_FALLBACK_ENABLED = os.environ.get("FACT_FALLBACK_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
FACT_INDEX = None
_FACT_INDEX_PATH = Path(
    os.environ.get("KB_FACT_INDEX", str(ROOT / "data" / "kb_fact_index.json"))
)
if not _FACT_INDEX_PATH.is_absolute():
    _FACT_INDEX_PATH = ROOT / _FACT_INDEX_PATH
try:
    FACT_INDEX = load_fact_index(_FACT_INDEX_PATH)
except (OSError, ValueError) as exc:
    print(json.dumps(
        {"level": "fact", "kind": "index_load_failed", "error": str(exc)},
        ensure_ascii=False,
    ))

# 会话 Cookie 是否强制加 Secure；留空则按请求的 X-Forwarded-Proto 自动判断（HTTPS 部署加，本地不加）
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
# 人工接管接口（/api/pending、/api/session/*、/api/resolve）的管理口令。
# 未配置时默认「全部拒绝」——宁可接管页打不开，也不能让待接管对话在公网裸奔。
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
# I-4 人工接管：转人工时连同最近 N 轮上下文一起入队，人工接手时能看到前因后果。
# 5 轮 = 最近 5 组「用户提问 + 助手回复」；超出自动丢弃，避免内存膨胀。
HANDOFF_CONTEXT_ROUNDS = max(1, int(os.environ.get("HANDOFF_CONTEXT_ROUNDS", "5")))
HISTORY_MAX_MESSAGES = HANDOFF_CONTEXT_ROUNDS * 2
EXTRA_ORIGINS = [
    o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
# I-2：进程启动时间戳，/api/health 的 uptime 字段（秒）由此计算，配合定时探活证明服务持续在线
_PROCESS_START = time.time()

# G4：README 事实区从这里读版本号；G2 的心跳字段也依赖下面的状态
__version__ = "2.0.3"

# ----------------------------------------------------------------------
# G2 防复发闸门（2026-11-09）：keepalive 配了但没生效
#
# 真实事故：Actions 的 5 分钟定时任务被 GitHub 降频到 3.5–7 小时，实例照常休眠；
# 而 health 里只有一个 uptime，看不出「最近一次真实流量是什么时候」，所以没人发现。
# 这里记录「最近一次非健康检查的入站请求」时刻，health 暴露 last_activity_at 与
# keepalive_ok。外部探活每 5 分钟打一次首页，字段就会持续更新；超过窗口即判定失效。
# 注意：/api/health 自身不刷新心跳，否则探针永远不会显示失效。
# ----------------------------------------------------------------------
_ACTIVITY_LOCK = threading.Lock()
_LAST_ACTIVITY = time.time()
KEEPALIVE_MAX_GAP_S = int(os.environ.get("KEEPALIVE_MAX_GAP_S", "1200"))


def mark_activity() -> None:
    """记录一次真实入站流量（健康检查除外）。"""
    global _LAST_ACTIVITY
    with _ACTIVITY_LOCK:
        _LAST_ACTIVITY = time.time()


def keepalive_snapshot() -> dict:
    now = time.time()
    gap = max(0.0, now - _LAST_ACTIVITY)
    return {
        "last_activity_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_LAST_ACTIVITY)),
        "seconds_since_activity": round(gap, 1),
        "keepalive_window_s": KEEPALIVE_MAX_GAP_S,
        "keepalive_ok": gap <= KEEPALIVE_MAX_GAP_S,
    }


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
                    "turn_count": 0,                  # 已完成轮次：判定会话首问（FAQ 缓存），reset 清零
                    "history": [],                    # 最近若干轮问答：转人工时随队列交给人工
                    "slots": {},                      # M-2 槽位：跨轮记住订单尾号/床型等关键信息
                    "handoff": None,                  # 转人工状态：{"ticket","since"}，reset 清除
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
            session["turn_count"] = 0                 # 清空对话 = 重新算首问，缓存可再次命中
            session["history"] = []                   # 旧上下文一并清掉
            session["slots"] = {}                     # 清空会话时槽位一并清掉（M-2）
            session["handoff"] = None                 # 清空对话 = 结束本次转人工，恢复正常问答
            session["seen"] = time.time()
            return session["epoch"]

    def epoch(self, token: str) -> int:
        with self._lock:
            session = self._data.get(token)
            return session.get("epoch", 0) if session else 0

    def turn_count(self, token: str) -> int:
        """已完成轮次数。0 表示这是会话首问（reset 后会重新归零）。"""
        with self._lock:
            session = self._data.get(token)
            return int(session.get("turn_count", 0)) if session else 0

    def record_turn(self, token: str, user_text: str, reply_text: str,
                    epoch: int | None = None) -> None:
        """记录一轮完整问答：轮次 +1，并把最近若干轮留在会话里。

        世代对不上（用户中途点了「清空」）说明这轮属于旧会话，整轮丢弃，
        避免旧上下文污染新世代；空文本一侧不入历史，但轮次照常累加。
        """
        with self._lock:
            session = self._data.get(token)
            if not session:
                return
            if epoch is not None and session.get("epoch", 0) != epoch:
                return
            session["turn_count"] = session.get("turn_count", 0) + 1
            history = session.setdefault("history", [])
            now = time.time()
            for role, text in (("user", user_text), ("assistant", reply_text)):
                if text:
                    history.append({"role": role, "text": text, "ts": now})
            if len(history) > HISTORY_MAX_MESSAGES:
                del history[:-HISTORY_MAX_MESSAGES]

    def history(self, token: str) -> list[dict]:
        """返回最近若干轮问答的副本（最多 HISTORY_MAX_MESSAGES 条）。"""
        with self._lock:
            session = self._data.get(token)
            if not session:
                return []
            return [dict(m) for m in session.get("history", [])]

    # ---- 方案 B：转人工后对话主权交给人工 ----
    def handoff(self, token: str) -> dict | None:
        """返回当前会话的转人工状态；未转人工/会话不存在时返回 None。"""
        with self._lock:
            session = self._data.get(token)
            state = session.get("handoff") if session else None
            return dict(state) if state else None

    def enter_handoff(self, token: str, ticket: str) -> None:
        with self._lock:
            session = self._data.get(token)
            if not session:
                return
            if not session.get("handoff"):
                session["handoff"] = {"ticket": ticket, "since": time.time()}

    def clear_handoff(self, token: str) -> None:
        """仅清掉转人工标记（工单已处理完时用），不影响历史与轮次。"""
        with self._lock:
            session = self._data.get(token)
            if session:
                session["handoff"] = None

    def append_user_message(self, token: str, text: str,
                            epoch: int | None = None) -> bool:
        """转人工后把用户消息记进会话历史（不增加轮次，不参与 FAQ 缓存）。"""
        with self._lock:
            session = self._data.get(token)
            if not session:
                return False
            if epoch is not None and session.get("epoch", 0) != epoch:
                return False
            history = session.setdefault("history", [])
            history.append({"role": "user", "text": text, "ts": time.time()})
            if len(history) > HISTORY_MAX_MESSAGES:
                del history[:-HISTORY_MAX_MESSAGES]
            return True

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

    def merge_slots(self, token: str, found: dict) -> dict:
        """把本轮抽到的槽位合并进会话，返回当前完整槽位（M-2）。"""
        with self._lock:
            session = self._data.get(token)
            if not session:
                return {}
            slots = session.setdefault("slots", {})
            slots.update(found)
            return dict(slots)


SESSIONS = SessionStore()

# ----------------------------------------------------------------------
# M-2 关键信息槽位化
#
# 扣子没有「会话级变量」可用（应用变量每请求重置、用户变量跨会话串扰），
# 所以槽位记在服务端的会话里：每收到一条用户消息就按规则抽一次，合并进
# 该会话的 slots，再以一条 [SLOTS_JSON] 助手快照的形式随请求发给扣子
# （auto_save_history 会把它存进会话历史，回复节点从 chatHistory 里读到）。
# 抽取只用确定性规则，宁缺毋滥 —— 抽不到就保持空，由提示词约束「空则追问」。
# ----------------------------------------------------------------------
SLOT_FABRIC_WORDS = ("有机棉", "长绒棉", "全棉", "纯棉", "天丝", "真丝", "磨毛", "亚麻")
SLOT_SKU_WORDS = ("四件套", "被芯", "枕芯", "枕头", "床垫", "床笠", "被套", "床单", "毛毯")

RE_ORDER_TAIL = re.compile(r"(?:尾号|订单号|单号)\s*[:：]?\s*(\d{4})")
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_BED_SIZE = re.compile(r"(1\.2|1\.5|1\.8|2\.0)\s*(?:米|m(?![a-zA-Z])|M(?![a-zA-Z]))")


def extract_slots(message: str) -> dict:
    """从一条用户消息里抽取关键槽位，抽到什么返回什么。"""
    found: dict[str, str] = {}
    m = RE_ORDER_TAIL.search(message)
    if m:
        found["order_no"] = f"尾号{m.group(1)}"
    else:
        m = RE_PHONE.search(message)
        if m:
            found["order_no"] = f"手机{m.group(0)}"
    m = RE_BED_SIZE.search(message)
    if m:
        found["bed_size"] = f"{m.group(1)}米"
    for word in SLOT_FABRIC_WORDS:
        if word in message:
            found["fabric"] = word
            break
    for word in SLOT_SKU_WORDS:
        if word in message:
            found["sku"] = word
            break
    return found

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


def estimate_tokens(text: str) -> int:
    """字节系 tokenizer 近似：中文 1 字≈1 token，英文/数字≈4 字符 1 token。"""
    if not text:
        return 0
    cn = sum(1 for c in text if ord(c) > 127)
    other = len(text) - cn
    return max(1, round(cn + other / 4))


# ----------------------------------------------------------------------
# v2.0 分支推断
#
# v2.0 画布改为「开始 → 单一LLM → 结束」，结束节点只返回 reply 纯文本，
# 不再输出代码节点判定的 branch 字段。这里按关键词从「用户消息 + 回复」
# 推断走了哪条分支，供业务日志与评测脚本统计路由。
# 关键词口径与 eval/dataset.json 的 branch_keywords 保持一致（评测脚本
# 在无服务端标签时也用同一套词反推，两边结论可互相印证）。
# ----------------------------------------------------------------------
BRANCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "商品咨询": ("尺寸", "被套", "床单", "枕", "材质", "面料", "规格", "克重", "支数", "颜色", "款式", "推荐", "套件"),
    "洗护指南": ("洗涤", "清洗", "水温", "晾晒", "晾干", "机洗", "洗衣机", "洗涤剂", "缩水", "起球", "保养", "浸泡", "烘干", "洗"),
    "物流发货": ("发货", "快递", "物流", "48", "运费", "包邮", "签收", "到货", "运输", "配送"),
    "退换售后": ("退货", "换货", "退款", "售后", "7天", "无理由", "寄回", "验货", "退货流程"),
    "优惠活动": ("满减", "优惠券", "折扣", "领券", "满199", "活动价", "积分", "会员", "活动"),
    "其他兜底": ("正在为您转接", "欢迎来到优眠家居", "回答不了", "帮不了", "议价", "智能客服小助手", "转接人工"),
}
# 命中数相同时的优先级，与旧意图识别的分支顺序一致
BRANCH_ORDER = ("商品咨询", "洗护指南", "物流发货", "退换售后", "优惠活动", "其他兜底")

# 回复里出现这些话术说明这轮实际转人工了（分支仍按内容归为「其他兜底」，与评测期望一致）
HANDOFF_MARKERS = ("正在为您转接", "转接人工客服")

# 方案 B：知识库无答案时，提示词只输出标记，话术统一由服务端替换。
# 这样改一句兜底话术不用回去改 5 个提示词、也不用重新发布对话流。
NO_ANSWER_MARKER = "[[NO_ANSWER]]"
# 发票类单独走一条兜底：话术必须含「正在为您转接」「转接人工客服」，确保仍能建工单。
NO_ANSWER_INVOICE_MARKER = "[[NO_ANSWER_INVOICE]]"
NO_ANSWER_MARKERS = (NO_ANSWER_MARKER, NO_ANSWER_INVOICE_MARKER)
NO_ANSWER_REPLY = (
    "亲，这边暂时没有查到准确资料，为了不耽误您，正在为您转接人工客服，"
    "请稍等~ 您也可以回复“转人工”，由人工客服继续跟进"
)
NO_ANSWER_INVOICE_REPLY = (
    "亲～发票需要人工为您开具，正在为您转接人工客服，请稍等~ "
    "您也可以回复“转人工”，由人工客服继续跟进"
)
NO_ANSWER_REPLIES = {
    NO_ANSWER_MARKER: NO_ANSWER_REPLY,
    NO_ANSWER_INVOICE_MARKER: NO_ANSWER_INVOICE_REPLY,
}
# 未登记的占位符（如 [[SOME_MARK]]）同样不能漏给用户；能识别就按兜底处理，否则删掉标记。
GENERIC_PLACEHOLDER_RE = re.compile(r"\[\[[A-Z][A-Z0-9_]*\]\]")


def log_guard_event(kind: str, **fields) -> None:
    """守卫结构化日志。与访问日志分开，level=guard 便于线上单独检索。"""
    record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": "guard", "kind": kind}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False))


def replace_no_answer_marker(text: str) -> str:
    """只含标记 → 整段换成对应兜底话术；标记夹在实质答复里 → 只删标记，不追加话术。"""
    hit = next((marker for marker in NO_ANSWER_MARKERS if marker in text), "")
    found = [marker for marker in NO_ANSWER_MARKERS if marker in text]
    found.extend(GENERIC_PLACEHOLDER_RE.findall(text))
    if not found:
        return text
    stripped = text
    for marker in NO_ANSWER_MARKERS:
        stripped = stripped.replace(marker, "")
    stripped = GENERIC_PLACEHOLDER_RE.sub("", stripped)
    only_marker = stripped.strip() == ""
    log_guard_event(
        "placeholder",
        markers=sorted(set(found)),
        action="fallback" if only_marker else "stripped",
    )
    if only_marker:
        if hit:
            return NO_ANSWER_REPLIES[hit]
        return NO_ANSWER_REPLY
    return stripped


def split_no_answer_stream(buffer: str) -> tuple[str, str]:
    """流式下发的切分：返回 (可立即下发的内容, 继续暂缓的尾部)。

    标记不能泄漏给前端，所以整段只有标记时先扣住，等流结束再决定是否替换；
    标记前后已有实质内容时只删标记本身。普通、发票和未登记标记都要覆盖。
    """
    hits = [(buffer.find(marker), marker) for marker in NO_ANSWER_MARKERS if marker in buffer]
    if hits:
        idx, marker = min(hits, key=lambda item: item[0])
        before = buffer[:idx]
        after = buffer[idx + len(marker):]
        if not before.strip() and not after.strip():
            return "", buffer
        cleaned = buffer
        for m in NO_ANSWER_MARKERS:
            cleaned = cleaned.replace(m, "")
        return cleaned, ""
    generic = GENERIC_PLACEHOLDER_RE.search(buffer)
    if generic:
        before = buffer[:generic.start()]
        after = buffer[generic.end():]
        if not before.strip() and not after.strip():
            return "", buffer
        return GENERIC_PLACEHOLDER_RE.sub("", buffer), ""
    # 没有完整标记，但尾部可能是某个标记的前半截，也要暂缓
    hold = 0
    for marker in NO_ANSWER_MARKERS:
        for size in range(min(len(buffer), len(marker) - 1), hold, -1):
            if buffer.endswith(marker[:size]):
                hold = size
                break
    partial = re.search(r"\[\[[A-Z0-9_]*$", buffer)
    if partial:
        hold = max(hold, len(buffer) - partial.start())
    if hold:
        return buffer[:-hold], buffer[-hold:]
    return buffer, ""


def infer_branch(message: str, reply: str) -> str:
    """按关键词命中数推断分支。回复内容权重高于用户消息；平分时按 BRANCH_ORDER 取先。"""
    best = ""
    best_score = 0
    for name in BRANCH_ORDER:
        kws = BRANCH_KEYWORDS[name]
        score = sum(2 for kw in kws if kw in reply) + sum(1 for kw in kws if kw in message)
        if score > best_score:
            best, best_score = name, score
    return best or "其他兜底"


def guard_completed_reply(text: str, message: str) -> tuple[str, object]:
    """非流式路径整段校验：返回 (可下发文本, 冲突对象或 None)。"""
    if not NUMERIC_GUARD_ENABLED or NUMERIC_MANIFEST is None or not text:
        return text, None
    violation = find_numeric_violation(text, message, NUMERIC_MANIFEST)
    if violation is not None:
        return NO_ANSWER_REPLY, violation
    return text, None


def lookup_fact(message: str) -> dict | None:
    """L1 查表：命中返回 {"reply", "entry", "score"}；未开启或未命中返回 None。"""
    if not FACT_ANSWER_ENABLED or FACT_INDEX is None or not message:
        return None
    try:
        return match_fact(message, FACT_INDEX)
    except (KeyError, TypeError, ValueError) as exc:
        log_guard_event("fact_lookup_failed", error=str(exc))
        return None


def fact_fallback_reply(message: str) -> str:
    """L3 兜底：扣子空回复或只回标记时，用知识库原文补一段；没命中返回空串。"""
    if not FACT_FALLBACK_ENABLED or FACT_INDEX is None or not message:
        return ""
    try:
        hit = match_fact(message, FACT_INDEX)
    except (KeyError, TypeError, ValueError) as exc:
        log_guard_event("fact_fallback_failed", error=str(exc))
        return ""
    return hit["reply"] if hit else ""


_STREAM_BOUNDARY_RE = re.compile(r"[。！？!?；;\n，,]")
_DIGIT_RE = re.compile(r"\d")


def _last_boundary_cut(text: str, limit: int) -> int:
    cut = 0
    for idx in range(min(limit, len(text))):
        if text[idx] in "。！？!?；;\n，,":
            cut = idx + 1
    return cut


class NumericGuardStream:
    """流式数值守卫：只暂扣含数字的句段到标点边界，其余内容即时放行。

    不含数字的内容完全不缓冲，保持原有逐字体验；含数字的句段在边界处
    统一校验，冲突时丢弃该句段并下发兜底，避免错数字漏给用户。
    """

    def __init__(self, message: str):
        self.message = message or ""
        self.pending = ""
        self.context = ""
        self.blocked = False
        self.violation = None

    def _check(self, segment: str):
        return find_numeric_violation(
            segment, self.message + "\n" + self.context, NUMERIC_MANIFEST
        )

    def feed(self, text: str) -> list[str]:
        if not text or self.blocked:
            return []
        if not NUMERIC_GUARD_ENABLED or NUMERIC_MANIFEST is None:
            return [text]
        self.pending += text
        out: list[str] = []
        while self.pending:
            digit = _DIGIT_RE.search(self.pending)
            if digit is None:
                out.append(self.pending)
                self.context = (self.context + self.pending)[-80:]
                self.pending = ""
                break
            boundary = _STREAM_BOUNDARY_RE.search(self.pending, digit.start())
            if boundary is None:
                cut = _last_boundary_cut(self.pending, digit.start())
                if cut:
                    out.append(self.pending[:cut])
                    self.context = (self.context + self.pending[:cut])[-80:]
                    self.pending = self.pending[cut:]
                break
            end = boundary.end()
            segment = self.pending[:end]
            violation = self._check(segment)
            if violation is not None:
                self.blocked = True
                self.violation = violation
                out.append(NO_ANSWER_REPLY)
                self.pending = ""
                break
            out.append(segment)
            self.context = (self.context + segment)[-80:]
            self.pending = self.pending[end:]
        return out

    def finish(self) -> list[str]:
        if self.blocked or not self.pending:
            return []
        segment = self.pending
        self.pending = ""
        if not NUMERIC_GUARD_ENABLED or NUMERIC_MANIFEST is None:
            return [segment]
        violation = self._check(segment)
        if violation is not None:
            self.blocked = True
            self.violation = violation
            return [NO_ANSWER_REPLY]
        return [segment]


# FAQ 首问缓存：key=归一化问题，value=(reply, branch, 写入时间戳)
FAQ_CACHE: dict[str, tuple[str, str, float]] = {}
FAQ_CACHE_LOCK = threading.Lock()

# 否定性/未命中类回答不进缓存：这类回答往往只是当次检索没召回，
# 固化 3600 秒会把一次坏答案反复回放。按语义特征匹配，不写死整句。
NEGATIVE_ANSWER_RE = re.compile(
    r"没有(?:查询|查|找到|匹配|检索)到|没有查到|查不到|未找到|"
    r"没有相关|无相关|暂无(?:相关|匹配|资料)|暂时没有(?:查到|相关|资料)|"
    r"核对下?商品名称|请(?:您)?核对|换个(?:说法|关键词|问法)"
)


def faq_cache_get(key: str) -> tuple[str, str] | None:
    with FAQ_CACHE_LOCK:
        item = FAQ_CACHE.get(key)
        if not item:
            return None
        reply, branch, ts = item
        if time.time() - ts > FAQ_CACHE_TTL:
            FAQ_CACHE.pop(key, None)
            return None
        return reply, branch


def cache_skip_reason(reply: str) -> str:
    """判断回复是否不适合写缓存；返回原因，空串表示可缓存。"""
    text = reply or ""
    if not text.strip():
        return "empty"
    if GENERIC_PLACEHOLDER_RE.search(text):
        return "placeholder"
    if NEGATIVE_ANSWER_RE.search(text):
        return "negative"
    return ""


def faq_cache_put(key: str, reply: str, branch: str) -> None:
    reason = cache_skip_reason(reply)
    if reason:
        # 明确记录「跳过写缓存」：与「没命中缓存」区分开，便于线上排查坏答案固化。
        log_guard_event("cache_skip", reason=reason, reply=(reply or "")[:80])
        return
    with FAQ_CACHE_LOCK:
        if len(FAQ_CACHE) >= FAQ_CACHE_MAX:
            # 淘汰最早写入的一条
            oldest = min(FAQ_CACHE, key=lambda k: FAQ_CACHE[k][2])
            FAQ_CACHE.pop(oldest, None)
        FAQ_CACHE[key] = (reply, branch, time.time())

# ==================== I-4 人工接管轻量版 ====================
# 转人工时自动入队，供只读接管页 /admin/takeover.html 查看。
# 数据结构：pending_handoff[session_id] = {
#     "ts": 入队时间戳,
#     "user_ip_hash": IP 短哈希（脱敏）,
#     "messages": 最近 HANDOFF_CONTEXT_ROUNDS 轮的问答（含触发转人工的这一轮）,
#     "last_reply": 转接话术原文,
#     "resolved": False,
# }
PENDING_HANDOFF: dict[str, dict] = {}
PENDING_HANDOFF_LOCK = threading.Lock()
PENDING_HANDOFF_MAX = 200          # 队列上限，防内存泄漏

def _handoff_position_locked(session_id: str) -> int:
    """调用方必须持有 PENDING_HANDOFF_LOCK。返回 1 起的排位，0 表示不在队列。"""
    unresolved = [k for k in sorted(
        PENDING_HANDOFF, key=lambda k: PENDING_HANDOFF[k]["ts"]
    ) if not PENDING_HANDOFF[k].get("resolved")]
    return unresolved.index(session_id) + 1 if session_id in unresolved else 0


def record_handoff(session_id: str, user_ip: str, last_reply: str,
                   messages: list[dict]) -> int:
    """把一次转人工会话入队（幂等，同 session 只记录第一次）。

    返回该会话在当前待接管队列中的排位（从 1 开始）。连续发「转人工」时
    仍返回第一次入队的位置，前端据此展示「第 N 位」而不是重复建单。
    """
    if not session_id:
        return 0
    with PENDING_HANDOFF_LOCK:
        if session_id not in PENDING_HANDOFF:
            # 队列满时淘汰最旧（按 ts 排序）
            if len(PENDING_HANDOFF) >= PENDING_HANDOFF_MAX:
                oldest = min(PENDING_HANDOFF, key=lambda k: PENDING_HANDOFF[k]["ts"])
                PENDING_HANDOFF.pop(oldest, None)
            PENDING_HANDOFF[session_id] = {
                "ts": time.time(),
                "user_ip_hash": _hash_for_log(user_ip),
                "messages": list(messages),        # 浅拷贝，防止后续修改
                "last_reply": last_reply,
                "resolved": False,
            }
        return _handoff_position_locked(session_id)


def append_handoff_message(session_id: str, text: str, user_ip: str) -> int:
    """方案 B：转人工后用户消息追加到同一工单，不新建工单，返回排位。

    工单已被人工处理掉时（被 resolve 移出队列），重新建一条，保证消息不丢。
    """
    if not session_id:
        return 0
    now = time.time()
    with PENDING_HANDOFF_LOCK:
        entry = PENDING_HANDOFF.get(session_id)
        if entry is None:
            if len(PENDING_HANDOFF) >= PENDING_HANDOFF_MAX:
                oldest = min(PENDING_HANDOFF, key=lambda k: PENDING_HANDOFF[k]["ts"])
                PENDING_HANDOFF.pop(oldest, None)
            entry = PENDING_HANDOFF[session_id] = {
                "ts": now,
                "user_ip_hash": _hash_for_log(user_ip),
                "messages": [],
                "last_reply": "",
                "resolved": False,
            }
        entry["messages"].append({"role": "user", "text": text, "ts": now})
        return _handoff_position_locked(session_id)

# 日志里不能存明文 IP / 会话 token，统一截断成短哈希，够定位同一会话即可。
def _hash_for_log(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"YoumianProxy/{__version__}"   # G4：版本号唯一来源是 __version__

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
        """取真实客户端 IP —— 限流按它分桶。

        线上前面有一层 Cloudflare：优先用 `CF-Connecting-IP`（由 Cloudflare 覆写，
        客户端伪造不了）；`X-Forwarded-For` 只能退而求其次 —— 它通常是「追加」，
        最左值可能是客户端自己填的假值，所以单 IP 限额只能算尽力而为，真正兜底的
        是全站每日上限。
        """
        cf_ip = (self.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip
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
        # HTTPS 部署（含反向代理）用 SameSite=None + Secure：挂件以 iframe 嵌在别的站点时，
        # Cookie 属于跨站上下文，Lax 会被浏览器丢弃，导致嵌入版多轮记忆失效。
        # 本地 HTTP 调试没法用 Secure，只能保持 Lax（同站页面不受影响）。
        secure = COOKIE_SECURE or self.headers.get("X-Forwarded-Proto") == "https"
        same_site = "None" if secure else "Lax"
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite={same_site}; Max-Age={SESSION_TTL}"
        if secure:
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

    def _admin_allowed(self) -> tuple[bool, str]:
        """校验人工接管接口的管理口令。

        口令优先取请求头 `X-Admin-Token`，其次取 query 参数 `token`
        （方便本地 curl 调试）。未配置 ADMIN_TOKEN 时一律拒绝：接管接口里
        是真实用户对话，宁可默认关闭，也不能在公网裸奔。
        """
        if not ADMIN_TOKEN:
            return False, "服务端未配置 ADMIN_TOKEN，接管接口默认关闭"
        supplied = (self.headers.get("X-Admin-Token") or "").strip()
        if not supplied:
            query = parse_qs(urlparse(self.path).query)
            supplied = (query.get("token") or [""])[0].strip()
        if not supplied or not secrets.compare_digest(
                supplied.encode("utf-8"), ADMIN_TOKEN.encode("utf-8")):
            return False, "管理口令不正确"
        return True, ""

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._req_start = time.time()
        self._pending_cookie = ""      # 同一连接会连续处理多个请求，进入时必须清空
        path = urlparse(self.path).path
        if path != "/api/health":
            # G2：首页/静态资源/接管页都算真实流量；健康检查自己不算，否则探针永远显示正常
            mark_activity()
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
                "uptime": round(time.time() - _PROCESS_START, 1),   # I-2：进程存活秒数，探活后可证明服务持续在线
                **keepalive_snapshot(),                              # G2：最近一次真实流量与探活是否失效
            })
            return
        # 方案 B：cookie 维度查询转人工状态，供刷新页面后恢复徽标（无需管理口令）
        if path == "/api/handoff":
            self._handle_handoff_state()
            return
        # I-4：人工接管轻量版 API（管理口令保护，见 _admin_allowed）
        if path == "/api/pending" or path.startswith("/api/session/"):
            ok, reason = self._admin_allowed()
            if not ok:
                self._reject(503 if not ADMIN_TOKEN else 403, reason)
                return
            if path == "/api/pending":
                self._handle_pending_list()
            else:
                sid = path[len("/api/session/"):]
                self._handle_pending_detail(sid)
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        self._req_start = time.time()
        self._pending_cookie = ""      # 同上
        mark_activity()                # G2：任何入站 POST 都算真实流量
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/reset":
            self._handle_reset()
            return
        # I-4：人工接管——标记已处理（从队列移除）
        if path == "/api/resolve":
            ok, reason = self._admin_allowed()
            if not ok:
                self._reject(503 if not ADMIN_TOKEN else 403, reason)
                return
            self._handle_pending_resolve()
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

        # 读掉请求体：本接口不看 body，但不读会让 keep-alive 连接上残留字节，
        # 污染下一个请求（表现为 501 Unsupported method）。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY_BYTES:
            self.rfile.read(length)

        token, _, _, _ = SESSIONS.resolve(self._read_cookie(SESSION_COOKIE))
        epoch = SESSIONS.reset(token)
        # 方案 B：清空对话 = 结束本次转人工，工单一并撤下，后续正常问答恢复调模型
        with PENDING_HANDOFF_LOCK:
            PENDING_HANDOFF.pop(token, None)
        self._pending_cookie = self._session_cookie(token)
        self._send_json(200, {"ok": True, "epoch": epoch})

    # ------------------------------------------------------------------
    # I-4 人工接管轻量版（只读队列 + 标记已处理）
    # ------------------------------------------------------------------
    def _handle_pending_list(self) -> None:
        """GET /api/pending —— 列出所有待接管会话（摘要）。"""
        with PENDING_HANDOFF_LOCK:
            items = []
            for sid, entry in PENDING_HANDOFF.items():
                if entry.get("resolved"):
                    continue
                # 历史里可能保留多轮，列表预览要取「最后一条用户消息」（即触发转人工的那句）
                last_user = ""
                for m in reversed(entry["messages"]):
                    if m["role"] == "user":
                        last_user = m["text"]
                        break
                items.append({
                    # 管理接口已鉴权，列表直接给完整 session_id，详情/标记按完整值精确匹配。
                    # 前端展示时再截前 12 位，避免界面上铺满长 token。
                    "session_id": sid,
                    "session_len": len(sid),            # 给人工确认是否为真 token 时参考
                    "ts": entry["ts"],
                    "time_str": datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S"),
                    "user_ip_hash": entry["user_ip_hash"],
                    "last_user": last_user[:40],
                    "last_reply_preview": entry["last_reply"][:60],
                    "msg_count": len(entry["messages"]),
                })
            items.sort(key=lambda x: x["ts"])   # 按入队时间正序
            unresolved = len(items)
            resolved = sum(1 for e in PENDING_HANDOFF.values() if e.get("resolved"))
        self._send_json(200, {
            "pending": unresolved,
            "resolved": resolved,
            "queue_max": PENDING_HANDOFF_MAX,
            "items": items,
        })

    def _handle_pending_detail(self, sid: str) -> None:
        """GET /api/session/{id} —— 查看某会话完整历史（只读）。

        只接受完整 token：不再做前缀匹配，否则空串会把整个队列的第一条
        直接匹配出来。
        """
        if len(sid) < 16:
            self._reject(400, "session_id 不合法（需要完整 token）")
            return
        with PENDING_HANDOFF_LOCK:
            key = sid if sid in PENDING_HANDOFF else None
            if key is None:
                self._send_json(404, {"error": "会话不存在或已被清理"})
                return
            entry = PENDING_HANDOFF[key]
            detail = {
                "session_id": key,
                "session_id_short": key[:12],
                "time_str": datetime.fromtimestamp(entry["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                "user_ip_hash": entry["user_ip_hash"],
                "resolved": entry.get("resolved", False),
                "context_rounds": HANDOFF_CONTEXT_ROUNDS,
                "messages": [
                    {
                        "role": m["role"],
                        "text": m["text"],
                        "time": datetime.fromtimestamp(m.get("ts", entry["ts"])).strftime("%H:%M:%S"),
                    }
                    for m in entry["messages"]
                ],
            }
        self._send_json(200, detail)

    def _handle_pending_resolve(self) -> None:
        """POST /api/resolve —— 标记某会话已处理（从队列移除/或 resolved=True）。

        请求体：{"session_id": "..."}
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reject(413, "请求体过大")
                return
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self._reject(400, "请求体必须是合法 JSON")
            return

        sid = data.get("session_id", "").strip()
        if not sid:
            self._reject(400, "缺少 session_id")
            return
        if len(sid) < 16:
            self._reject(400, "session_id 不合法（需要完整 token）")
            return

        with PENDING_HANDOFF_LOCK:
            # 只接受完整 token，不做前缀匹配（空串/短前缀会误伤别的会话）
            key = sid if sid in PENDING_HANDOFF else None
            if key is None:
                self._send_json(404, {"error": "会话不存在或已被清理"})
                return
            # 直接移除（轻量版不保留已处理历史；面试时说"后续加 resolved 标记保留可审计"）
            del PENDING_HANDOFF[key]
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # 对话代理
    # ------------------------------------------------------------------
    def _handle_handoff_message(self, token: str, message: str,
                                state: dict, epoch: int) -> None:
        """方案 B：已转人工的会话，消息只送达人工，不调用模型。

        - 追加进同一工单（不新建），接管面板能看到消息与时间；
        - 通过 handoff 事件（delivered=True）告知前端「已送达」；
        - 不产生 delta，前端不会出现机器人气泡。
        """
        now = time.time()
        SESSIONS.append_user_message(token, message, epoch)
        position = append_handoff_message(token, message, self._client_ip())
        self._chat_branch = ""          # 不产生新的 branch，保持评测口径不变
        self._chat_handoff = True
        self._chat_cache_hit = False
        self._chat_in_tok = 0           # 没有模型调用，成本为 0
        self._chat_out_tok = 0
        self._chat_reply = ""
        self._chat_ttft = round(now - self._req_start, 3)
        self._sse_start()
        self._sse_send("handoff", json.dumps({
            "ticket": state.get("ticket", token[:12]),
            "queue_position": position,
            "delivered": True,
            "message": "已发送给人工客服，请耐心等待～",
            "at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False))
        self._sse_send("done", "[DONE]")
        try:
            self._sse_end()
        except OSError:
            pass
        self._emit_chat_log(token, message)

    def _handle_handoff_state(self) -> None:
        """刷新页面后恢复会话：转人工状态 + 最近对话历史。

        只读 cookie，绝不创建会话、绝不入队：刷新本身不能产生新工单。
        若会话里还留着 handoff 但队列里已没有对应的活动工单（人工已处理完 /
        已被撤下），说明这条转人工已经结束——不能对外宣称「等待人工接入」，
        同时清掉会话状态，恢复正常问答，避免刷新出现没有来源的幽灵提示。
        """
        token = self._read_cookie(SESSION_COOKIE)
        messages = SESSIONS.history(token) if token else []
        state = SESSIONS.handoff(token) if token else None
        if not state:
            self._send_json(200, {"handoff": False, "messages": messages})
            return
        with PENDING_HANDOFF_LOCK:
            position = _handoff_position_locked(token)
        if position <= 0:
            SESSIONS.clear_handoff(token)
            self._send_json(200, {"handoff": False, "messages": messages})
            return
        self._pending_cookie = self._session_cookie(token)
        self._send_json(200, {
            "handoff": True,
            "ticket": state.get("ticket", ""),
            "queue_position": position,
            "messages": messages,
        })

    def _handle_chat(self) -> None:
        # ---- 第 1 层防护：来源校验（先拦，避免白读请求体）----
        if not self._origin_allowed():
            self._reject(403, "请求来源不被允许")
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

        # 方案 B：已转人工 → 对话主权交给人工，本条消息只落队列，绝不调用模型/缓存。
        handoff_state = SESSIONS.handoff(token)
        if handoff_state:
            self._handle_handoff_message(token, message, handoff_state, epoch)
            return

        # ---- 第 2、3 层防护：限流（转人工消息不消耗模型额度，故放在其后）----
        allowed, reason = LIMITER.check(self._client_ip())
        if not allowed:
            self._reject(429, reason)
            return
        if not COZE_API_TOKEN:
            self._reject(500, "服务端未配置 COZE_API_TOKEN，请先在 .env 中填写")
            return

        # ---- 确定性回答层（L1）：结构化事实直接查本地索引，命中即返回，不调用扣子 ----
        fact = lookup_fact(message)
        if fact is not None:
            reply = fact["reply"]
            entry = fact.get("entry") or {}
            self._chat_ttft = round(time.time() - self._req_start, 2)
            self._chat_reply = reply
            self._chat_in_tok = 0          # 未调用模型，无输入/输出费用
            self._chat_out_tok = 0
            self._chat_guard_blocked = False
            self._chat_handoff = False
            self._chat_cache_hit = False
            self._chat_branch = infer_branch(message, reply)
            log_guard_event(
                "fact_hit",
                file=entry.get("file", ""),
                section=entry.get("section", ""),
                score=fact.get("score"),
            )
            SESSIONS.record_turn(token, message, reply, epoch)
            self._sse_start()
            self._sse_send("delta", json.dumps({"content": reply}, ensure_ascii=False))
            self._sse_send("branch", json.dumps({"branch": self._chat_branch}, ensure_ascii=False))
            self._sse_send("done", "[DONE]")
            try:
                self._sse_end()
            except OSError:
                pass
            self._emit_chat_log(token, message)
            return

        # 首问判定用会话轮次：reset 会把 turn_count 清零，点过「清空对话」后仍能重新命中缓存
        first_turn = SESSIONS.turn_count(token) == 0

        # M-2：先从本条消息抽槽位、合并进会话，再把完整槽位以一条助手快照
        # 随请求发出（auto_save_history 会存进会话历史，供回复节点读取）。
        # 扣子要求 additional_messages 最后一条必须是 user，所以快照放前面。
        slots = SESSIONS.merge_slots(token, extract_slots(message))
        slots_snapshot = "[SLOTS_JSON]" + json.dumps(slots, ensure_ascii=False, separators=(",", ":"))

        # P2-4 成本指标：估算本轮输入 token（用户消息 + 槽位快照 + 系统提示开销）
        self._chat_in_tok = estimate_tokens(message + slots_snapshot) + PROMPT_OVERHEAD_TOK
        self._chat_cache_hit = False
        self._chat_handoff = False
        self._chat_reply = ""
        self._chat_out_tok = 0
        self._chat_guard_blocked = False
        self._chat_guard_metric = ""
        self._chat_guard_value = None
        resp = None  # 提前初始化，保证 finally 里 resp.close() 在缓存命中路径不报错

        # P2-4 FAQ 缓存：只缓存会话首问（first_turn），相同问题直接回放，不调用模型。
        # 归一化 key：去空格转小写，避免「四件套尺寸」和「四件套 尺寸」存两份。
        cache_key = re.sub(r"\s+", "", message).lower()
        if first_turn:
            cached = faq_cache_get(cache_key)
            if cached is not None:
                cached_reply, cached_branch = cached
                # 缓存内容写入前已校验过；这里再校验一次，清单若更新可及时淘汰旧值。
                cached_reply, violation = guard_completed_reply(cached_reply, message)
                if violation is not None:
                    pad = violation.as_log()
                    log_guard_event("numeric", path="cache", **pad)
                    with FAQ_CACHE_LOCK:
                        FAQ_CACHE.pop(cache_key, None)
                else:
                    self._chat_ttft = round(time.time() - self._req_start, 2)
                    self._chat_branch = cached_branch
                    self._chat_reply = cached_reply
                    self._chat_out_tok = estimate_tokens(cached_reply)
                    self._chat_cache_hit = True
                    SESSIONS.record_turn(token, message, cached_reply, epoch)
                    self._sse_start()
                    self._sse_send("delta", json.dumps({"content": cached_reply}, ensure_ascii=False))
                    if cached_branch:
                        self._sse_send("branch", json.dumps({"branch": cached_branch}, ensure_ascii=False))
                    self._sse_send("done", "[DONE]")
                    # 缓存命中分支在下面的 try/finally 之外，必须自己收尾：终结 chunked 流 + 写日志
                    try:
                        self._sse_end()
                    except OSError:
                        pass
                    self._emit_chat_log(token, message)
                    return  # 不调用扣子

        body = {
            "bot_id": COZE_BOT_ID,
            "user_id": user_id,
            "stream": True,              # 流式返回，前端可以逐字显示
            "auto_save_history": True,   # 保存会话，多轮才记得住上下文
            # 工作流开始节点的 SLOTS_JSON 输入（选填）：留空占位，保持兼容
            "parameters": {"SLOTS_JSON": ""},
            "additional_messages": [
                {"role": "assistant", "content": slots_snapshot, "content_type": "text"},
                {"role": "user", "content": message, "content_type": "text"},
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
            self._pump_stream(resp, token, epoch, message)
            # P2-4 FAQ 缓存：首问且成功拿到非空回复、且未转人工时写入缓存，下次同问题直接回放。
            if first_turn and self._chat_reply and not self._chat_handoff:
                faq_cache_put(cache_key, self._chat_reply, self._chat_branch)
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
            if resp is not None:
                resp.close()
            try:
                self._sse_end()
            except OSError:
                pass
            self._emit_chat_log(token, message)

    def _pump_stream(self, resp, token: str, epoch: int | None = None, message: str = "") -> None:
        """把扣子的 SSE 事件翻译成前端更简单的事件：meta / delta / branch / handoff / error / done。"""
        # 业务日志字段：首字延迟、命中分支，在流处理中采集
        self._chat_ttft: float | None = None
        self._chat_branch: str = ""
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
        reply_text = ""
        stream_pending = ""
        guard_stream = NumericGuardStream(message)
        self._chat_guard_blocked = False
        self._chat_guard_metric = ""
        self._chat_guard_value = None

        def emit(text: str) -> None:
            nonlocal reply_text, sent_delta
            if not text:
                return
            if self._chat_ttft is None:
                self._chat_ttft = round(time.time() - self._req_start, 2)
            self._sse_send("delta", json.dumps({"content": text}, ensure_ascii=False))
            reply_text += text
            sent_delta = True

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
                    # 方案 B：标记要等攒齐再判断，先扣住可能是 [[NO_ANSWER]] 前缀的尾部
                    stream_pending += piece
                    visible, stream_pending = split_no_answer_stream(stream_pending)
                    if visible and not sent_delta and not visible.strip():
                        # 还没发过正文时，纯空白也先留着，避免把「只含标记」误判成已有实质内容
                        stream_pending = visible + stream_pending
                        visible = ""
                    if visible:
                        for chunk in guard_stream.feed(visible):
                            emit(chunk)
            else:
                # 完成事件里带的是整段回复，作为兜底
                completed_answer = msg.get("content") or completed_answer

        # 收尾：暂缓的尾部按方案 B 规则处理（只含标记 → 兜底话术；夹在实质答复里 → 删标记）
        if stream_pending:
            marker = stream_pending.strip()
            if not sent_delta and marker in NO_ANSWER_REPLIES:
                # L3 兜底：普通标记且有知识库原文时，优先回原文；发票标记不覆盖。
                fallback = "" if marker == NO_ANSWER_INVOICE_MARKER else fact_fallback_reply(message)
                if fallback:
                    log_guard_event("fact_fallback", path="marker")
                tail = fallback or NO_ANSWER_REPLIES[marker]
            elif not sent_delta and GENERIC_PLACEHOLDER_RE.fullmatch(marker):
                # 未登记的占位符单独出现：不能把空回复丢给用户，按兜底处理
                log_guard_event("placeholder", markers=[marker], action="fallback")
                tail = NO_ANSWER_REPLY
            elif re.fullmatch(r"\[\[[A-Z0-9_]*", marker):
                # 流结束时仍是半截标记，直接丢弃，避免 "[[ABC" 漏给用户
                log_guard_event("placeholder", markers=[marker], action="dropped_partial")
                tail = ""
            else:
                tail = stream_pending
                for m in NO_ANSWER_MARKERS:
                    tail = tail.replace(m, "")
                tail = GENERIC_PLACEHOLDER_RE.sub("", tail)
            for chunk in guard_stream.feed(tail):
                emit(chunk)
            stream_pending = ""

        # 没有标点收尾的残留句段也要过一遍守卫，避免漏字或漏拦
        for chunk in guard_stream.finish():
            emit(chunk)
        if guard_stream.blocked:
            self._chat_guard_blocked = True
            violation = guard_stream.violation
            if violation is not None:
                log = violation.as_log()
                self._chat_guard_metric = log["metric"]
                self._chat_guard_value = log["value"]
                log_guard_event("numeric", path="stream", **log)

        if structured:
            reply, branch = self._split_structured(completed_answer or "".join(pieces))
            if not reply:
                # 解析失败不能把 JSON 原样丢给用户，给一句明确的提示
                self._sse_send("error", json.dumps({"message": "回复格式异常，请再发送一次～"}, ensure_ascii=False))
                return
            reply_text = replace_no_answer_marker(reply)
            if reply.strip() == NO_ANSWER_MARKER:
                fallback = fact_fallback_reply(message)
                if fallback:
                    log_guard_event("fact_fallback", path="structured")
                    reply_text = fallback
            reply_text, violation = guard_completed_reply(reply_text, message)
            if violation is not None:
                self._chat_guard_blocked = True
                log = violation.as_log()
                self._chat_guard_metric = log["metric"]
                self._chat_guard_value = log["value"]
                log_guard_event("numeric", path="structured", **log)
            if reply_text:
                if self._chat_ttft is None:
                    self._chat_ttft = round(time.time() - self._req_start, 2)
                self._sse_send("delta", json.dumps({"content": reply_text}, ensure_ascii=False))
                sent_delta = True

        # 极端情况：没有增量（例如只给了完成事件），一次性补发
        if not sent_delta and completed_answer:
            reply_text = replace_no_answer_marker(completed_answer)
            if completed_answer.strip() == NO_ANSWER_MARKER:
                fallback = fact_fallback_reply(message)
                if fallback:
                    log_guard_event("fact_fallback", path="completed")
                    reply_text = fallback
            reply_text, violation = guard_completed_reply(reply_text, message)
            if violation is not None:
                self._chat_guard_blocked = True
                log = violation.as_log()
                self._chat_guard_metric = log["metric"]
                self._chat_guard_value = log["value"]
                log_guard_event("numeric", path="completed", **log)
            if reply_text:
                if self._chat_ttft is None:
                    self._chat_ttft = round(time.time() - self._req_start, 2)
                self._sse_send("delta", json.dumps({"content": reply_text}, ensure_ascii=False))
                sent_delta = True
        if conversation_id and not sent_meta:
            self._sse_send("meta", json.dumps({"conversation_id": conversation_id}, ensure_ascii=False))

        # 上游流异常结束：既没有增量也没有完成事件。直接发 done 会让前端留下空白气泡，
        # 这里改为下发 error，让用户看到明确提示而不是一段沉默。
        # 注意：必须在发 branch 之前拦截，否则失败的请求也会带一个分类标签，污染路由统计。
        if not sent_delta:
            # L3 兜底：上游空回复时，知识库有原文就用原文，不再让用户看到空气泡或超时。
            fallback = fact_fallback_reply(message)
            if fallback:
                log_guard_event("fact_fallback", path="empty")
                emit(fallback)
            else:
                self._sse_send("error", json.dumps({"message": "响应超时了，请再发送一次～"}, ensure_ascii=False))
                return

        if not branch:
            # v2.0：结束节点只返回 reply 纯文本，不再有代码节点判定的 branch，按关键词推断
            branch = infer_branch(message, reply_text or completed_answer)
        # 分类标签：前端忽略，评测脚本据此统计路由
        self._sse_send("branch", json.dumps({"branch": branch}, ensure_ascii=False))
        self._chat_branch = branch
        # 转人工判定：分支标签里没有「转人工」（转接话术按内容归「其他兜底」，与评测期望一致），
        # 是否转人工单独按回复里的转接话术判定，供日志统计与 FAQ 缓存排除
        self._chat_handoff = any(marker in reply_text for marker in HANDOFF_MARKERS)

        # 先把本轮的问答写进会话（轮次 + 最近若干轮历史），转人工时连同上下文一起入队
        SESSIONS.record_turn(token, message, reply_text, epoch)

        # I-4：转人工时自动入队待接管（用 token 作唯一 key，幂等）。
        # 入队成功后补一个 handoff 事件：前端据此展示「已转接人工」状态与工单号，
        # 不必再从回复话术里猜。branch 仍保持「其他兜底」，不影响现有评测口径。
        if self._chat_handoff:
            position = record_handoff(
                session_id=token,
                user_ip=self._client_ip(),
                last_reply=reply_text,
                messages=SESSIONS.history(token),
            )
            # 方案 B：状态写进服务端会话，刷新页面后仍能恢复「等待人工接入」，
            # 后续消息不再调用模型，全部转给人工。
            SESSIONS.enter_handoff(token, token[:12])
            self._sse_send("handoff", json.dumps({
                "ticket": token[:12],
                "queue_position": position,
                "message": "已为您转接人工客服，正在等待人工接入～",
            }, ensure_ascii=False))

        # P2-4：记录完整回复与输出 token，供成本日志与 FAQ 缓存使用
        self._chat_reply = reply_text
        self._chat_out_tok = estimate_tokens(reply_text)

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

    def log_message(self, fmt: str, *args) -> None:
        """访问层结构化日志：一行 JSON，IP 与 UA 不存明文。

        字段：t(时间) / level / method / path / status / ip_hash / ua / elapsed。
        业务字段（分支、首字、转人工）由 _emit_chat_log 单独输出。
        """
        elapsed = round(time.time() - getattr(self, "_req_start", time.time()), 3)
        ip = self.client_address[0] if getattr(self, "client_address", None) else ""
        ua = (self.headers.get("User-Agent", "") if self.headers else "")[:120]
        # status 兼容 BaseHTTPRequestHandler 的 "%s" %s %s 与 "%s" 两种调用
        status = args[1] if len(args) > 1 else (args[0] if args else "")
        # 管理口令支持走 ?token= 方便 curl 调试，日志里必须打码，不能落进 Render 日志
        safe_path = re.sub(r"([?&]token=)[^&]*", r"\1***", self.path)
        rec = {
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "level": "access",
            "method": self.command,
            "path": safe_path,
            "status": str(status),
            "ip_hash": _hash_for_log(ip),
            "ua": ua,
            "elapsed": elapsed,
        }
        print(json.dumps(rec, ensure_ascii=False))

    def _emit_chat_log(self, token: str, message: str) -> None:
        """业务层结构化日志：一行 JSON，可算转人工率、平均轮次、P95 延迟、token 成本。"""
        elapsed = round(time.time() - getattr(self, "_req_start", time.time()), 3)
        branch = getattr(self, "_chat_branch", "") or ""
        handoff = bool(getattr(self, "_chat_handoff", False))
        cache_hit = bool(getattr(self, "_chat_cache_hit", False))
        in_tok = int(getattr(self, "_chat_in_tok", 0))
        out_tok = int(getattr(self, "_chat_out_tok", 0))
        # 缓存命中不产生模型费用；否则按输入/输出 token 估算成本（元）
        if cache_hit:
            cost_yuan = 0.0
        else:
            cost_yuan = (in_tok * COZE_IN_PRICE + out_tok * COZE_OUT_PRICE) / 1000
        rec = {
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "level": "chat",
            "session": _hash_for_log(token),
            "branch": branch,
            "handoff": handoff,
            "ttft": getattr(self, "_chat_ttft", None),
            "elapsed": elapsed,
            "msg_len": len(message),
            "cache_hit": cache_hit,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "cost": round(cost_yuan, 6),
            "guard_blocked": bool(getattr(self, "_chat_guard_blocked", False)),
            "guard_metric": getattr(self, "_chat_guard_metric", "") or "",
            "guard_value": getattr(self, "_chat_guard_value", None),
        }
        print(json.dumps(rec, ensure_ascii=False))


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
