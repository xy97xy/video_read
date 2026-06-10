# Video Describe — Design Spec
Date: 2026-06-09

## Problem

594 video files are in `photos.db` with `described_at=NULL`. The existing `describe`
command skips them (`_VIDEO_EXTS` filter). Videos need scene-level descriptions saved
to the DB so they can be clustered and used for highlight generation.

## Solution

Extend `photos.py describe` to handle video files using the existing `pipeline.py`
scene detection + Qwen description logic. Summary caption goes into `photos.caption`
(keeps search/cluster working). Per-scene data goes into a new `video_scenes` table.

## CLI Interface

```bash
# Unchanged — now describes both photos and videos
python photos.py describe --db output/photos.db

# Force re-describe videos (same --force flag)
python photos.py describe --db output/photos.db --force

# Claude provider works for photos only (videos always use Qwen for now)
python photos.py describe --db output/photos.db --provider claude --model haiku --workers 5
```

No new flags. Videos are processed automatically when `describe` finds video rows with
`described_at IS NULL`.

## DB Schema

### New table: `video_scenes`

```sql
CREATE TABLE IF NOT EXISTS video_scenes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id    INTEGER NOT NULL REFERENCES photos(id),
    start_sec   REAL NOT NULL,
    end_sec     REAL NOT NULL,
    caption     TEXT,
    score       REAL,
    created_at  INTEGER
)
```

Created by `_init_db` alongside the existing `photos` table. Added as a migration
entry so existing DBs are upgraded automatically on first run.

### `photos` table (unchanged)

`photos.caption` receives the video summary (first non-empty scene caption).
`photos.described_at` is set to `int(time.time())` as usual.

## Architecture

### `photos/describe.py`

Add `describe_video(model, processor, path: Path) -> dict`:

```python
def describe_video(model, processor, path: Path) -> dict:
    # Returns:
    # {
    #   "caption": str | None,       # summary: first non-empty scene caption
    #   "quality": "good",           # videos always "good" (no blur/dark check)
    #   "scene": str | None,         # scene of first chunk
    #   "people": "none",            # not detected for video
    #   "scenes": [                  # per-scene data
    #     {"start_sec": float, "end_sec": float, "caption": str, "score": float}
    #   ]
    # }
```

Implementation:
1. Import `detect_scenes`, `split_long_scenes`, `describe_chunk`, `load_qwen` from `pipeline.py`
   (already loaded by the time `describe_video` is called)
2. Run `detect_scenes(str(path))` → list of `{start, end}` dicts
3. Run `split_long_scenes(scenes)` → chunks of ≤10s
4. For each chunk: extract temp segment with ffmpeg, call `describe_chunk(model, processor, seg_path, start, end)`
5. Summary caption = first chunk caption that is not None/empty
6. If 0 scenes detected: return `{"caption": "(no scenes detected)", "scenes": [], ...}`

### `photos.py`

**`_init_db`**: add `video_scenes` table creation + migration entry:

```python
("video_scenes_table", """
    CREATE TABLE IF NOT EXISTS video_scenes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id INTEGER NOT NULL REFERENCES photos(id),
        start_sec REAL NOT NULL,
        end_sec REAL NOT NULL,
        caption TEXT,
        score REAL,
        created_at INTEGER
    )
"""),
```

**`cmd_describe`**: split pending rows into photos and videos, process separately:

```python
_VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi'}

photo_rows = [r for r in photos if Path(r["path"]).suffix.lower() not in _VIDEO_EXTS]
video_rows = [r for r in photos if Path(r["path"]).suffix.lower() in _VIDEO_EXTS]

# Photos: existing Qwen/Claude path (unchanged)
# Videos: describe_video → write to photos + video_scenes
```

For each video:
1. Call `describe_video(model, processor, path)` 
2. Write summary fields to `photos` table (caption, quality, scene, people, described_at)
3. Write each scene to `video_scenes` table
4. `conn.commit()` after each video (same pattern as photos)

Video rows always use Qwen regardless of `--provider` flag. The `--provider claude`
path only applies to photo rows.

### `pipeline.py`

No changes. Functions are imported directly.

## Data Flow

```
describe --db output/photos.db
  → fetch rows WHERE discarded=0 AND described_at IS NULL
  → split into photo_rows + video_rows
  → photo_rows → existing Qwen/Claude path (unchanged)
  → video_rows:
      → load_qwen() (called once if any video rows present, regardless of --provider)
      → for each video:
          → detect_scenes + split_long_scenes
          → describe_chunk per scene
          → write summary → photos table
          → write scenes → video_scenes table
          → commit
```

## Error Handling

- **Corrupt/unsupported video**: log warning, skip, leave `described_at=NULL`
- **Qwen OOM on a scene**: skip that scene, save remaining scenes + partial summary
- **0 scenes detected**: caption = `"(no scenes detected)"`, empty `video_scenes`, mark `described_at`
- **ffmpeg not found**: fail fast with clear message

## Testing

- Unit test `describe_video` by mocking `detect_scenes` and `describe_chunk`:
  verify summary = first non-empty caption, scenes list populated correctly
- Unit test 0-scenes case: verify caption = `"(no scenes detected)"`, no scene rows
- Integration test `cmd_describe` with mixed photo+video rows:
  verify photo rows described via Qwen, video rows get `video_scenes` entries
- Verify `--provider claude` skips video rows (they still use Qwen)
- Existing photo describe tests unchanged

## Out of Scope

- Claude provider for videos (future — add `ClaudeVideoDescriber` separately)
- Video clustering (separate spec)
- Audio/speech transcription (pipeline.py has Whisper support; not used here)
- Thumbnail extraction (pipeline.py supports this; not needed for describe-only)
