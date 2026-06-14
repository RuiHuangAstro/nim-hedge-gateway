import json
import os

import pytest

from app import response_archive
from app.config import config


@pytest.fixture
def archive_path(tmp_path, monkeypatch):
    """Point the archive at a temp file and reset module state."""
    target = tmp_path / "archive.jsonl"
    monkeypatch.setattr(config.archive, "file_path", str(target))
    monkeypatch.setattr(config.archive, "enabled", True)
    monkeypatch.setattr(config.archive, "max_bytes_per_file", 10 * 1024 * 1024)
    monkeypatch.setattr(config.archive, "backup_count", 10)
    monkeypatch.setattr(
        config.archive,
        "categories",
        ["harmony_repaired", "harmony_unparsed", "validation_failed"],
    )
    response_archive.reset_for_tests()
    yield target
    response_archive.reset_for_tests()


def _read_records(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_archive_records_written(archive_path):
    response_archive.archive(
        "harmony_repaired",
        virtual_model="nim-large",
        candidate_name="kimi",
        real_model="moonshotai/kimi-k2.6",
        raw_content="<|tool_call_begin|>functions.Bash:0...<|tool_call_end|>",
        extra={"parsed_calls": 1},
    )
    records = _read_records(archive_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["category"] == "harmony_repaired"
    assert rec["virtual_model"] == "nim-large"
    assert rec["candidate_name"] == "kimi"
    assert rec["real_model"] == "moonshotai/kimi-k2.6"
    assert "<|tool_call_begin|>" in rec["raw_content"]
    assert rec["extra"]["parsed_calls"] == 1
    assert "ts" in rec


def test_archive_disabled_writes_nothing(archive_path, monkeypatch):
    monkeypatch.setattr(config.archive, "enabled", False)
    response_archive.reset_for_tests()
    response_archive.archive(
        "harmony_repaired",
        virtual_model="nim-large",
        raw_content="x",
    )
    assert not os.path.exists(archive_path)


def test_archive_skips_uncaptured_categories(archive_path, monkeypatch):
    monkeypatch.setattr(config.archive, "categories", ["harmony_repaired"])
    response_archive.reset_for_tests()
    response_archive.archive(
        "validation_failed",
        virtual_model="nim-large",
        raw_content="x",
    )
    response_archive.archive(
        "harmony_repaired",
        virtual_model="nim-large",
        raw_content="y",
    )
    records = _read_records(archive_path)
    assert len(records) == 1
    assert records[0]["category"] == "harmony_repaired"


def test_archive_response_dict_extracts_message(archive_path):
    response_dict = {
        "choices": [
            {
                "message": {
                    "content": "hello",
                    "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "Bash", "arguments": "{}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    response_archive.archive(
        "validation_failed",
        virtual_model="nim-large",
        candidate_name="kimi",
        real_model="moonshotai/kimi-k2.6",
        response_dict=response_dict,
        extra={"reason": "bad json"},
    )
    records = _read_records(archive_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["content"] == "hello"
    assert rec["finish_reason"] == "tool_calls"
    assert rec["tool_calls"][0]["function"]["name"] == "Bash"
    assert rec["extra"]["reason"] == "bad json"


def test_archive_rotation_caps_total_size(tmp_path, monkeypatch):
    """A few-record-per-file rotation keeps at most backup_count old files."""
    target = tmp_path / "rot.jsonl"
    monkeypatch.setattr(config.archive, "file_path", str(target))
    monkeypatch.setattr(config.archive, "enabled", True)
    # Tiny size cap so each record likely rotates the file.
    monkeypatch.setattr(config.archive, "max_bytes_per_file", 200)
    monkeypatch.setattr(config.archive, "backup_count", 2)
    monkeypatch.setattr(
        config.archive, "categories", ["harmony_repaired"]
    )
    response_archive.reset_for_tests()

    for i in range(20):
        response_archive.archive(
            "harmony_repaired",
            virtual_model="nim-large",
            candidate_name=f"c{i}",
            raw_content="x" * 150,
        )

    response_archive.reset_for_tests()
    files = sorted(tmp_path.iterdir())
    # At most: archive.jsonl + backup_count backups = 3 files
    assert len(files) <= 3
