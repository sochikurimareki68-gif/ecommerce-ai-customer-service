# -*- coding: utf-8 -*-
"""知识库数值守卫：把知识库里的数字变成可核对的清单，运行时拦截模型改数。

设计原则
--------
1. 清单只从 `docs/knowledge-base/upload/*.md` 生成，不手写。
2. 只校验有明确单位和上下文的指标（水温、价格、克重、缩水率等），
   没有品类/商品上下文时不判，减少误杀。
3. 允许值 = 知识库里实际出现过的值的集合，而不是按业务规则推导。
   例如知识库写「水温 ≤40℃」，模型输出 30℃ 也会判冲突，因为 30 未在
   该品类出现过——这正是本守卫要拦的「模型改数」。
4. 清单里同时保留品类级（通用段）和商品级两套指标：
   能唯一定位到商品时用商品级，否则用品类通用段；都没有就放行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent / "data" / "kb_numeric_manifest.json"

_NUM = r"\d+(?:\.\d+)?"
_RANGE = rf"{_NUM}(?:\s*[-~至到]\s*{_NUM})?"

# 指标提取规则：group(1) 是数值或数值区间。
# 规则刻意带上下文（水温/熨烫/烘干/缩水/加价…），避免把普通叙述里的数字当成规格。
_METRIC_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wash_temp_c", re.compile(
        rf"(?:水温|温水|冷水|洗涤|机洗|水洗|清洗)[^。；;，,\n]{{0,16}}?({_RANGE})\s*(?:℃|度|摄氏度)")),
    ("wash_temp_c", re.compile(
        rf"(?:天丝|莱赛尔)[^。；;，,\n]{{0,12}}?({_RANGE})\s*(?:℃|度|摄氏度)")),
    ("wash_temp_c", re.compile(
        rf"(?:≤|不超过|低于)\s*({_RANGE})\s*(?:℃|度|摄氏度)[^。；;，,\n]{{0,8}}?(?:手洗|机洗|水洗)")),
    ("iron_temp_c", re.compile(
        rf"熨烫[^。；;，,\n]{{0,16}}?({_RANGE})\s*(?:℃|度|摄氏度)")),
    ("dry_temp_c", re.compile(
        rf"烘干[^。；;，,\n]{{0,16}}?({_RANGE})\s*(?:℃|度|摄氏度)")),
    ("weight_g_m2", re.compile(
        rf"({_RANGE})\s*(?:g|克)\s*/\s*(?:㎡|m2|m²|平方米|平米)")),
    ("shrink_pct", re.compile(
        rf"缩水(?:率)?[^。；;，,\n]{{0,12}}?({_RANGE})\s*%")),
    ("markup_pct", re.compile(
        rf"加价[^。；;，,\n]{{0,12}}?({_RANGE})\s*%")),
    ("thread_count", re.compile(rf"({_NUM})\s*支")),
    ("size_m", re.compile(rf"(?<![\d.])({_NUM})\s*(?:m|米)(?![a-zA-Z0-9])")),
    ("ship_hours", re.compile(rf"(?:延迟至|延迟到)\s*({_RANGE})\s*小时")),
    ("ship_hours", re.compile(rf"({_RANGE})\s*小时(?:内)?(?:发货|内为发货时效)")),
    ("workdays", re.compile(rf"({_RANGE})\s*个工作日")),
    ("price_cny", re.compile(rf"[¥￥]\s*({_RANGE})")),
    ("price_cny", re.compile(rf"({_RANGE})\s*元")),
)

# 价格里的促销语境不当成商品价，避免「满 300 减 30 元」被误判。
_PRICE_SKIP_PREFIX_RE = re.compile(r"[减券优折省满返]")

_DISCOUNT_CLIP = "。；;，,\n"
_SENTENCE_BOUNDARIES = "。！？!?；;\n"

# 商品名里可剥离的修饰词，用于生成「四件套」「床垫保护套」这类品类别名。
_ATTR_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?支|\d+(?:\.\d+)?%|100%|纯棉|磨毛|天丝|莱赛尔|长绒棉|桑蚕丝|大豆纤维|"
    r"白鹅绒|鹅绒|乳胶|记忆棉|软管|法兰绒|有机棉|加厚|保暖|凉感|防水|亲肤|双面|高支|"
    r"新疆|埃及|天然|白|纯|桑)"
)
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_GENERIC_OBJECT_RE = re.compile(
    r"[\u4e00-\u9fa5]{0,6}(?:四件套|六件套|三件套|床垫保护套|保护套|被芯|枕芯|床单|被套|枕套|被子|枕头|被|枕)$"
)
_TOPIC_RE = re.compile(
    r"发货|物流|配送|到货|签收|退换|退货|换货|退款|优惠|满减|折扣|售后|洗护|清洗|"
    r"晾晒|保养|发票|定制|团购|会员|积分|支付|客服|保修|质量|螨虫|过敏"
)

# 标题为这些内容的 ## 段落视为品类通用段（价格对照、洗护通则、物流政策等），
# 其余 ## 段落视为具体商品。
_SHARED_SECTION_RE = re.compile(
    r"全系|系列对比|对比|总览|选购|搭配|首次使用|按面料洗护|按品类清洁|晾晒与收纳|"
    r"清洗频率|常见问题|洗护与保养|枕芯与枕套|售后|物流|发货|优惠|活动|退换|联系|"
    r"增值|品牌|认证|规格|其他"
)


@dataclass(frozen=True)
class MetricClaim:
    key: str
    numbers: tuple[float, ...]
    raw: str
    start: int
    end: int


@dataclass
class GuardViolation:
    metric: str
    value: float
    allowed: tuple[float, ...]
    category: str
    product: str
    snippet: str

    def as_log(self) -> dict:
        return {
            "metric": self.metric,
            "value": _display_number(self.value),
            "allowed": [_display_number(v) for v in self.allowed],
            "category": self.category,
            "product": self.product,
            "snippet": self.snippet,
        }


def _numbers_in(raw: str) -> tuple[float, ...]:
    return tuple(float(n) for n in re.findall(_NUM, raw))


def _display_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def extract_claims(text: str) -> list[MetricClaim]:
    """从一段文本里提取所有可校验的数值声明。"""
    claims: list[MetricClaim] = []
    seen: set[tuple[str, int, int]] = set()
    for key, pattern in _METRIC_RULES:
        for match in pattern.finditer(text):
            raw = match.group(1)
            if not raw:
                continue
            numbers = _numbers_in(raw)
            if not numbers:
                continue
            if key == "price_cny" and match.group(0).endswith("元"):
                prefix = text[max(0, match.start() - 6):match.start()]
                if _PRICE_SKIP_PREFIX_RE.search(prefix):
                    continue
            token = (key, match.start(1), match.end(1))
            if token in seen:
                continue
            seen.add(token)
            claims.append(MetricClaim(key, numbers, raw, match.start(1), match.end(1)))
    claims.sort(key=lambda c: c.start)
    return claims


# ----------------------------------------------------------------------
# 清单生成
# ----------------------------------------------------------------------
def _clean_heading(text: str) -> str:
    return _PAREN_RE.sub("", text).strip()


def _variants(name: str) -> list[str]:
    """生成商品名/品类名的可匹配变体，供最长别名定位。"""
    cleaned = _clean_heading(name)
    if not cleaned:
        return []
    parts = [p.strip() for p in cleaned.split("·") if p.strip()]
    seed = [cleaned, *parts]
    out: list[str] = []
    for item in seed:
        item = item.strip("·-—_ ")
        if not item:
            continue
        out.append(item)
        work = item
        # 逐步剥修饰词，保留每一步的中间结果（如「长绒棉四件套」「四件套」）
        while True:
            stripped = _ATTR_RE.sub("", work).strip("·-—_ ")
            if stripped == work or len(stripped) < 2:
                break
            out.append(stripped)
            work = stripped
        # 白鹅绒被 → 鹅绒被、桑蚕丝被 → 蚕丝被
        if len(item) >= 3 and item[0] in "白桑纯":
            out.append(item[1:])
    # 品牌 + 通用名词，如「云柔四件套」「暖眠鹅绒被」
    if len(parts) > 1:
        brand = parts[0]
        for item in list(out):
            if _GENERIC_OBJECT_RE.match(item) and not item.startswith(brand):
                out.append(brand + item)
    # 去掉纯数字/标点，保留真正能出现在答复里的中文片段
    result: list[str] = []
    for item in out:
        item = item.strip("·-—_ ")
        if len(item) < 2 or re.fullmatch(r"[\d.%×xX\-~至到]+", item):
            continue
        if item not in result:
            result.append(item)
    return result


def _generic_aliases(name: str) -> list[str]:
    """只取能代表品类/品类的通用名词，如「四件套」「床垫保护套」。"""
    cleaned = _clean_heading(name)
    result = []
    for item in _variants(name):
        if item == cleaned:
            continue
        if _GENERIC_OBJECT_RE.match(item):
            result.append(item)
    return result


def _add_metric(bucket: dict, key: str, numbers: Iterable[float], sample: str) -> None:
    item = bucket.setdefault(key, {"values": set(), "samples": []})
    item["values"].update(float(n) for n in numbers)
    if sample and len(item["samples"]) < 3 and sample not in item["samples"]:
        item["samples"].append(sample[:120])


def _finalize_metrics(bucket: dict) -> dict:
    return {
        key: {
            "values": sorted(_display_number(v) for v in item["values"]),
            "samples": item["samples"],
        }
        for key, item in sorted(bucket.items())
    }


def _apply_price_table(categories: list[dict], upload_dir: Path) -> None:
    """价格速查表是全品类价格的唯一出处，按小节把价位补回对应品类。

    价格表单独成一个文件（14），若只按文件名归属，02-05 品类的价位集合会变空，
    数值守卫就失去拦串价的能力，因此这里按小节标题把价位回填到 02-05。
    """
    path = upload_dir / "14-价格速查.md"
    if not path.exists():
        return
    by_id = {category["id"]: category for category in categories}
    rules = (
        (("套件",), ("02",)),
        (("被芯",), ("03",)),
        (("枕芯",), ("04",)),
        (("保护套",), ("05",)),
        (("报价口径",), ("02", "03", "04", "05")),
    )
    values: dict[str, set[float]] = {}
    samples: dict[str, list[str]] = {}
    heading = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # 只看二级标题：三级标题是拆分后的单款条目，会把「套件/被芯/枕芯/保护套」
        # 这些分组词覆盖掉，导致价位无法回填到 02-05。
        if line.startswith("## ") and not line.startswith("### "):
            heading = line[3:].strip()
            continue
        if not re.match(r"-\s*答复[：:]", line):
            continue
        targets = next((ids for words, ids in rules if any(w in heading for w in words)), ())
        if not targets:
            continue
        claims = [c for c in extract_claims(line) if c.key == "price_cny"]
        if not claims:
            continue
        for cat_id in targets:
            if cat_id not in by_id:
                continue
            bucket = values.setdefault(cat_id, set())
            for claim in claims:
                bucket.update(float(v) for v in claim.numbers)
            kept = samples.setdefault(cat_id, [])
            if len(kept) < 3 and line[:120] not in kept:
                kept.append(line[:120])
    for cat_id, nums in values.items():
        metrics = by_id[cat_id]["shared_metrics"]
        metric = metrics.setdefault("price_cny", {"values": [], "samples": []})
        merged = {float(v) for v in metric["values"]} | nums
        metric["values"] = sorted(_display_number(v) for v in merged)
        metric["samples"] = (metric["samples"] + samples.get(cat_id, []))[:3]


def build_manifest(upload_dir: Path) -> dict:
    """从知识库目录生成清单（不读网络，不依赖第三方库）。"""
    files = sorted(upload_dir.glob("*.md"))
    categories: list[dict] = []
    for path in files:
        stem = path.stem
        match = re.match(r"^\s*(\d+)\s*[-_]\s*(.+)$", stem)
        cat_id = match.group(1) if match else ""
        cat_name = (match.group(2) if match else stem).strip()
        category = {
            "id": cat_id,
            "name": cat_name,
            "file": path.name,
            "aliases": [],
            "shared_metrics": {},
            "products": [],
        }
        shared_bucket: dict = {}
        products: dict[str, dict] = {}
        section_aliases: list[str] = []
        current = None  # None = 通用段；否则为商品 dict

        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                heading = line[3:].strip()
                if _SHARED_SECTION_RE.search(heading):
                    current = None
                    section_aliases.extend(_variants(heading))
                    section_aliases.extend(_TOPIC_RE.findall(heading))
                else:
                    current = {
                        "name": _clean_heading(heading),
                        "aliases": _variants(heading),
                        "metrics": {},
                    }
                    products.setdefault(current["name"], current)
                active_field = False
                continue
            if not line.startswith("- "):
                active_field = False
                continue
            indented = raw[:1].isspace()
            if not indented:
                if not re.match(r"-\s*(答复|数值边界|例外)[：:]", line):
                    active_field = False
                    continue
                active_field = True
            elif not active_field:
                # 结构拆分后的分条续行，仍属于上一条答复/数值边界/例外
                continue
            claims = extract_claims(line)
            if not claims:
                continue
            bucket = shared_bucket if current is None else current["metrics"]
            for claim in claims:
                _add_metric(bucket, claim.key, claim.numbers, line)

        category["shared_metrics"] = _finalize_metrics(shared_bucket)
        category["products"] = [
            {
                "name": item["name"],
                "aliases": item["aliases"],
                "metrics": _finalize_metrics(item["metrics"]),
            }
            for item in products.values()
        ]
        aliases = list(_variants(cat_name))
        for alias in section_aliases:
            if alias not in aliases:
                aliases.append(alias)
        for item in category["products"]:
            for alias in _generic_aliases(item["name"]):
                if alias not in aliases:
                    aliases.append(alias)
        category["aliases"] = aliases
        categories.append(category)

    _apply_price_table(categories, upload_dir)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_from": upload_dir.name,
        "file_count": len(files),
        "metric_keys": sorted({key for key, _ in _METRIC_RULES}),
        "categories": categories,
    }


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise ValueError("数值清单格式不正确")
    return data


# ----------------------------------------------------------------------
# 运行时校验
# ----------------------------------------------------------------------
def _best_alias(text: str, aliases: Iterable[str]) -> int:
    """返回文本里命中的最长别名长度，未命中返回 0。"""
    best = 0
    for alias in aliases:
        if alias and alias in text and len(alias) > best:
            best = len(alias)
    return best


def _sentence_around(text: str, start: int, end: int) -> str:
    left = start
    while left > 0 and text[left - 1] not in _SENTENCE_BOUNDARIES:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _SENTENCE_BOUNDARIES:
        right += 1
    return text[left:right]


def _match_categories(text: str, manifest: dict) -> list[tuple[dict, int]]:
    """按命中别名的长度排序返回候选品类；商品别名也算，避免「优惠」抢走商品价。"""
    scored: list[tuple[dict, int]] = []
    for category in manifest.get("categories", []):
        score = _best_alias(text, category.get("aliases", []))
        for product in category.get("products", []):
            score = max(score, _best_alias(text, product.get("aliases", [])))
        if score:
            scored.append((category, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _match_products(text: str, category: dict) -> list[dict]:
    """按最长别名定位最具体的商品；同名通用词命中多个时返回多个。"""
    matches: list[tuple[dict, int]] = []
    for product in category.get("products", []):
        score = _best_alias(text, product.get("aliases", []))
        if score:
            matches.append((product, score))
    if not matches:
        return []
    top = max(score for _, score in matches)
    return [product for product, score in matches if score == top]


def _category_values(category: dict, metric: str) -> tuple[float, ...]:
    """品类允许值 = 通用段 ∪ 全部商品值，避免通用段漏记导致误杀。"""
    values: set[float] = set()
    for item in category.get("shared_metrics", {}).get(metric, {}).get("values", []):
        values.add(float(item))
    for product in category.get("products", []):
        for item in product.get("metrics", {}).get(metric, {}).get("values", []):
            values.add(float(item))
    return tuple(sorted(values))


def _allowed_values(category: dict, product: dict | None, metric: str) -> tuple[float, ...]:
    if product is not None:
        values = product.get("metrics", {}).get(metric, {}).get("values")
        if values:
            return tuple(float(v) for v in values)
    return _category_values(category, metric)


def find_violation(
    reply: str,
    context: str = "",
    manifest: dict | None = None,
) -> GuardViolation | None:
    """检查一段答复；返回第一条冲突，未命中返回 None。

    context 一般传用户问题 + 已下发的上下文，用于在答复本身没写品类时
    仍能推断「这句话在说哪个品类」。
    """
    if not reply or not manifest:
        return None
    hint = (context or "")[-160:]

    for claim in extract_claims(reply):
        sentence = _sentence_around(reply, claim.start, claim.end)
        candidates = _match_categories(sentence, manifest)
        if not candidates:
            candidates = _match_categories(hint, manifest)
        if not candidates:
            continue

        # 同一句话可能同时命中多个品类（如「优惠：云柔 ¥199」既像活动又像商品）。
        # 只要有一个品类能解释这些数字，就按放行处理，避免跨品类误杀；
        # 全部候选都解释不了时才判冲突，取别名最具体的那一个。
        conflict: GuardViolation | None = None
        for category, _score in candidates:
            products = _match_products(sentence, category)
            product = products[0] if len(products) == 1 else None
            allowed = _allowed_values(category, product, claim.key)
            if not allowed:
                continue
            if all(
                any(abs(number - value) < 1e-6 for value in allowed)
                for number in claim.numbers
            ):
                conflict = None
                break
            if conflict is None:
                bad = next(
                    number for number in claim.numbers
                    if not any(abs(number - value) < 1e-6 for value in allowed)
                )
                conflict = GuardViolation(
                    metric=claim.key,
                    value=bad,
                    allowed=allowed,
                    category=category.get("name", ""),
                    product=(product or {}).get("name", ""),
                    snippet=sentence[:120],
                )
        if conflict is not None:
            return conflict
    return None
