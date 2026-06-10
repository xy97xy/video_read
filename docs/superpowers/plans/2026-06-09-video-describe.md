# Video Describe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `photos.py describe` to detect scenes in video files with TransNetV2, describe each scene with Qwen2.5-VL, write a summary caption to `photos.caption`, and save per-scene rows to a new `video_scenes` table.

**Architecture:** `_init_db` gets an idempotent `CREATE TABLE IF NOT EXISTS video_scenes` statement. `photos/describe.py` gets a new `describe_video(model, processor, path)` function that lazily imports `detect_scenes`, `split_long_scenes`, `describe_chunk`, `cut_segment`, and `compute_chunk_score` from `pipeline.py`. `cmd_describe` is refactored to split rows into photo/video lists, hoist Qwen model loading, then process photos (unchanged logic) followed by videos.

**Tech Stack:** Python, SQLite, TransNetV2 (scene detection), Qwen2.5-VL-7B-Instruct (description), ffmpeg (segment extraction), existing `pipeline.py` functions (no changes to that file), `unittest.mock.patch.dict(sys.modules)` for testing pipeline imports without GPU.

---

## File Map

| File | Action | What changes |
|------|--------|--------------|
| `photos.py` | Modify | `_init_db`: add `video_scenes` table; `cmd_describe`: hoist model load, add video routing loop |
| `photos/describe.py` | Modify | Add `import tempfile` at top; add `describe_video` function |
| `tests/test_describe.py` | Modify | Add `video_scenes` table migration tests |
| `tests/test_video_describe.py` | Create | Unit tests for `describe_video` (mocked pipeline) |
| `tests/test_describe_cmd.py` | Modify | Add video routing integration tests |

**`pipeline.py`: NO CHANGES.**

---

## Task 1: `video_scenes` DB table

**Files:**
- Modify: `photos.py:19-54` (`_init_db`)
- Modify: `tests/test_describe.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_describe.py`:

```python
def test_video_scenes_table_created(tmp_path):
    photos = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = photos._init_db(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "video_scenes" in tables


def test_video_scenes_table_columns(tmp_path):
    photos = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = photos._init_db(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(video_scenes)").fetchall()}
    conn.close()
    for col in ("id", "photo_id", "start_sec", "end_sec", "caption", "score", "created_at"):
        assert col in cols, f"Missing column: {col}"


def test_video_scenes_created_on_existing_db(tmp_path):
    """_init_db adds video_scenes to a DB created before this migration."""
    import sqlite3 as _sqlite3
    db_path = str(tmp_path / "photos.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE, taken_at INTEGER,
        lat REAL, lon REAL, place TEXT, cluster_id INTEGER, discarded INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

    photos = _load_photos_module()
    conn = photos._init_db(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "video_scenes" in tables
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py::test_video_scenes_table_created tests/test_describe.py::test_video_scenes_table_columns tests/test_describe.py::test_video_scenes_created_on_existing_db -v
```

Expected: FAIL — `video_scenes` table doesn't exist yet.

- [ ] **Step 3: Add `video_scenes` table to `_init_db`**

In `photos.py`, add the CREATE statement directly after the `photos` CREATE (before the `migrations` list). The full updated `_init_db` body:

```python
def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id           INTEGER PRIMARY KEY,
            path         TEXT UNIQUE,
            taken_at     INTEGER,
            lat          REAL,
            lon          REAL,
            place        TEXT,
            cluster_id   INTEGER,
            discarded    INTEGER DEFAULT 0,
            caption      TEXT,
            quality      TEXT,
            scene        TEXT,
            people       TEXT,
            described_at INTEGER,
            flagged      INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_scenes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id    INTEGER NOT NULL REFERENCES photos(id),
            start_sec   REAL NOT NULL,
            end_sec     REAL NOT NULL,
            caption     TEXT,
            score       REAL,
            created_at  INTEGER
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    migrations = [
        ("discarded",    "ALTER TABLE photos ADD COLUMN discarded    INTEGER DEFAULT 0"),
        ("caption",      "ALTER TABLE photos ADD COLUMN caption      TEXT"),
        ("quality",      "ALTER TABLE photos ADD COLUMN quality      TEXT"),
        ("scene",        "ALTER TABLE photos ADD COLUMN scene        TEXT"),
        ("people",       "ALTER TABLE photos ADD COLUMN people       TEXT"),
        ("described_at", "ALTER TABLE photos ADD COLUMN described_at INTEGER"),
        ("flagged",      "ALTER TABLE photos ADD COLUMN flagged      INTEGER DEFAULT 0"),
        ("discard_reason", "ALTER TABLE photos ADD COLUMN discard_reason TEXT"),
    ]
    for col, sql in migrations:
        if col not in cols:
            conn.execute(sql)
    conn.commit()
    return conn
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py::test_video_scenes_table_created tests/test_describe.py::test_video_scenes_table_columns tests/test_describe.py::test_video_scenes_created_on_existing_db -v
```

Expected: 3 PASS.

- [ ] **Step 5: Verify no regressions in existing describe tests**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd /scratch/video_read && git add photos.py tests/test_describe.py
git commit -m "feat: add video_scenes table to _init_db"
```

---

## Task 2: `describe_video` function

**Files:**
- Modify: `photos/describe.py` (add `import tempfile`, add `describe_video`)
- Create: `tests/test_video_describe.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_video_describe.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_video_describe.py -v
```

Expected: ImportError or AttributeError — `describe_video` doesn't exist yet.

- [ ] **Step 3: Add `import tempfile` to `photos/describe.py`**

The current imports are (lines 1–11):
```python
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
```

`tempfile` is not currently imported. Add it (alphabetically after `shutil`):

Replace:
```python
import shutil
from pathlib import Path
```

With:
```python
import shutil
import tempfile
from pathlib import Path
```

- [ ] **Step 4: Add `describe_video` to `photos/describe.py`**

Add the following function at the end of `photos/describe.py` (after `describe_photo`):

```python
def describe_video(model, processor, path: Path) -> dict:
    from pipeline import (
        detect_scenes, split_long_scenes, describe_chunk,
        cut_segment, compute_chunk_score,
    )

    try:
        scenes = detect_scenes(str(path))
    except Exception as e:
        log.warning(f"scene detection failed for {path.name}: {e}")
        return {"caption": None, "quality": "good", "scene": None, "people": "none", "scenes": []}

    chunks = split_long_scenes(scenes)

    if not chunks:
        return {"caption": "(no scenes detected)", "quality": "good", "scene": None, "people": "none", "scenes": []}

    seg_dir = tempfile.mkdtemp(prefix="video_describe_")
    described_scenes = []
    try:
        for i, chunk in enumerate(chunks):
            seg_path = os.path.join(seg_dir, f"seg_{i}.mp4")
            try:
                cut_segment(str(path), chunk["start"], chunk["end"], seg_path)
                result = describe_chunk(model, processor, seg_path, chunk["start"], chunk["end"])
                score = compute_chunk_score(result)
                described_scenes.append({
                    "start_sec": chunk["start"],
                    "end_sec": chunk["end"],
                    "caption": result.get("action"),
                    "score": score,
                })
            except Exception as e:
                log.warning(f"scene {i} of {path.name} failed: {e}")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)

    summary = next((s["caption"] for s in described_scenes if s.get("caption")), None)

    return {
        "caption": summary,
        "quality": "good",
        "scene": None,
        "people": "none",
        "scenes": described_scenes,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_video_describe.py -v
```

Expected: 5 PASS.

- [ ] **Step 6: Verify no regressions**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py tests/test_video_describe.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd /scratch/video_read && git add photos/describe.py tests/test_video_describe.py
git commit -m "feat: add describe_video to photos/describe.py"
```

---

## Task 3: Route videos in `cmd_describe`

**Files:**
- Modify: `photos.py` (`cmd_describe`)
- Modify: `tests/test_describe_cmd.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_describe_cmd.py`:

```python
def test_cmd_describe_video_writes_video_scenes(tmp_path):
    """Video rows are described and their scenes written to video_scenes table."""
    import sys
    from unittest.mock import patch

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


def test_cmd_describe_claude_provider_skips_videos(tmp_path):
    """--provider claude processes photos but routes videos through Qwen."""
    import sys
    from unittest.mock import patch, call

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
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py::test_cmd_describe_video_writes_video_scenes tests/test_describe_cmd.py::test_cmd_describe_claude_provider_skips_videos tests/test_describe_cmd.py::test_cmd_describe_corrupt_video_is_skipped -v
```

Expected: FAIL — `cmd_describe` currently filters out video files.

- [ ] **Step 3: Add `import logging` and `log` to `photos.py`**

`cmd_describe` now calls `log.warning(...)`, so `log` must exist at module level. At the top of `photos.py`, add `import logging` to the import block (alphabetically) and add `log = logging.getLogger(__name__)` after the imports.

The updated top of `photos.py`:

```python
#!/usr/bin/env python3
import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from photos.metadata import find_media_files, extract_metadata, reverse_geocode
from photos.cluster import build_clusters
from photos.dedup import find_exact_duplicates, find_phash_duplicates

log = logging.getLogger(__name__)
```

- [ ] **Step 4: Refactor `cmd_describe` in `photos.py`**

Replace the entire `cmd_describe` function (lines 369–441) with:

```python
def cmd_describe(args):
    import asyncio
    from photos.describe import load_qwen, describe_photo, ClaudeDescriber, describe_video

    conn = _init_db(args.db)
    provider = getattr(args, "provider", "qwen")
    benchmark = getattr(args, "benchmark", False)

    try:
        if benchmark:
            _cmd_benchmark(conn, args)
            return

        if getattr(args, "force", False):
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0 AND described_at IS NULL"
            ).fetchall()

        if not rows:
            print("✓ All photos already described. Use --force to re-describe.")
            return

        all_items = [{"id": r[0], "path": r[1]} for r in rows if Path(r[1]).exists()]
        photo_rows = [r for r in all_items if Path(r["path"]).suffix.lower() not in _VIDEO_EXTS]
        video_rows = [r for r in all_items if Path(r["path"]).suffix.lower() in _VIDEO_EXTS]

        need_qwen = (provider == "qwen" and photo_rows) or bool(video_rows)
        model = processor = None
        if need_qwen:
            n_qwen = (len(photo_rows) if provider == "qwen" else 0) + len(video_rows)
            print(f"Loading Qwen2.5-VL ({n_qwen} item(s) to describe)...")
            t0 = time.time()
            model, processor = load_qwen()
            print(f"Model loaded in {time.time() - t0:.0f}s")

        if provider == "claude" and photo_rows:
            describer = ClaudeDescriber(
                model=getattr(args, "model", "haiku"),
                workers=getattr(args, "workers", 5),
            )
            print(f"Describing {len(photo_rows)} photos with Claude ({describer.model}, {describer.workers} workers)...")
            results = asyncio.run(describer.describe_batch(photo_rows))
            n_described = 0
            for photo, result in zip(photo_rows, results):
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                n_described += 1
            conn.commit()
            print(f"\n✓ Described {n_described} photo(s) with Claude {describer.model}")
        elif photo_rows:
            n_described = 0
            bar = tqdm(photo_rows, unit="photo")
            for photo in bar:
                p = Path(photo["path"])
                bar.set_description(p.name[:40])
                result = describe_photo(model, processor, p)
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                conn.commit()
                n_described += 1
            print(f"\n✓ Described {n_described} photo(s)")

        if video_rows:
            n_videos = 0
            bar = tqdm(video_rows, unit="video")
            for video in bar:
                p = Path(video["path"])
                bar.set_description(p.name[:40])
                try:
                    result = describe_video(model, processor, p)
                except Exception as e:
                    log.warning(f"describe_video failed for {p.name}: {e}")
                    continue
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), video["id"]),
                )
                for scene in result["scenes"]:
                    conn.execute(
                        "INSERT INTO video_scenes (photo_id, start_sec, end_sec, caption, score, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (video["id"], scene["start_sec"], scene["end_sec"],
                         scene["caption"], scene["score"], int(time.time())),
                    )
                conn.commit()
                n_videos += 1
            print(f"\n✓ Described {n_videos} video(s)")
    finally:
        conn.close()
```

Also remove the now-redundant local `_VIDEO_EXTS` definition that was on line 395 of the old `cmd_describe` (the module-level `_VIDEO_EXTS` on line 258 already covers all video extensions).

Add `import logging` to the top of `photos.py` and add:

```python
log = logging.getLogger(__name__)
```

near the top (after the imports), since `cmd_describe` now uses `log.warning(...)`.

Wait — check if `logging` is already imported in `photos.py`. It is NOT currently imported in `photos.py`. Add it.

- [ ] **Step 5: Run the new tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py::test_cmd_describe_video_writes_video_scenes tests/test_describe_cmd.py::test_cmd_describe_claude_provider_skips_videos tests/test_describe_cmd.py::test_cmd_describe_corrupt_video_is_skipped -v
```

Expected: 3 PASS.

- [ ] **Step 6: Run the full describe test suite**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py tests/test_video_describe.py tests/test_describe_cmd.py -v
```

Expected: all pass. Note: `test_dedup_cmd.py` and `test_enhance_cmd.py` have pre-existing failures unrelated to this work — those are expected.

- [ ] **Step 7: Commit**

```bash
cd /scratch/video_read && git add photos.py tests/test_describe_cmd.py
git commit -m "feat: route video rows through describe_video in cmd_describe"
```

---

## Done

After Task 3, `python photos.py describe --db output/photos.db` will:
1. Detect video files among undescribed rows
2. Load Qwen once for both Qwen-provider photos and all videos
3. For each video: run TransNetV2 scene detection + per-scene Qwen description
4. Write summary caption to `photos.caption` and all scenes to `video_scenes`

The 594 pending videos in `output/photos.db` can now be described by running:
```bash
python photos.py describe --db output/photos.db
```
