from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_dataset import _domain_allowed, load_and_validate


def test_bundled_dataset_passes_local_validation():
    records, errors = load_and_validate()
    assert len(records) == 300
    assert errors == []


def test_domain_allowlist_matches_subdomains_only():
    assert _domain_allowed("https://en.wikipedia.org/wiki/Python", ["wikipedia.org"])
    assert _domain_allowed("https://wikipedia.org/", ["wikipedia.org"])
    assert not _domain_allowed("https://wikipedia.org.attacker.example/", ["wikipedia.org"])
    assert not _domain_allowed("file:///etc/passwd", ["wikipedia.org"])


def test_validator_detects_duplicate_ids(tmp_path: Path):
    record = {
        "task_id": "same", "instruction": "Search for a public page", "category": "navigation",
        "task_type": "search_open", "target": {"site_name": "Wikipedia", "start_url": "https://www.wikipedia.org/", "allowed_domains": ["wikipedia.org"]},
        "planned_steps": [{"action": "navigate"}], "success_criteria": []
    }
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
    records, errors = load_and_validate(path)
    assert len(records) == 1
    assert len(errors) == 1
    assert "duplicate task_id" in errors[0]["error"]
