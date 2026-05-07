# tests/test_pipeline_multi.py
import pytest
from unittest.mock import patch
import json
from pipeline import get_shot_at

def _ffprobe_tags(tags: dict) -> str:
    return json.dumps({"format": {"tags": tags, "duration": "10.0"}})

def test_get_shot_at_prefers_quicktime_creationdate():
    tags = {
        "com.apple.quicktime.creationdate": "2025-11-16T20:19:29-0800",
        "creation_time": "2025-11-17T04:19:29.000000Z",
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = _ffprobe_tags(tags)
        result = get_shot_at("fake.MOV")
    assert result == "2025-11-16T20:19:29-0800"

def test_get_shot_at_falls_back_to_creation_time():
    tags = {"creation_time": "2025-11-17T04:19:29.000000Z"}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = _ffprobe_tags(tags)
        result = get_shot_at("fake.MOV")
    assert result == "2025-11-17T04:19:29.000000Z"

def test_get_shot_at_returns_none_when_no_tags():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = _ffprobe_tags({})
        result = get_shot_at("fake.MOV")
    assert result is None
