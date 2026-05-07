# tests/test_pipeline_multi.py
import pytest
from unittest.mock import patch
import json
from pipeline import get_shot_at, collect_videos

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

def test_collect_videos_explicit_list(tmp_path):
    v1 = tmp_path / "IMG_8435.MOV"
    v2 = tmp_path / "IMG_8436.MOV"
    v1.touch(); v2.touch()
    with patch("pipeline.get_shot_at", side_effect=["2025-11-16T20:19:29-0800", "2025-11-16T20:21:42-0800"]):
        result = collect_videos(videos=[str(v1), str(v2)], video_dir=None)
    assert [r["video"] for r in result] == [str(v1), str(v2)]
    assert result[0]["shot_at"] == "2025-11-16T20:19:29-0800"

def test_collect_videos_dir_sorted_by_shot_at(tmp_path):
    # v2 shot before v1 despite alphabetical order
    v1 = tmp_path / "IMG_8436.MOV"
    v2 = tmp_path / "IMG_8435.MOV"
    v1.touch(); v2.touch()
    with patch("pipeline.get_shot_at", side_effect={
        str(v1): "2025-11-16T20:21:42-0800",
        str(v2): "2025-11-16T20:19:29-0800",
    }.get):
        result = collect_videos(videos=[], video_dir=str(tmp_path))
    assert result[0]["video"] == str(v2)  # earlier shot_at first

def test_collect_videos_falls_back_to_filename_sort_when_no_timestamp(tmp_path):
    v1 = tmp_path / "IMG_8435.MOV"
    v2 = tmp_path / "IMG_8436.MOV"
    v1.touch(); v2.touch()
    with patch("pipeline.get_shot_at", return_value=None):
        result = collect_videos(videos=[], video_dir=str(tmp_path))
    assert result[0]["video"] == str(v1)

def test_describe_output_includes_source_video_and_shot_at(tmp_path):
    """chunks.json written by describe must have source_video on each chunk and shot_at at top level."""
    chunks_path = tmp_path / "chunks.json"
    data = {
        "video": "/path/video.MOV",
        "shot_at": "2025-11-16T20:19:29-0800",
        "duration": 20.0,
        "chunks": [
            {"start": 0.0, "end": 10.0, "source_video": "/path/video.MOV", "description": "x"},
            {"start": 10.0, "end": 20.0, "source_video": "/path/video.MOV", "description": "y"},
        ],
        "speech": [],
    }
    chunks_path.write_text(json.dumps(data))
    loaded = json.loads(chunks_path.read_text())
    assert loaded["shot_at"] == "2025-11-16T20:19:29-0800"
    assert loaded["chunks"][0]["source_video"] == "/path/video.MOV"
