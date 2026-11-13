# -*- coding: utf-8 -*-
"""知识库事实查表：把价格、规格、克重、水温、时限等结构化事实从模型手里拿走。

设计原则
--------
1. 索引只从 `docs/knowledge-base/upload/*.md` 生成，不手写；答案逐字引用原文。
2. 命中后直接返回原文，不切片、不计算、不取中间价。
3. 判定从严：商品有歧义、条目平分、问题里的规格原文没有，一律视为未命中，
   交回扣子，宁可漏答也不答错。
4. 只做事实查询；推荐、对比、组合计算、库存颜色、投诉赔付不拦。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent / "data" / "kb_fact_index.json"

# 意图词表。刻意不收「多久」「几天」这类离开上下文就有歧义的词。
INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "price": ("多少钱", "价格", "报价", "售价", "价位", "几块钱", "贵不贵", "便宜吗", "¥", "￥"),
    "size": ("尺寸", "多大", "几米", "1.2米", "1.5米", "1.8米", "1.2m", "1.5m", "1.8m",
             "被套", "床单", "枕套", "规格"),
    "weight": ("克重", "g/㎡", "多厚", "厚不厚", "厚度"),
    "thread": ("支数", "几支", "多少支", "密度", "根/英寸"),
    "shrink": ("缩水",),
    "wash": ("水温", "多少度", "机洗", "手洗", "水洗", "能洗吗", "能不能洗", "可以洗吗",
             "能水洗", "可水洗", "怎么洗", "洗涤", "清洗",
             "多久洗", "多久换", "洗几次", "清洗频率"),
    "iron": ("熨烫", "能熨"),
    "dry": ("烘干",),
    "ship": ("发货", "多久发", "几天发", "时效", "现货"),
    "leadtime": ("预售", "定制", "做多久", "定做", "调货", "备货"),
    "bulk": ("团购", "批发", "采购", "批量", "起订"),
    "return": ("退换", "退货", "换货", "无理由", "保修", "签收"),
    "promo": ("满减", "优惠", "券", "会员", "折扣", "活动价", "几折", "打折"),
    "markup": ("加价", "定制费", "刺绣", "定制"),
    "contact": ("联系", "电话", "微信", "客服电话"),
    "store": ("门店", "实体店", "加盟", "线下店", "线下门店"),
    "cod": ("货到付款", "到付"),
    "freeship": ("包邮", "运费"),
    "invoice": ("发票", "开票", "抬头", "税号"),
    "origin": ("产地", "哪里生产", "哪里发", "工厂", "发货地", "仓库"),
    "cert": ("认证", "检测报告", "oeko", "iso"),
    "brand": ("品牌", "你们卖什么", "经营范围", "做什么的", "主营"),
    "material": ("面料", "材质", "纯棉", "长绒棉", "天丝", "磨毛", "法兰绒", "有机棉", "羊绒"),
    "feature": ("防水", "防螨", "透气", "掉色", "静电"),
}

# 价格离开具体商品无法回答：没有定位到商品时交回模型反问，避免报成别的品类价。
_NEEDS_PRODUCT = {"price"}

# 对比、推荐、组合计算不拦，交回模型。
_NON_FACT_RE = re.compile(
    r"哪个(?!品牌)|对比|区别|怎么选|选哪|推荐|适合谁|还是|两套|三套|"
    r"一共|总共|合计|加起来|组合|满减后|到手价|折算|平均|"
    r"掉毛吗|起球吗|质量|好不好|怎么样|那款|这款|哪款"
)

# 对比、选型、搭配类段落只做推荐，不进入确定性回答。
_NON_FACT_SECTIONS = ("对比", "选款", "选型", "推荐", "搭配", "区别", "哪个", "送礼", "婚庆", "场景")

# 同一意图的默认段落：用户没点名细分场景时优先回通用口径。
_DEFAULT_SECTIONS: dict[str, str] = {
    "ship": "现货发货时效",
    "return": "适用条件与除外商品",
    "bulk": "团购起订量与折扣",
    "freeship": "运费与包邮规则",
    "size": "四件套尺寸对照",
}

# 消息里出现限定词时，改成优先对应段落。
_PREFERRED_SECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "promo": (("会员", "会员等级与折扣"), ("满减", "满减档位与新人券"), ("券", "优惠券领取与使用")),
    "markup": (("刺绣", "定制 LOGO 与颜色尺寸"), ("logo", "定制 LOGO 与颜色尺寸")),
    "leadtime": (("定制", "预售、定制与调货发货时效"), ("预售", "预售、定制与调货发货时效")),
    "size": (("被芯", "被芯尺寸与床宽匹配"), ("被套", "四件套尺寸对照"), ("床单", "四件套尺寸对照")),
    "feature": (("防水", "三款面料与季节差异"), ("透气", "三款面料与季节差异")),
}

# 品类词 → 所属品类代码。顺序即优先级：先具体后笼统，
# 「床垫保护套」必须先于「保护套」，「被套」必须先于「被」。
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("05", ("床垫保护套", "床垫罩", "床笠", "保护套")),
    ("02", ("四件套", "三件套", "六件套", "套件", "被套", "床单", "床裙", "枕套")),
    ("03", ("被芯", "被子", "羽绒被", "蚕丝被", "夏凉被", "子母被", "绒被", "纤维被")),
    ("04", ("枕芯", "枕头", "乳胶枕", "记忆棉枕", "软管枕", "鹅绒枕")),
)

# 跨品类文件（01/06/14）里靠段落标题判断归属；判断不出来就视为通用条目。
_ENTRY_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("05", ("床垫保护套", "床笠", "保护套")),
    ("04", ("枕芯", "枕头", "枕高", "睡姿")),
    ("03", ("被芯", "被子", "羽绒被", "蚕丝被", "夏凉被", "子母被")),
    ("02", ("四件套", "套件", "床单", "被套", "床裙")),
)

# 段落标题本身横跨多个品类时，按标题而不是关键词归属，避免被误判成单品类。
_ENTRY_CATEGORY_OVERRIDES: dict[str, tuple[str, ...]] = {
    "枕芯尺寸与枕套匹配": ("04",),
    "枕芯与床品的更换周期": ("04", "02"),
    "被套与被芯的尺寸匹配": ("02", "03"),
}

# 这些事实离开品类无法确定归属，没品类词也没商品名时不猜，交回模型。
# 有品类词时，跨品类通用段落（如 06 不分品类的洗涤参数）也不许顶上来。
_CATEGORY_BOUND_INTENTS = {
    "price", "size", "material", "wash", "dry", "iron", "shrink",
    "weight", "thread", "feature",
}

# 条目段落关键词：同一意图下优先命中主题更贴的段落。
_SECTION_HINTS: dict[str, tuple[str, ...]] = {
    "ship": ("发货时效", "到货时间", "物流"),
    "return": ("适用条件", "起算时间", "退换", "退货", "换货"),
    "freeship": ("包邮", "运费"),
    "promo": ("满减", "优惠", "会员", "券"),
    "store": ("品牌与经营范围",),
    "invoice": ("发票", "开票"),
    "cert": ("认证",),
    "origin": ("产地",),
    "brand": ("品牌",),
    "material": ("面料",),
    "wash": ("洗", "清洁"),
    "weight": ("参数", "克重"),
    "thread": ("参数", "支数"),
    "bulk": ("团购", "起订"),
    "contact": ("联系",),
    "feature": ("防水", "透气", "防螨", "面料"),
}
# 没点名时，通用问题优先命中非细分段落。
_QUALIFIERS = ("预售", "定制", "调货", "大促", "活动", "批量", "团购")

# 商品名过于泛化，不能单独当作线索。
_GENERIC_ALIASES = {
    "四件套", "三件套", "六件套", "被芯", "枕芯", "保护套", "床垫保护套",
    "被子", "枕头", "床单", "被套", "枕套", "套件", "床垫罩",
}

_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]·\-—_/]+")


def normalize(text: str) -> str:
    """统一全半角、去空白与标点，便于包含匹配。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            ch = chr(code - 0xFEE0)
        out.append(ch)
    return _PUNCT_RE.sub("", "".join(out).lower())


def detect_intents(message: str) -> set[str]:
    msg = normalize(message)
    found = set()
    for intent, words in INTENT_WORDS.items():
        if any(normalize(w) in msg for w in words):
            found.add(intent)
    return found


def _bigrams(text: str) -> set[str]:
    norm = normalize(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _coverage(message: str, text: str) -> float:
    """问题里有多少比重能在条目文本中找到（0~1）。"""
    q = _bigrams(message)
    if not q:
        return 0.0
    return len(q & _bigrams(text)) / len(q)


def _entry_text(entry: dict) -> str:
    return " ".join(
        str(entry.get(key) or "")
        for key in ("section", "questions", "answer", "boundary", "exception", "fallback")
    )


def _entry_intents(entry: dict) -> set[str]:
    text = normalize(_entry_text(entry))
    found = set(intent for intent, words in INTENT_WORDS.items()
                if any(normalize(w) in text for w in words))
    for metric in entry.get("metrics") or []:
        mapped = {
            "price_cny": "price", "size_m": "size", "weight_g_m2": "weight",
            "thread_count": "thread", "shrink_pct": "shrink", "wash_temp_c": "wash",
            "iron_temp_c": "iron", "dry_temp_c": "dry", "ship_hours": "ship",
            "workdays": "leadtime", "markup_pct": "markup",
        }.get(metric)
        if mapped:
            found.add(mapped)
    return found


def _match_products(message: str, products: list[dict]) -> list[dict]:
    msg = normalize(message)
    hits = []
    for product in products:
        for alias in product.get("aliases") or []:
            norm = normalize(alias)
            if len(norm) >= 2 and norm not in _GENERIC_ALIASES and norm in msg:
                hits.append(product)
                break
    return hits


def _detect_category(msg_norm: str) -> str:
    """问题里最具体的品类词 → 品类代码；没有明确品类词返回空串。

    取「最长命中」而不是「先命中」：「床垫保护套」不能被「保护套」抢先，
    「四件套」也不能被「套件」抢先。
    """
    best_code = ""
    best_len = 0
    for code, words in _CATEGORY_RULES:
        for word in words:
            norm = normalize(word)
            if norm and norm in msg_norm and len(norm) > best_len:
                best_code, best_len = code, len(norm)
    return best_code


def _entry_categories(entry: dict) -> set[str]:
    """条目所属品类代码集合；空集合表示通用条目（不绑定品类）。"""
    prefix = (entry.get("file") or "")[:2]
    if prefix in {"02", "03", "04", "05"}:
        return {prefix}
    section = entry.get("section") or ""
    for title, codes in _ENTRY_CATEGORY_OVERRIDES.items():
        if normalize(title) == normalize(section):
            return set(codes)
    norm = normalize(section)
    return {
        code for code, words in _ENTRY_CATEGORY_HINTS
        if any(normalize(word) in norm for word in words)
    }


def _presence_check(message: str, entry: dict) -> bool:
    """问题里点名了床型/规格时，原文答复必须能对上，否则不命中。"""
    msg = normalize(message)
    specs = re.findall(r"(12|15|18)(?:m|米)", msg)
    if not specs:
        return True
    answer = normalize(entry.get("answer") or "")
    if all(f"{spec}m" in answer or f"{spec}米" in answer for spec in specs):
        return True
    # 原文只列了在售规格（如「规格 1.5m、1.8m」）时，整段返回同样能回答「有没有 1.2m」。
    return "规格" in answer


def match_fact(message: str, index: dict) -> dict | None:
    """返回命中的条目与拼好的回复；未命中返回 None。

    返回值结构：{"reply", "entry", "score", "intents"}
    """
    intents = detect_intents(message)
    msg_norm = normalize(message)
    if any(normalize(word) in msg_norm for word in ("刺绣", "logo", "加价", "定制费")):
        intents = {"markup"}
    elif "leadtime" in intents and any(word in msg_norm for word in ("多久", "周期", "几天", "做完")):
        intents = {"leadtime"}
    if not intents or _NON_FACT_RE.search(msg_norm):
        return None

    products = index.get("products") or []
    product_hits = _match_products(message, products)
    if len(product_hits) > 1:
        return None
    product = product_hits[0] if product_hits else None

    if product is None and (intents & _NEEDS_PRODUCT):
        return None

    category = _detect_category(msg_norm)
    # 商品与问题里的品类词冲突时，不猜：交给模型澄清。
    if product and category and (product.get("file") or "")[:2] in {"02", "03", "04", "05"}:
        if (product.get("file") or "")[:2] != category:
            return None
    # 品类归属不清（既没品类词也没商品名）时，这些事实不猜，交回模型。
    if not category and product is None and (intents & _CATEGORY_BOUND_INTENTS):
        return None

    best = None
    second = 0.0
    for entry in index.get("entries") or []:
        entry_intents = _entry_intents(entry)
        shared = intents & entry_intents
        if not shared:
            continue

        same_product = bool(product and entry.get("product") == product.get("name"))
        section = entry.get("section") or ""
        if any(word in section for word in _NON_FACT_SECTIONS):
            continue
        if product is None and entry.get("scope") == "product":
            continue
        if category:
            entry_cats = _entry_categories(entry)
            if entry_cats and category not in entry_cats:
                continue
            # 有品类词时，跨品类通用段落不再参与竞争；本品类自己的条目
            # （02–05 文件内）已由上一步归到该品类，不受影响。
            if not entry_cats and (intents & _CATEGORY_BOUND_INTENTS):
                continue
        if product and entry.get("scope") == "product" and not same_product:
            continue
        if not _presence_check(message, entry):
            continue

        topic = " ".join((section, " ".join(entry.get("questions") or [])))
        cov = max(_coverage(message, topic), 0.5 * _coverage(message, _entry_text(entry)))
        # 段落标题直接含意图词（如「运费与包邮规则」）时，允许低覆盖也进入打分。
        section_word_hit = any(
            normalize(word) in normalize(section)
            for intent in shared
            for word in INTENT_WORDS.get(intent, ())
        )
        deadline_hit = (
            "return" in shared
            and ("几天" in msg_norm or "多久" in msg_norm)
            and "7 天" in (entry.get("answer") or "")
        )
        answer_word_hit = any(
            normalize(word) in normalize(entry.get("answer") or "")
            for intent in shared
            for word in INTENT_WORDS.get(intent, ())
        )
        if (not same_product and cov < 0.34 and not section_word_hit
                and not deadline_hit and not answer_word_hit):
            continue

        score = 10.0 * len(shared) + (12.0 if same_product else 0.0) + 30.0 * cov
        # 已知商品时，别品类的通用段落要让位给本品类原文。
        if product and entry.get("scope") == "category" and entry.get("file") != product.get("file"):
            score -= 12.0
        if category and (entry.get("file") or "").startswith(category):
            score += 8.0
        for intent in shared:
            hints = _SECTION_HINTS.get(intent) or ()
            score += 3.0 * sum(1 for hint in hints if hint in section)
            if _DEFAULT_SECTIONS.get(intent) == section:
                score += 10.0
            for keyword, target in _PREFERRED_SECTIONS.get(intent, ()):
                if keyword in normalize(message) and section == target:
                    score += 10.0
                    break
        if not any(q in msg_norm for q in _QUALIFIERS):
            if any(q in section for q in _QUALIFIERS):
                score -= 6.0
        # 「退换货要几天」这类问题点名了期限，优先 7 天口径原文。
        if "return" in shared and ("几天" in msg_norm or "多久" in msg_norm):
            if "7 天" in (entry.get("answer") or ""):
                score += 15.0
        if best is None or score > best[0]:
            second = best[0] if best else 0.0
            best = (score, entry, shared)
        elif score > second:
            second = score

    if best is None:
        return None
    score, entry, shared = best
    if score < 12.0:
        return None
    # 只有两个不同条目分数接近时才判歧义；同一商品的相邻段落允许略低。
    if second and score - second < 2.0 and second >= 12.0:
        return None

    answer = (entry.get("answer") or "").strip()
    if not answer or "[[NO_ANSWER" in answer:
        return None

    return {
        "reply": "亲，" + answer,
        "entry": entry,
        "score": round(score, 2),
        "intents": sorted(shared),
    }


def load_index(path: Path | str = DEFAULT_INDEX_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def search(message: str, index: dict | None = None) -> dict | None:
    """便捷入口：未传索引时按默认路径加载。"""
    if index is None:
        index = load_index()
    return match_fact(message, index)
