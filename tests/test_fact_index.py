# -*- coding: utf-8 -*-
"""事实索引（data/kb_fact_index.json）的同步与完整性测试。

覆盖：
  · 索引与知识库 14 个 md 逐字段同步
  · 条目数、商品数、答复原文逐字可回溯
  · 答复里不含占位标记
  · 生成脚本 --check 能发现漂移
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UPLOAD_DIR = ROOT / "docs" / "knowledge-base" / "upload"
INDEX_PATH = ROOT / "data" / "kb_fact_index.json"

_spec = importlib.util.spec_from_file_location(
    "gen_kb_fact_index", ROOT / "scripts" / "gen_kb_fact_index.py"
)
gen_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_index)


class FactIndexTest(unittest.TestCase):
    def setUp(self):
        self.index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))

    def test_index_is_in_sync_with_knowledge_base(self):
        self.assertEqual(self.index, gen_index.build_index(UPLOAD_DIR))

    def test_index_covers_all_files_and_entries(self):
        self.assertEqual(self.index["file_count"], 14)
        self.assertEqual(len(self.index["entries"]), self.index["entry_count"])
        self.assertEqual(self.index["entry_count"], 171)
        self.assertEqual(len(self.index["products"]), 21)

    def test_every_answer_is_verbatim_in_source_file(self):
        for entry in self.index["entries"]:
            source = (UPLOAD_DIR / entry["file"]).read_text(encoding="utf-8")
            self.assertTrue(entry["answer"], entry["id"])
            self.assertIn(entry["answer"], source, entry["id"])

    def test_answers_never_contain_placeholders(self):
        for entry in self.index["entries"]:
            self.assertNotIn("[[", entry["answer"], entry["id"])

    def test_mattress_protector_has_product_and_category_entries(self):
        entries = [e for e in self.index["entries"] if e["file"].startswith("05-")]
        self.assertTrue(entries)
        self.assertEqual({e["scope"] for e in entries}, {"product", "category"})

    def test_check_reports_drift(self):
        generated = gen_index.build_index(UPLOAD_DIR)
        drifted = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        drifted["entries"][0]["answer"] = "被改过的答复"
        problems = gen_index._diff(generated, drifted)
        self.assertTrue(any("answer" in item for item in problems))


if __name__ == "__main__":
    unittest.main()
