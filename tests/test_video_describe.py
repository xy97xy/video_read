import os, sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _fake_pipeline(scenes, describe_result, score=2.0):
    """Build a mock pipeline module with the functions describe_video imports."""
    m = MagicMock()
    m.detect_scenes.return_value = scenes
    m.split_long_scenes.return_value = scenes  # identity (no chunking needed in tests)
    m.cut_segment = MagicMock()               # no-op (no real ffmpeg call)
    m.describe_chunk.return_value = describe_result
    m.compute_chunk_score.return_value = score
    return m


def test_describe_video_happy_path(tmp_path):
    """Two scenes → both described, summary = first action, scenes list populated."""
    from photos.describe import describe_video

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    scenes = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.0}]
    desc = {
        "action": "a person hiking uphill",
        "shot": "wide shot",
        "energy": "medium",
        "setting": "mountain trail",
        "quality": "good",
    }
    fake = _fake_pipeline(scenes, desc, score=2.5)

    with patch.dict(sys.modules, {"pipeline": fake}):
        result = describe_video(MagicMock(), MagicMock(), video)

    assert result["caption"] == "a person hiking uphill"
    assert result["quality"] == "good"
    assert result["people"] == "none"
    assert result["scene"] is None
    assert len(result["scenes"]) == 2
    s0 = result["scenes"][0]
    assert s0["start_sec"] == 0.0
    assert s0["end_sec"] == 5.0
    assert s0["caption"] == "a person hiking uphill"
    assert s0["score"] == 2.5


def test_describe_video_zero_scenes(tmp_path):
    """No scenes detected → caption = '(no scenes detected)', empty scenes list."""
    from photos.describe import describe_video

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    fake = _fake_pipeline(scenes=[], describe_result={})

    with patch.dict(sys.modules, {"pipeline": fake}):
        result = describe_video(MagicMock(), MagicMock(), video)

    assert result["caption"] == "(no scenes detected)"
    assert result["scenes"] == []


def test_describe_video_failed_scene_is_skipped(tmp_path):
    """If describe_chunk raises on scene 0, scene 1 still succeeds."""
    from photos.describe import describe_video

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    scenes = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.0}]
    good_desc = {
        "action": "waves crashing on a beach",
        "shot": "wide",
        "energy": "high",
        "setting": "beach",
        "quality": "good",
    }

    fake = MagicMock()
    fake.detect_scenes.return_value = scenes
    fake.split_long_scenes.return_value = scenes
    fake.cut_segment = MagicMock()
    fake.describe_chunk.side_effect = [RuntimeError("GPU error"), good_desc]
    fake.compute_chunk_score.return_value = 3.0

    with patch.dict(sys.modules, {"pipeline": fake}):
        result = describe_video(MagicMock(), MagicMock(), video)

    assert len(result["scenes"]) == 1
    assert result["scenes"][0]["caption"] == "waves crashing on a beach"
    assert result["caption"] == "waves crashing on a beach"


def test_describe_video_all_scenes_fail(tmp_path):
    """If all scenes fail, caption is None (not '(no scenes detected)')."""
    from photos.describe import describe_video

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    scenes = [{"start": 0.0, "end": 5.0}]
    fake = MagicMock()
    fake.detect_scenes.return_value = scenes
    fake.split_long_scenes.return_value = scenes
    fake.cut_segment = MagicMock()
    fake.describe_chunk.side_effect = RuntimeError("always fails")
    fake.compute_chunk_score.return_value = 0.0

    with patch.dict(sys.modules, {"pipeline": fake}):
        result = describe_video(MagicMock(), MagicMock(), video)

    assert result["scenes"] == []
    assert result["caption"] is None


def test_describe_video_summary_is_first_nonempty_action(tmp_path):
    """Summary = first scene with a non-None, non-empty action."""
    from photos.describe import describe_video

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    scenes = [
        {"start": 0.0, "end": 5.0},
        {"start": 5.0, "end": 10.0},
        {"start": 10.0, "end": 15.0},
    ]

    def desc_side_effect(model, proc, seg_path, start, end):
        return {
            0.0: {"action": None,  "shot": None, "energy": None, "setting": None, "quality": "good"},
            5.0: {"action": "two people kayaking", "shot": "wide", "energy": "medium", "setting": "lake", "quality": "good"},
            10.0: {"action": "sunset over water", "shot": "aerial", "energy": "low", "setting": "lake", "quality": "good"},
        }[start]

    fake = MagicMock()
    fake.detect_scenes.return_value = scenes
    fake.split_long_scenes.return_value = scenes
    fake.cut_segment = MagicMock()
    fake.describe_chunk.side_effect = desc_side_effect
    fake.compute_chunk_score.return_value = 2.0

    with patch.dict(sys.modules, {"pipeline": fake}):
        result = describe_video(MagicMock(), MagicMock(), video)

    assert result["caption"] == "two people kayaking"
    assert len(result["scenes"]) == 3
    assert result["scenes"][0]["caption"] is None
    assert result["scenes"][1]["caption"] == "two people kayaking"
