"""Unit tests for the NEXA dataset catalog (no browser or network required)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dataset.catalog import DatasetCatalog


class DatasetCatalogTests(unittest.TestCase):
    def test_bundled_dataset_loads_and_has_unique_ids(self):
        catalog = DatasetCatalog()
        self.assertGreaterEqual(len(catalog.records), 200)
        self.assertEqual(catalog.stats()["load_error_count"], 0)
        ids = [r["task_id"] for r in catalog.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_filter_by_category_site_and_query(self):
        catalog = DatasetCatalog()
        result = catalog.list_tasks(category="recovery", limit=200)
        self.assertGreater(result["total"], 0)
        self.assertTrue(all(r["category"] == "recovery" for r in result["items"]))
        site_result = catalog.list_tasks(site="Wikipedia", limit=200)
        self.assertTrue(all("wikipedia" in r["target"]["site_name"].lower() for r in site_result["items"]))
        query_result = catalog.list_tasks(query="Python tutorial", limit=20)
        self.assertGreater(query_result["total"], 0)

    def test_retrieve_returns_ranked_examples(self):
        catalog = DatasetCatalog()
        matches = catalog.retrieve("Search for Python tutorials on YouTube and open a result", limit=5)
        self.assertTrue(matches)
        self.assertIn("score", matches[0])
        self.assertIn("task", matches[0])

    def test_missing_file_reports_error_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = DatasetCatalog(Path(tmp))
            self.assertEqual(catalog.records, [])
            self.assertEqual(catalog.stats()["load_error_count"], 1)

    def test_invalid_or_duplicate_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            base = {
                "task_id": "DUP-1", "instruction": "Open example", "category": "navigation",
                "task_type": "navigate", "target": {"site_name": "Example", "start_url": "https://example.com", "allowed_domains": ["example.com"]},
                "planned_steps": [{"action": "navigate"}], "success_criteria": ["verify"],
            }
            (path / "tasks.jsonl").write_text(json.dumps(base)+"\n"+json.dumps(base)+"\n", encoding="utf-8")
            catalog = DatasetCatalog(path)
            self.assertEqual(len(catalog.records), 1)
            self.assertEqual(len(catalog.load_errors), 1)

    def test_rejects_non_http_start_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            item = {
                "task_id": "BAD-1", "instruction": "Run code", "category": "test", "task_type": "test",
                "target": {"site_name": "Bad", "start_url": "file:///etc/passwd", "allowed_domains": ["example.com"]},
                "planned_steps": [{"action": "navigate"}], "success_criteria": [],
            }
            (path / "tasks.jsonl").write_text(json.dumps(item)+"\n", encoding="utf-8")
            catalog = DatasetCatalog(path)
            self.assertEqual(catalog.records, [])
            self.assertIn("HTTP(S)", catalog.load_errors[0])


if __name__ == "__main__":
    unittest.main()
