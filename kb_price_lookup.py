# -*- coding: utf-8 -*-
"""价格前置查数：从「14-价格速查」解析出「款 + 规格 → 准确价」，只做查表。

设计原则
--------
1. 数字只来自 14-价格速查 的答复原文，不推算、不取中间价、不跨款顶替。
2. 只处理「点明了款、点明了规格、且是价格问法」的三要素齐全请求；
   款不在表里、规格缺失、该档标「无」、说法有歧义，一律不注入，原样转发给模型。
3. 注入内容是一条带标记的助手快照（[PRICE_REF]{...}），措辞由模型组织；
   快照不含「参考价」等用户话术，回显时由 server.py 统一剥除。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_PRICE_FILE = (
    Path(__file__).resolve().parent / "docs" / "knowledge-base" / "upload" / "14-价格速查.md"
)

# 价格问法：只认明确问价的说法，避免把「贵不贵」这类主观评价也拉进来。
PRICE_ASK_RE = re.compile(r"多少钱|价格|报价|售价|标价|卖多少|价位|几块钱|[¥￥]")
# 对比、组合、活动、议价类问题不注入单一价格：只给一个数会误导。
PRICE_SKIP_RE = re.compile(
    r"对比|区别|哪个|哪款|选哪|推荐|合计|一共|总共|组合|满减|到手价|折算|平均|"
    r"优惠|活动|折扣|券|便宜|议价|砍价|团购|批发|起订|"
    r"包邮|运费|邮费|快递|物流|发货|退货|换货|退款"
)

# 解析答复里的「规格 + 价格」与「无 某规格」。
_SPEC = (
    r"(?:\d{1,3}(?:\.\d+)?\s*(?:m|米)"
    r"|(?:\d{2,3})\s*[×xX*]\s*\d{2,3}\s*(?:cm|厘米)?)"
)
_PRICE_ITEM_RE = re.compile(rf"({_SPEC})\s*[¥￥]\s*(\d+(?:\.\d+)?)")
_ABSENT_RE = re.compile(rf"无\s*({_SPEC})")

# 这些标题不是「一款一价」的商品条目：索引、口径说明、横切汇总、组合价、配件价。
_SKIP_TITLE_WORDS = (
    "索引", "说明", "读表", "总览", "组合", "刺绣", "定制", "三款", "枕套单买",
)

_GENERIC_ALIASES = {
    "四件套", "三件套", "六件套", "被芯", "枕芯", "保护套", "床垫保护套",
    "被子", "枕头", "床单", "被套", "枕套", "套件", "床垫罩", "价格", "多少钱",
    # 面料词单独出现时跨多款（「天丝」同时是清风四件套与天丝夏凉被），不能当款名。
    "天丝", "纯棉", "磨毛", "有机棉", "长绒棉", "法兰绒", "记忆棉", "乳胶", "软管", "鹅绒", "大豆",
}

# 疑问/语气词，做别名时统一刮掉。
_ALIAS_NOISE_RE = re.compile(r"多少钱|价格|报价|售价|标价|有货吗|有吗|一个|呢|吗|的|？|\?")
_ALIAS_SPEC_RE = re.compile(_SPEC)

# 品类识别：问句里只出现品类词、没点名款时，按「品类 + 规格」横切报价。
# 顺序敏感：「床垫保护套」要先于「保护套」吃掉，避免歧义。
_CATEGORY_RULES = (
    ("床垫保护套", ("床垫保护套", "保护套")),
    ("被芯", ("被芯", "被子")),
    ("枕芯", ("枕芯", "枕头")),
    ("四件套", ("四件套",)),
    ("六件套", ("六件套",)),
    ("三件套", ("三件套",)),
)


def _product_category(name: str) -> str:
    """由款名判定品类（一款只属于一个品类）。"""
    if "保护套" in name:
        return "床垫保护套"
    for category in ("四件套", "六件套", "三件套"):
        if category in name:
            return category
    if "枕" in name:
        return "枕芯"
    if "被" in name:
        return "被芯"
    return ""


def _category_hits(text: str) -> list[str]:
    """文本里点名的品类（去重）；「四件套」「六件套」互不包含，不会串。"""
    msg = _norm(text)
    out: list[str] = []
    for category, words in _CATEGORY_RULES:
        if category in out:
            continue
        if any(_norm(word) in msg for word in words):
            out.append(category)
    return out


# 品类词前面挂着一个表里没有的款名（如「云舒四件套」）时，不能按品类横切：
# 用户点名了具体款，查不到就该原样转发，而不是拿整个品类顶替。
_UNKNOWN_BEFORE_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,3})(?:四件套|六件套|三件套|被芯|被子|枕芯|枕头|床垫保护套|保护套)"
)
# 前缀里出现这些字，多半是「想买/你们/这款」这类口语，不是款名。
_NON_BRAND_CHARS = set("我你们要相买看问请这那哪所全每各的有是在和与或套件款个")
_CATEGORY_WORDS = {word for _, words in _CATEGORY_RULES for word in words}


def _names_unknown_product(text: str) -> bool:
    for prefix in _UNKNOWN_BEFORE_RE.findall(_norm(text)):
        if prefix in _GENERIC_ALIASES:
            continue
        if any(prefix in word for word in _CATEGORY_WORDS):   # 「床垫」+「保护套」
            continue
        if any(ch in _NON_BRAND_CHARS for ch in prefix):
            continue
        return True
    return False


def _norm(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return re.sub(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]·\-—_/]+", "", "".join(out)).lower()


def _norm_spec(raw: str) -> str:
    text = raw.replace(" ", "").replace("厘米", "cm")
    if any(sep in text for sep in ("×", "x", "X", "*")):
        first, second = re.split(r"[×xX*]", text)
        first = re.sub(r"(cm)$", "", first)
        second = re.sub(r"(cm)$", "", second)
        return f"{first}x{second}cm"
    value = re.sub(r"(m|米)$", "", text)
    try:
        return f"{float(value):g}m"
    except ValueError:
        return text


def _spec_display(spec: str) -> str:
    return spec.replace("x", "×")


def _query_specs(message: str) -> set[str]:
    """从用户问句里提取规格；返回规范化后的集合，空集/多个都表示不注入。"""
    msg = message.replace(" ", "")
    specs: set[str] = set()
    for first, second in re.findall(r"(\d{2,3})(?:cm|厘米)?[×xX*](\d{2,3})(?:cm|厘米)?", msg):
        specs.add(f"{first}x{second}cm")
    # 口语「1米8」「1米5」
    for whole, decimal in re.findall(r"(\d)米(\d)(?!\d)", msg):
        specs.add(f"{whole}.{decimal}m")
    for value in re.findall(r"(\d+(?:\.\d+)?)\s*(?:m|米)(?![m米\d])", msg):
        try:
            specs.add(f"{float(value):g}m")
        except ValueError:
            continue
    return specs


def _aliases(heading: str, questions: list[str]) -> list[str]:
    candidates = [heading, heading.replace("·", ""), heading.replace(" ", "")]
    if "·" in heading:
        candidates.append(heading.split("·", 1)[0])
    for question in questions:
        stripped = _ALIAS_SPEC_RE.sub(" ", question)
        stripped = _ALIAS_NOISE_RE.sub("", stripped).strip(" ·-—_")
        stripped = re.sub(r"有$", "", stripped).strip(" ·-—_")
        if not stripped or any(word in stripped for word in ("各", "几款", "多少", "三款", "五款", "九款")):
            continue
        candidates.append(stripped)
    out: list[str] = []
    for item in candidates:
        item = item.strip(" ·-—_")
        norm = _norm(item)
        if len(norm) < 2 or norm in _GENERIC_ALIASES or item in out:
            continue
        out.append(item)
    return out


def _iter_sections(path: Path):
    """逐条产出 (标题, 常见问法原文, 答复原文)。答复允许缩进续行。"""
    heading = ""
    questions = ""
    answer_lines: list[str] = []
    in_answer = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("### "):
            if heading:
                yield heading, questions, " ".join(answer_lines).strip()
            heading = line[4:].strip()
            questions = ""
            answer_lines = []
            in_answer = False
            continue
        if line.startswith("## "):
            if heading:
                yield heading, questions, " ".join(answer_lines).strip()
            heading = ""
            questions = ""
            answer_lines = []
            in_answer = False
            continue
        if not heading:
            continue
        if line.startswith("- 常见问法："):
            questions = line[len("- 常见问法："):].strip()
            in_answer = False
        elif line.startswith("- 答复："):
            answer_lines = [line[len("- 答复："):].strip()]
            in_answer = True
        elif in_answer and line.startswith("-"):
            # 下一个字段（适用条件/数值边界等）出现，答复结束
            in_answer = False
        elif in_answer and line:
            answer_lines.append(line)

    if heading:
        yield heading, questions, " ".join(answer_lines).strip()


def load_price_table(path: Path | str = DEFAULT_PRICE_FILE) -> dict:
    """解析 14-价格速查，返回 {"products": [...]}；文件缺失或为空时抛 OSError/ValueError。"""
    file_path = Path(path)
    products: list[dict] = []
    for heading, question_text, answer in _iter_sections(file_path):
        if not answer or not re.search(r"[¥￥]\s*\d", answer):
            continue
        if any(word in heading for word in _SKIP_TITLE_WORDS):
            continue
        specs: dict[str, float] = {}
        for raw_spec, raw_price in _PRICE_ITEM_RE.findall(answer):
            spec = _norm_spec(raw_spec)
            try:
                price = float(raw_price)
            except ValueError:
                continue
            specs.setdefault(spec, int(price) if price.is_integer() else price)
        if not specs:
            continue
        absent = {_norm_spec(raw) for raw in _ABSENT_RE.findall(answer)}
        questions = [q.strip() for q in re.split(r"[？?；;]", question_text) if q.strip()]
        products.append({
            "name": heading,
            "category": _product_category(heading),
            "aliases": _aliases(heading, questions),
            "specs": specs,
            "absent": sorted(absent),
        })
    if not products:
        raise ValueError("价格速查里没有解析到任何单品价")
    return {"products": products}


def _product_hits(text: str, products: list[dict]) -> list[dict]:
    """返回文本里点名到的款（按款去重）；同一句里出现多款时由调用方判定为歧义。"""
    msg = _norm(text)
    hits: list[dict] = []
    for product in products:
        if any(
            len(_norm(alias)) >= 2 and _norm(alias) in msg
            for alias in product.get("aliases") or []
        ):
            hits.append(product)
    return hits


def _product_from_context(context, products: list[dict]) -> dict | None:
    """从最近的用户原话里补主语：由近到远取第一条能唯一点名商品的。

    最近一条一旦同时点名多款，就视为指代不明，直接放弃——不能跳过它去用更早的款。
    """
    for text in reversed(list(context or ())):
        hits = _product_hits(text, products)
        if len(hits) > 1:
            return None
        if len(hits) == 1:
            return hits[0]
    return None


def _category_from_context(context, products: list[dict]) -> str | None:
    """从最近的用户原话里补品类；最近一条同时点名多款或多个品类就放弃。"""
    for text in reversed(list(context or ())):
        if len(_product_hits(text, products)) > 1:
            return None
        if _names_unknown_product(text):
            return None
        categories = _category_hits(text)
        if len(categories) > 1:
            return None
        if categories:
            return categories[0]
    return None


def _category_prices(message: str, context, products: list[dict], spec: str) -> dict | None:
    """品类 + 规格都明确、款名未定时，横切该品类下所有有该规格的款，价格升序。"""
    categories = _category_hits(message) or []
    if len(categories) > 1:
        return None
    if categories and _names_unknown_product(message):
        return None
    category = categories[0] if categories else _category_from_context(context, products)
    if not category:
        return None
    rows: list[list] = []
    for product in products:
        if product.get("category") != category:
            continue
        if spec in (product.get("absent") or []):
            continue
        price = (product.get("specs") or {}).get(spec)
        if price is None:
            continue
        rows.append([product["name"], price])
    if not rows:
        return None
    rows.sort(key=lambda row: (row[1], row[0]))
    return {"category": category, "spec": spec, "prices": rows}


def match_price(message: str, table: dict, context=()) -> dict | None:
    """命中返回价格事实；任何不确定都返回 None（不注入）。

    · 规格明确：返回 {"product","spec","price"}
    · 没写规格但款名唯一：返回该款全部有价规格 {"product","specs"}
    · 款名省略（如「1.8 米多少钱」）：从 context（最近用户原话，旧→新）补主语
    · 品类与规格明确、款名未定：返回该品类该规格各款价 {"category","spec","prices"}
    """
    if not message or not table:
        return None
    if not PRICE_ASK_RE.search(message) or PRICE_SKIP_RE.search(message):
        return None
    specs = _query_specs(message)
    if len(specs) > 1:      # 一句里问多个规格（含对比），不猜
        return None
    products = table.get("products") or []
    hits = _product_hits(message, products)
    if len(hits) > 1:
        return None
    product = hits[0] if hits else _product_from_context(context, products)
    if product is None:
        # 没点名款：只有规格也明确时，才按品类横切；没规格不猜
        if not specs:
            return None
        return _category_prices(message, context, products, next(iter(specs)))
    prices = dict(product.get("specs") or {})
    if not prices:
        return None
    if not specs:
        # 无规格：把该款表内全部有价规格交给模型逐档说明；表外的档不会出现
        return {"product": product["name"], "spec": "", "specs": prices}
    spec = next(iter(specs))
    if spec in (product.get("absent") or []):
        return None
    price = prices.get(spec)
    if price is None:
        return None
    return {"product": product["name"], "spec": spec, "price": price}


def build_price_snapshot(hit: dict) -> str:
    """把查到的价格拼成一条单行助手快照；不含面向用户的话术。"""
    if hit.get("prices"):
        payload = {
            "category": hit["category"],
            "spec": _spec_display(hit["spec"]),
            "prices": [{"product": name, "price": price} for name, price in hit["prices"]],
            "scope": "只给该规格各款的价：按给定顺序逐款列出，不要罗列其他规格、不要推荐某款、不要漏款；仅用于回答紧接着的一条提问",
        }
    elif hit.get("specs"):
        payload = {
            "product": hit["product"],
            "prices": {_spec_display(k): v for k, v in hit["specs"].items()},
            "scope": "该款全部有价规格；可逐档说明或给区间，不得补表外规格、不得罗列其他款；仅用于回答紧接着的一条提问",
        }
    else:
        payload = {
            "product": hit["product"],
            "spec": _spec_display(hit["spec"]),
            "price": hit["price"],
            "scope": "仅用于回答紧接着的一条提问；只使用本价格数字，不罗列其他款或规格；与本问无关时忽略本条",
        }
    return "[PRICE_REF]" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_default_table() -> dict | None:
    try:
        return load_price_table()
    except (OSError, ValueError):
        return None
