import argparse
import importlib.util
import os, sys, shutil, sqlite3, subprocess
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

# Load photos.py as photos_main to avoid collision with the photos/ package
_spec = importlib.util.spec_from_file_location("photos_main", os.path.join(PROJ, "photos.py"))
_photos_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_photos_mod)
_init_db = _photos_mod._init_db
cmd_describe = _photos_mod.cmd_describe


def _cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(not _cuda_available(), reason="requires GPU")
def test_describe_real_photo(tmp_path):
    takeout = Path(PROJ) / "output" / "takeout" / "Takeout" / "Google Photos"
    jpgs = list(takeout.rglob("*.JPG")) + list(takeout.rglob("*.jpg"))
    if not jpgs:
        pytest.skip("No JPG files found in output/takeout")

    src = jpgs[0]
    dest = tmp_path / src.name
    shutil.copy2(src, dest)

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (1,?,1000)", (str(dest),))
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "photos.py", "describe", "--db", db],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    assert "Described 1" in result.stdout

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT caption, described_at FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[1] is not None, "described_at should be set"
    assert row[0] is not None, "caption should be non-null"


def test_describe_subcommand_in_help():
    result = subprocess.run(
        [sys.executable, "photos.py", "--help"],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0
    assert "describe" in result.stdout


def test_cmd_describe_exits_early_when_all_described(tmp_path):
    """When all photos already have described_at set, model is never loaded and DB is unchanged."""
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, described_at) VALUES (1, '/a.jpg', 1000, 9999)"
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(db=db, force=False)

    # No GPU mock needed — if the guard works, load_qwen is never imported/called.
    # If load_qwen were called, it would crash (no GPU in test env), causing the test to fail.
    # That crash IS the signal — this test would fail if the early-return guard broke.
    with patch("photos.describe.load_qwen", side_effect=RuntimeError("should not be called")):
        cmd_describe(args)  # must complete without raising

    # Verify DB row is unchanged
    conn = sqlite3.connect(db)
    val = conn.execute("SELECT described_at FROM photos WHERE id=1").fetchone()[0]
    conn.close()
    assert val == 9999, "described_at should be unchanged"


def test_cmd_describe_writes_result_to_db(tmp_path):
    """File exists → describe_photo called → DB row updated with caption and described_at."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(db=db, force=False)
    mock_model = MagicMock()
    mock_processor = MagicMock()
    fake_result = {"caption": "a sunny park", "quality": "good", "scene": "park", "people": "few"}

    with patch("photos.describe.load_qwen", return_value=(mock_model, mock_processor)):
        with patch("photos.describe.describe_photo", return_value=fake_result):
            cmd_describe(args)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT caption, quality, scene, people, described_at FROM photos WHERE id=1"
    ).fetchone()
    conn.close()

    assert row[0] == "a sunny park"
    assert row[1] == "good"
    assert row[2] == "park"
    assert row[3] == "few"
    assert row[4] is not None, "described_at should be set"


def test_cmd_describe_skips_missing_file(tmp_path):
    """Files that don't exist on disk are skipped — described_at stays NULL."""
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, '/nonexistent/ghost.jpg', 1000)"
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(db=db, force=False)
    mock_model = MagicMock()
    mock_processor = MagicMock()

    with patch("photos.describe.load_qwen", return_value=(mock_model, mock_processor)):
        with patch("photos.describe.describe_photo") as mock_describe:
            cmd_describe(args)

    mock_describe.assert_not_called()

    conn = sqlite3.connect(db)
    val = conn.execute("SELECT described_at FROM photos WHERE id=1").fetchone()[0]
    conn.close()
    assert val is None, "described_at should stay NULL for missing file"


def test_cmd_describe_claude_provider_writes_db(tmp_path):
    """--provider claude calls ClaudeDescriber.describe_batch and writes results to DB."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="claude", model="haiku", workers=2, benchmark=False
    )

    fake_result = {"caption": "a lake at dusk", "quality": "good", "scene": "lake", "people": "none"}

    async def fake_batch(photos):
        return [fake_result for _ in photos]

    mock_describer = MagicMock()
    mock_describer.describe_batch = fake_batch

    with patch("photos.describe.ClaudeDescriber", return_value=mock_describer):
        cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute(
        "SELECT caption, quality, scene, people, described_at FROM photos WHERE id=1"
    ).fetchone()
    conn.close()

    assert row[0] == "a lake at dusk"
    assert row[1] == "good"
    assert row[4] is not None


def test_cmd_describe_qwen_path_unchanged(tmp_path):
    """--provider qwen still calls load_qwen and describe_photo."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=5, benchmark=False
    )
    mock_model = MagicMock()
    mock_processor = MagicMock()
    fake_result = {"caption": "a forest path", "quality": "good", "scene": "forest", "people": "none"}

    with patch("photos.describe.load_qwen", return_value=(mock_model, mock_processor)):
        with patch("photos.describe.describe_photo", return_value=fake_result):
            cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute("SELECT caption FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "a forest path"


def test_cmd_describe_new_flags_in_help():
    result = subprocess.run(
        [sys.executable, "photos.py", "describe", "--help"],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--workers" in result.stdout
    assert "--benchmark" in result.stdout


def test_cmd_describe_video_writes_video_scenes(tmp_path):
    """Video rows are described and their scenes written to video_scenes table."""
    real_video = tmp_path / "clip.mp4"
    real_video.write_bytes(b"fake video data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_video),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=5, benchmark=False
    )

    fake_result = {
        "caption": "a sunset timelapse",
        "quality": "good",
        "scene": None,
        "people": "none",
        "scenes": [
            {"start_sec": 0.0, "end_sec": 5.0, "caption": "a sunset timelapse", "score": 2.5},
            {"start_sec": 5.0, "end_sec": 10.0, "caption": "clouds drifting", "score": 1.5},
        ],
    }

    with patch("photos.describe.load_qwen", return_value=(MagicMock(), MagicMock())):
        with patch("photos.describe.describe_video", return_value=fake_result):
            cmd_describe(args)

    conn = sqlite3.connect(db)
    photo_row = conn.execute(
        "SELECT caption, quality, described_at FROM photos WHERE id=1"
    ).fetchone()
    scene_rows = conn.execute(
        "SELECT start_sec, end_sec, caption, score FROM video_scenes WHERE photo_id=1"
    ).fetchall()
    conn.close()

    assert photo_row[0] == "a sunset timelapse"
    assert photo_row[1] == "good"
    assert photo_row[2] is not None
    assert len(scene_rows) == 2
    assert scene_rows[0] == (0.0, 5.0, "a sunset timelapse", 2.5)
    assert scene_rows[1] == (5.0, 10.0, "clouds drifting", 1.5)


def test_cmd_describe_claude_provider_routes_videos_through_qwen(tmp_path):
    """--provider claude processes photos but routes videos through Qwen."""
    real_photo = tmp_path / "photo.jpg"
    real_photo.write_bytes(b"fake jpeg")
    real_video = tmp_path / "clip.mp4"
    real_video.write_bytes(b"fake video")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)", (str(real_photo),))
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (2, ?, 1000)", (str(real_video),))
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="claude", model="haiku", workers=2, benchmark=False
    )

    claude_result = {"caption": "a park", "quality": "good", "scene": "park", "people": "none"}
    video_result = {
        "caption": "a walk in the park",
        "quality": "good", "scene": None, "people": "none",
        "scenes": [{"start_sec": 0.0, "end_sec": 5.0, "caption": "a walk in the park", "score": 2.0}],
    }

    async def fake_batch(photos):
        return [claude_result for _ in photos]

    mock_claude = MagicMock()
    mock_claude.describe_batch = fake_batch

    with patch("photos.describe.ClaudeDescriber", return_value=mock_claude):
        with patch("photos.describe.load_qwen", return_value=(MagicMock(), MagicMock())):
            with patch("photos.describe.describe_video", return_value=video_result):
                cmd_describe(args)

    conn = sqlite3.connect(db)
    photo_row = conn.execute("SELECT caption FROM photos WHERE id=1").fetchone()
    video_row = conn.execute("SELECT caption FROM photos WHERE id=2").fetchone()
    scene_count = conn.execute("SELECT COUNT(*) FROM video_scenes WHERE photo_id=2").fetchone()[0]
    conn.close()

    assert photo_row[0] == "a park"
    assert video_row[0] == "a walk in the park"
    assert scene_count == 1


def test_cmd_describe_corrupt_video_is_skipped(tmp_path):
    """If describe_video raises, the video row stays with described_at=NULL."""
    real_video = tmp_path / "bad.mp4"
    real_video.write_bytes(b"not a real video")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)", (str(real_video),))
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=5, benchmark=False
    )

    with patch("photos.describe.load_qwen", return_value=(MagicMock(), MagicMock())):
        with patch("photos.describe.describe_video", side_effect=RuntimeError("corrupt")):
            cmd_describe(args)

    conn = sqlite3.connect(db)
    val = conn.execute("SELECT described_at FROM photos WHERE id=1").fetchone()[0]
    conn.close()
    assert val is None, "described_at must stay NULL when video description fails"


def test_benchmark_does_not_write_db(tmp_path):
    """Benchmark mode reads DB but never writes described_at."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, described_at, caption, quality, scene, people) "
        "VALUES (1, ?, 1000, 9999, 'old caption', 'good', 'park', 'none')",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=2, benchmark=True
    )

    fake_result = {"caption": "new caption", "quality": "good", "scene": "beach", "people": "few"}

    async def fake_batch(photos):
        return [fake_result for _ in photos]

    mock_describer = MagicMock()
    mock_describer.describe_batch = fake_batch

    with patch("photos.describe.ClaudeDescriber", return_value=mock_describer):
        with patch("photos.describe.load_qwen", return_value=(MagicMock(), MagicMock())):
            with patch("photos.describe.describe_photo", return_value=fake_result):
                cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute("SELECT caption, described_at FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "old caption", "benchmark must not overwrite DB"
    assert row[1] == 9999, "described_at must not change"
