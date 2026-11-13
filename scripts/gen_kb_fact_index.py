#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从知识库上传文件生成事实查表索引（data/kb_fact_index.json）。

索引只做两件事：
1. 把每条 `###` 问答的原文答复按 `- 答复：` 逐字搬进 JSON；
2. 记录商品别名、指标类型与出处，供 `kb_fact_answer.py` 做确定性命中。

用法：
    python3 scripts/gen_kb_fact_index.py            # 生成/覆盖
    python3 scripts/gen_kb_fact_index.py --check    # 只校验索引是否与知识库同步
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kb_numeric_guard import extract_claims  # noqa: E402

UPLOAD_DIR = ROOT / "docs" / "knowledge-base" / "upload"
INDEX_PATH = ROOT / "data" / "kb_fact_index.json"
SCHEMA_VERSION = 1

_FIELD_MAP = {
    "常见问法": "questions",
    "答复": "answer",
    "适用条件": "conditions",
    "数值边界": "boundary",
    "例外": "exception",
    "兜底": "fallback",
}
_ALIAS_LINE = "口语叫法："
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_GENERIC_ALIASES = {
    "四件套", "三件套", "六件套", "被芯", "枕芯", "保护套", "床垫保护套",
    "被子", "枕头", "床单", "被套", "枕套", "套件", "床垫罩",
}


def _split_questions(raw: str) -> list[str]:
    return [q.strip() for q in re.split(r"[？?；;]", raw) if q.strip()]


def _split_aliases(raw: str) -> list[str]:
    return [a.strip() for a in re.split(r"[、，,；;/]", raw) if a.strip()]


def _product_aliases(name: str, spoken: list[str]) -> list[str]:
    """商品名本身 + 去括号名 + · 前缀 + 口语叫法，去重且剔除泛词。"""
    candidates = [name, _PAREN_RE.sub("", name)]
    if "·" in name:
        candidates.append(name.split("·", 1)[0])
    candidates.extend(spoken)
    out = []
    for item in candidates:
        item = item.strip(" ·-—_")
        if len(item) < 2 or item in _GENERIC_ALIASES or item in out:
            continue
        out.append(item)
    return out


def _metrics_of(text: str) -> list[str]:
    keys = []
    for claim in extract_claims(text):
        if claim.key not in keys:
            keys.append(claim.key)
    return keys


def build_index(upload_dir: Path = UPLOAD_DIR) -> dict:
    entries: list[dict] = []
    products: list[dict] = []
    files = sorted(upload_dir.glob("*.md"))

    for path in files:
        h2 = ""
        spoken: list[str] = []
        current: dict | None = None
        section_no = 0

        def flush() -> None:
            nonlocal current
            if current is None:
                return
            if current.get("answer"):
                entries.append(current)
            current = None

        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                flush()
                h2 = line[3:].strip()
                spoken = []
            elif line.startswith("### "):
                flush()
                section_no += 1
                current = {
                    "id": f"{path.stem}#{section_no}",
                    "file": path.name,
                    "scope": "category",
                    "category": h2,
                    "product": "",
                    "section": line[4:].strip(),
                    "questions": [],
                    "answer": "",
                    "conditions": "",
                    "boundary": "",
                    "exception": "",
                    "fallback": "",
                    "metrics": [],
                }
                if h2 and spoken:
                    current["scope"] = "product"
                    current["product"] = h2
            elif line.startswith(_ALIAS_LINE):
                spoken = _split_aliases(line[len(_ALIAS_LINE):])
                if h2 and not any(p["name"] == h2 and p["file"] == path.name for p in products):
                    products.append({
                        "name": h2,
                        "file": path.name,
                        "aliases": _product_aliases(h2, spoken),
                    })
            elif current is not None and line.startswith("- ") and "：" in line:
                key, _, value = line[2:].partition("：")
                key, value = key.strip(), value.strip()
                field = _FIELD_MAP.get(key)
                if not field:
                    continue
                if field == "questions":
                    current["questions"] = _split_questions(value)
                else:
                    current[field] = value
        flush()

    for entry in entries:
        entry["metrics"] = _metrics_of(
            " ".join(str(entry.get(k) or "") for k in ("answer", "boundary", "exception"))
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": upload_dir.name,
        "file_count": len(files),
        "entry_count": len(entries),
        "products": products,
        "entries": entries,
    }


def _diff(expected: dict, actual: dict) -> list[str]:
    problems: list[str] = []
    for key in ("schema_version", "generated_from", "file_count", "entry_count"):
        if expected.get(key) != actual.get(key):
            problems.append(f"{key}: 索引={actual.get(key)!r} 知识库={expected.get(key)!r}")
    if len(expected["products"]) != len(actual.get("products") or []):
        problems.append(f"商品数: 索引={len(actual.get('products') or [])} 知识库={len(expected['products'])}")
    if len(expected["entries"]) != len(actual.get("entries") or []):
        problems.append(f"问答条目数: 索引={len(actual.get('entries') or [])} 知识库={len(expected['entries'])}")
    by_id = {e["id"]: e for e in actual.get("entries") or []}
    for entry in expected["entries"]:
        old = by_id.get(entry["id"])
        if old is None:
            problems.append(f"{entry['file']} {entry['section']}: 索引缺少该条目")
            continue
        for field in ("answer", "boundary", "exception", "section", "product"):
            if old.get(field) != entry[field]:
                problems.append(
                    f"{entry['file']} {entry['section']}: 字段 {field} 与知识库不一致"
                )
    return problems


def main(argv: list[str]) -> int:
    index = build_index()
    if "--check" in argv:
        if not INDEX_PATH.is_file():
            print(f"缺少索引文件：{INDEX_PATH}")
            return 1
        actual = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        problems = _diff(index, actual)
        if problems:
            print(f"事实索引与知识库不一致（{len(problems)} 处），请重新运行生成脚本：")
            for item in problems[:30]:
                print(f"  - {item}")
            return 1
        print(f"事实索引已同步：{index['file_count']} 个文件、{index['entry_count']} 条问答、"
              f"{len(index['products'])} 个商品")
        return 0

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {INDEX_PATH.relative_to(ROOT)}：{index['file_count']} 个文件、"
          f"{index['entry_count']} 条问答、{len(index['products'])} 个商品")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
