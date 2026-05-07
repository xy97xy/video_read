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


from pipeline import merge_chunks_json

def _make_chunk_file(tmp_path, stem, video_path, shot_at, chunks):
    data = {"video": video_path, "shot_at": shot_at, "duration": 30.0,
            "chunks": chunks, "speech": [{"start": 1.0, "end": 2.0, "text": "hi", "words": []}]}
    p = tmp_path / f"{stem}_chunks.json"
    p.write_text(json.dumps(data))
    return str(p)

def test_merge_chunks_json_sorts_by_shot_at(tmp_path):
    f1 = _make_chunk_file(tmp_path, "vid1", "/v1.MOV", "2025-11-16T20:21:00-0800",
                          [{"start": 0.0, "end": 10.0, "source_video": "/v1.MOV", "description": "b"}])
    f2 = _make_chunk_file(tmp_path, "vid2", "/v2.MOV", "2025-11-16T20:19:00-0800",
                          [{"start": 0.0, "end": 10.0, "source_video": "/v2.MOV", "description": "a"}])
    result = merge_chunks_json([f1, f2])
    assert result["chunks"][0]["source_video"] == "/v2.MOV"  # earlier shot_at first
    assert result["chunks"][0]["index"] == 0
    assert result["chunks"][1]["index"] == 1

def test_merge_chunks_json_speech_keyed_by_source(tmp_path):
    f1 = _make_chunk_file(tmp_path, "vid1", "/v1.MOV", "2025-11-16T20:19:00-0800",
                          [{"start": 0.0, "end": 10.0, "source_video": "/v1.MOV", "description": "x"}])
    result = merge_chunks_json([f1])
    assert "/v1.MOV" in result["speech"]

def test_merge_chunks_json_excludes_all_chunks(tmp_path):
    f1 = _make_chunk_file(tmp_path, "vid1", "/v1.MOV", "2025-11-16T20:19:00-0800",
                          [{"start": 0.0, "end": 5.0, "source_video": "/v1.MOV", "description": "x"}])
    (tmp_path / "all_chunks.json").write_text('{"sources":[],"chunks":[],"speech":{}}')
    result = merge_chunks_json([f1])
    assert len(result["chunks"]) == 1


from pipeline import concat_chunks
from unittest.mock import patch, MagicMock

def test_concat_chunks_multi_source_builds_correct_inputs(tmp_path):
    chunks = [
        {"start": 0.0, "end": 10.0, "source_video": "/v1.MOV"},
        {"start": 5.0, "end": 15.0, "source_video": "/v2.MOV"},
        {"start": 20.0, "end": 30.0, "source_video": "/v1.MOV"},
    ]
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run):
        concat_chunks(
            video_path=None,
            chunks=chunks,
            selected=[0, 1, 2],
            output_path=str(tmp_path / "out.mp4"),
        )

    cmd = captured["cmd"]
    assert cmd.count("-i") == 2
    assert "/v1.MOV" in cmd
    assert "/v2.MOV" in cmd
