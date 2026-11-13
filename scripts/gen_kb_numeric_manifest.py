# -*- coding: utf-8 -*-
"""从知识库 md 重新生成数值清单。

用法：
    python3 scripts/gen_kb_numeric_manifest.py
    python3 scripts/gen_kb_numeric_manifest.py --check   # 只比对，不写文件

清单固定落在 data/kb_numeric_manifest.json，运行时由 server.py 加载。
知识库改了数字后重跑本脚本即可，不需要手改 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb_numeric_guard import build_manifest  # noqa: E402

UPLOAD_DIR = ROOT / "docs" / "knowledge-base" / "upload"
MANIFEST_PATH = ROOT / "data" / "kb_numeric_manifest.json"


def _dump(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def _fmt_values(values: list) -> str:
    return "[" + ", ".join(f"{value:g}" for value in values) + "]"


def _diff_metrics(where: str, old: dict, new: dict) -> list[str]:
    """对比同一层级的指标，输出「哪个指标、清单值 → 知识库值」。"""
    lines: list[str] = []
    for key in sorted(set(old) | set(new)):
        old_item = old.get(key, {})
        new_item = new.get(key, {})
        old_values = old_item.get("values", [])
        new_values = new_item.get("values", [])
        if old_values != new_values:
            lines.append(
                f"{where} · {key}：清单 {_fmt_values(old_values)} → 知识库 {_fmt_values(new_values)}"
            )
        elif old_item.get("samples") != new_item.get("samples"):
            lines.append(f"{where} · {key}：数值一致，原文样本有变化")
    return lines


def diff_manifest(old: dict, new: dict) -> list[str]:
    """逐文件、逐品类、逐商品、逐指标列出清单与知识库的差异。"""
    lines: list[str] = []
    if old.get("schema_version") != new.get("schema_version"):
        lines.append(
            f"schema_version：清单 {old.get('schema_version')} → 知识库生成 {new.get('schema_version')}"
        )

    old_cats = {cat.get("file"): cat for cat in old.get("categories", [])}
    new_cats = {cat.get("file"): cat for cat in new.get("categories", [])}

    for file in sorted(new_cats.keys() - old_cats.keys()):
        lines.append(f"{file}：知识库新增品类「{new_cats[file].get('name', '')}」，清单未包含")
    for file in sorted(old_cats.keys() - new_cats.keys()):
        lines.append(f"{file}：清单中的品类「{old_cats[file].get('name', '')}」在知识库里已不存在")

    for file in sorted(old_cats.keys() & new_cats.keys()):
        old_cat = old_cats[file]
        new_cat = new_cats[file]
        label = f"{file} · {new_cat.get('name', '')}"
        if old_cat.get("name") != new_cat.get("name"):
            lines.append(f"{file}：品类名 {old_cat.get('name', '')} → {new_cat.get('name', '')}")
        if old_cat.get("aliases") != new_cat.get("aliases"):
            lines.append(f"{label}：品类别名有变化")
        lines.extend(_diff_metrics(
            f"{label} · 通用段",
            old_cat.get("shared_metrics", {}),
            new_cat.get("shared_metrics", {}),
        ))

        old_products = {item.get("name"): item for item in old_cat.get("products", [])}
        new_products = {item.get("name"): item for item in new_cat.get("products", [])}
        for name in sorted(new_products.keys() - old_products.keys()):
            lines.append(f"{label}：知识库新增商品「{name}」，清单未包含")
        for name in sorted(old_products.keys() - new_products.keys()):
            lines.append(f"{label}：清单中的商品「{name}」在知识库里已不存在")
        for name in sorted(old_products.keys() & new_products.keys()):
            old_product = old_products[name]
            new_product = new_products[name]
            if old_product.get("aliases") != new_product.get("aliases"):
                lines.append(f"{label} · {name}：商品别名有变化")
            lines.extend(_diff_metrics(
                f"{label} · {name}",
                old_product.get("metrics", {}),
                new_product.get("metrics", {}),
            ))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="生成知识库数值清单")
    parser.add_argument("--check", action="store_true", help="只检查清单是否与知识库同步")
    args = parser.parse_args()

    if not UPLOAD_DIR.is_dir():
        print(f"知识库目录不存在：{UPLOAD_DIR}", file=sys.stderr)
        return 2

    manifest = build_manifest(UPLOAD_DIR)
    text = _dump(manifest)

    if args.check:
        if not MANIFEST_PATH.is_file():
            print(f"❌ 数值清单不存在：{MANIFEST_PATH.relative_to(ROOT)}")
            print("运行 `python3 scripts/gen_kb_numeric_manifest.py` 生成后一并提交。")
            return 1

        current_text = MANIFEST_PATH.read_text(encoding="utf-8")
        if current_text == text:
            print("✅ 数值清单与知识库一致")
            return 0

        print(f"❌ 数值清单已过期：{MANIFEST_PATH.relative_to(ROOT)} 与知识库不一致")
        try:
            current = json.loads(current_text)
        except json.JSONDecodeError as exc:
            print(f"  清单无法解析：{exc}")
        else:
            for line in diff_manifest(current, manifest)[:40]:
                print(f"  · {line}")
        print("运行 `python3 scripts/gen_kb_numeric_manifest.py` 重新生成后一并提交。")
        return 1

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(text, encoding="utf-8")

    cats = manifest["categories"]
    products = sum(len(c["products"]) for c in cats)
    metrics = sum(
        len(c["shared_metrics"]) + sum(len(p["metrics"]) for p in c["products"])
        for c in cats
    )
    print(f"已生成 {MANIFEST_PATH.relative_to(ROOT)}")
    print(f"品类 {len(cats)} 个 / 商品 {products} 个 / 指标项 {metrics} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
