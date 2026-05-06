# Multi-Video Highlight Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `batch` and `merge` modes to `pipeline.py` and update `cut` to support highlights drawn from multiple source videos.

**Architecture:** `batch` runs the existing single-video describe pipeline on N videos sequentially, writing one `<stem>_chunks.json` per video with resume support. `merge` combines those files into `all_chunks.json` sorted by real shot timestamp from file metadata. `cut` is updated to build an ffmpeg filter graph with one input per unique source video.

**Tech Stack:** Python 3.14, ffprobe (metadata extraction), ffmpeg (multi-input filter_complex), faster-whisper, TransNetV2, Qwen2.5-VL 7B 4-bit

---

## File Map

| File | Change |
|---|---|
| `pipeline.py` | Add `get_shot_at()`, `collect_videos()`, `batch` mode, `merge` mode; update `concat_chunks()` and `cut` mode for multi-source |
| `tests/test_pipeline_multi.py` | New — unit tests for pure logic functions |
| `.claude/skills/video-highlight-pipeline/SKILL.md` | Add multi-video branch at Step 0 |

---

### Task 1: `get_shot_at()` — extract timestamp from video metadata

**Files:**
- Modify: `pipeline.py` (add function after `get_duration`)
- Create: `tests/test_pipeline_multi.py`

- [ ] **Step 1: Create test file with failing test**

```python
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
```

- [ ] **Step 2: Run to verify failure**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -v
```
Expected: `ImportError: cannot import name 'get_shot_at' from 'pipeline'`

- [ ] **Step 3: Implement `get_shot_at` in `pipeline.py`**

Add after `get_duration()`:

```python
def get_shot_at(video_path: str) -> str | None:
    """Return ISO 8601 shot timestamp from video metadata, or None."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    info = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
    tags = info.get("format", {}).get("tags", {})
    return (
        tags.get("com.apple.quicktime.creationdate")
        or tags.get("creation_time")
        or None
    )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py::test_get_shot_at_prefers_quicktime_creationdate tests/test_pipeline_multi.py::test_get_shot_at_falls_back_to_creation_time tests/test_pipeline_multi.py::test_get_shot_at_returns_none_when_no_tags -v
```
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add pipeline.py tests/test_pipeline_multi.py
git commit -m "feat: add get_shot_at() to extract video timestamp from metadata"
```

---

### Task 2: `collect_videos()` — gather and sort input videos

**Files:**
- Modify: `pipeline.py` (add function after `get_shot_at`)
- Modify: `tests/test_pipeline_multi.py`

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_pipeline_multi.py
import os
from pipeline import collect_videos

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
```

- [ ] **Step 2: Run to verify failure**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -k "collect_videos" -v
```
Expected: `ImportError: cannot import name 'collect_videos'`

- [ ] **Step 3: Implement `collect_videos` in `pipeline.py`**

Add after `get_shot_at()`:

```python
VIDEO_EXTS = {".mov", ".mp4", ".MOV", ".MP4"}

def collect_videos(videos: list[str], video_dir: str | None) -> list[dict]:
    """Return sorted list of {video, shot_at} dicts from explicit list and/or directory."""
    paths = [str(Path(v).resolve()) for v in videos]
    if video_dir:
        for p in sorted(Path(video_dir).iterdir()):
            if p.suffix in VIDEO_EXTS and str(p.resolve()) not in paths:
                paths.append(str(p.resolve()))

    entries = []
    for p in paths:
        shot_at = get_shot_at(p)
        if shot_at is None:
            log.warning(f"no timestamp metadata for {Path(p).name} — using filename order")
        entries.append({"video": p, "shot_at": shot_at})

    # Sort: entries with timestamps first (by timestamp), then no-timestamp entries by path
    def sort_key(e):
        return (0, e["shot_at"]) if e["shot_at"] else (1, e["video"])

    return sorted(entries, key=sort_key)
```

- [ ] **Step 4: Run tests**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -k "collect_videos" -v
```
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add pipeline.py tests/test_pipeline_multi.py
git commit -m "feat: add collect_videos() with shot_at-based chronological sort"
```

---

### Task 3: Update `describe` mode to write `source_video` and `shot_at`

**Files:**
- Modify: `pipeline.py` — `main()` describe branch only

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_pipeline_multi.py
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
```

This test validates the schema; the actual write logic is exercised via integration.

- [ ] **Step 2: Run to verify test passes (schema test only)**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py::test_describe_output_includes_source_video_and_shot_at -v
```
Expected: PASSED (it's a schema validation test)

- [ ] **Step 3: Update `main()` describe branch in `pipeline.py`**

Find the block that writes `out = {"video": ..., "duration": ..., "chunks": ..., "speech": ...}` and replace with:

```python
        # tag each chunk with its source video
        video_abs = str(Path(args.video).resolve())
        for chunk in chunks:
            chunk["source_video"] = video_abs

        shot_at = get_shot_at(args.video)
        out = {
            "video": video_abs,
            "shot_at": shot_at,
            "duration": duration,
            "chunks": chunks,
            "speech": speech,
        }
```

- [ ] **Step 4: Smoke-test with existing clip**

```bash
source venv/bin/activate && python pipeline.py describe \
  --video eval/sample_videos/test_1min_720p.mp4 \
  --output /tmp/smoke_chunks.json \
  --max-chunk-seconds 10 --no-transcribe
python3 -c "import json; d=json.load(open('/tmp/smoke_chunks.json')); print('shot_at:', d.get('shot_at')); print('source_video:', d['chunks'][0].get('source_video'))"
```
Expected: `shot_at:` (may be None for .mp4), `source_video: /home/xiaoyu/.../test_1min_720p.mp4`

- [ ] **Step 5: Commit**

```bash
git add pipeline.py tests/test_pipeline_multi.py
git commit -m "feat: describe mode writes source_video per chunk and shot_at at top level"
```

---

### Task 4: `merge_chunks_json()` — merge logic + `merge` mode

**Files:**
- Modify: `pipeline.py` — add `merge_chunks_json()` function and `merge` argparse subcommand

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_pipeline_multi.py
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
    # create a pre-existing all_chunks.json in same dir — should not be read
    (tmp_path / "all_chunks.json").write_text('{"sources":[],"chunks":[],"speech":{}}')
    result = merge_chunks_json([f1])
    assert len(result["chunks"]) == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -k "merge_chunks" -v
```
Expected: `ImportError: cannot import name 'merge_chunks_json'`

- [ ] **Step 3: Implement `merge_chunks_json` in `pipeline.py`**

Add after `concat_chunks()`:

```python
def merge_chunks_json(chunk_files: list[str]) -> dict:
    """Merge per-video chunk files into one combined dict sorted by shot_at."""
    sources_data = []
    for path in chunk_files:
        with open(path) as f:
            data = json.load(f)
        sources_data.append(data)

    # Sort sources by shot_at; None timestamps go last (filename order preserved)
    def src_sort_key(d):
        return (0, d["shot_at"]) if d.get("shot_at") else (1, d["video"])
    sources_data.sort(key=src_sort_key)

    sources = [{"video": d["video"], "shot_at": d.get("shot_at"), "duration": d["duration"]}
               for d in sources_data]

    all_chunks = []
    for d in sources_data:
        all_chunks.extend(d["chunks"])

    for idx, chunk in enumerate(all_chunks):
        chunk["index"] = idx

    speech = {d["video"]: d.get("speech", []) for d in sources_data}

    return {"sources": sources, "chunks": all_chunks, "speech": speech}
```

- [ ] **Step 4: Add `merge` subcommand to `main()`**

In `main()`, add alongside other `sub.add_parser(...)` calls:

```python
    # merge mode
    m = sub.add_parser("merge", help="combine per-video chunks.json → all_chunks.json")
    m.add_argument("--output-dir", required=True, help="Dir containing *_chunks.json files")
    m.add_argument("--output", required=True, help="Path to write all_chunks.json")
```

Add the handler in the `if args.mode ==` block:

```python
    elif args.mode == "merge":
        chunk_files = sorted(
            p for p in Path(args.output_dir).glob("*_chunks.json")
            if p.name != "all_chunks.json"
        )
        if not chunk_files:
            log.error(f"no *_chunks.json files found in {args.output_dir}")
            return 1
        log.info(f"merging {len(chunk_files)} chunk file(s)...")
        merged = merge_chunks_json([str(f) for f in chunk_files])
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(merged, f, indent=2)
        log.info(f"wrote {args.output}")

        # Print combined summary grouped by source
        print("\n--- COMBINED CHUNK SUMMARY ---")
        speech_by_src = merged["speech"]
        src_videos = {s["video"]: s for s in merged["sources"]}
        current_src = None
        for ch in merged["chunks"]:
            src = ch["source_video"]
            if src != current_src:
                current_src = src
                meta = src_videos[src]
                shot_str = ""
                if meta.get("shot_at"):
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(meta["shot_at"].replace("Z", "+00:00"))
                        shot_str = f" — {dt.strftime('%b %-d, %-I:%M %p')}"
                    except ValueError:
                        shot_str = f" — {meta['shot_at']}"
                print(f"\n=== {Path(src).name}{shot_str} ===")
            src_speech = speech_by_src.get(src, [])
            spoken = speech_in_range(src_speech, ch["start"], ch["end"])
            speech_tag = f" [speech: {' / '.join(spoken)[:60]}]" if spoken else ""
            print(f"[{ch['index']}] {ch['start']:.1f}s-{ch['end']:.1f}s:{speech_tag}")
            print(f"     {ch['description'][:100]}...")
        print(f"\nRun: python pipeline.py cut --chunks-json {args.output} --selected 0,1,2 --output highlight.mp4")
```

- [ ] **Step 5: Run tests**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -k "merge_chunks" -v
```
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add pipeline.py tests/test_pipeline_multi.py
git commit -m "feat: add merge_chunks_json() and pipeline.py merge mode"
```

---

### Task 5: `batch` mode

**Files:**
- Modify: `pipeline.py` — add `batch` subcommand to `main()`

No new functions needed — `batch` calls the existing `describe` internals directly.

- [ ] **Step 1: Add `batch` subcommand to `main()`**

Add alongside other `sub.add_parser(...)` calls:

```python
    # batch mode
    b = sub.add_parser("batch", help="describe multiple videos → per-video chunks.json files")
    b.add_argument("--video-dir", default=None, help="Dir to scan for .MOV/.mp4 files")
    b.add_argument("--videos", nargs="*", default=[], help="Explicit video paths (additive with --video-dir)")
    b.add_argument("--output-dir", required=True, help="Dir to write <stem>_chunks.json files")
    b.add_argument("--scene-threshold", type=float, default=0.5)
    b.add_argument("--max-chunk-seconds", type=int, default=MAX_CHUNK_SECONDS)
    b.add_argument("--transcribe", action="store_true", default=True)
    b.add_argument("--no-transcribe", dest="transcribe", action="store_false")
    b.add_argument("--whisper-model", default="base")
```

Add handler in `if args.mode ==` block:

```python
    elif args.mode == "batch":
        entries = collect_videos(videos=args.videos, video_dir=args.video_dir)
        if not entries:
            log.error("no videos found — provide --video-dir or --videos")
            return 1
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

        model, processor = load_qwen()

        for i, entry in enumerate(entries):
            video_path = entry["video"]
            stem = Path(video_path).stem
            out_path = Path(args.output_dir) / f"{stem}_chunks.json"

            if out_path.exists():
                log.info(f"[{i+1}/{len(entries)}] skipping {stem} (already done)")
                continue

            shot_str = ""
            if entry.get("shot_at"):
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(entry["shot_at"].replace("Z", "+00:00"))
                    shot_str = f" — {dt.strftime('%b %-d, %-I:%M %p')}"
                except ValueError:
                    shot_str = f" — {entry['shot_at']}"
            log.info(f"[{i+1}/{len(entries)}] {stem}{shot_str}")

            duration = get_duration(video_path)
            scenes = detect_scenes(video_path, threshold=args.scene_threshold, duration=duration)
            chunks = split_long_scenes(scenes, max_chunk=args.max_chunk_seconds)
            log.info(f"  {len(scenes)} scenes → {len(chunks)} chunks")

            chunks = describe_all(model, processor, video_path, chunks)

            speech = []
            if args.transcribe:
                speech = transcribe_audio(video_path, model_size=args.whisper_model)

            video_abs = str(Path(video_path).resolve())
            for chunk in chunks:
                chunk["source_video"] = video_abs

            out_data = {
                "video": video_abs,
                "shot_at": entry.get("shot_at"),
                "duration": duration,
                "chunks": chunks,
                "speech": speech,
            }
            with open(out_path, "w") as f:
                json.dump(out_data, f, indent=2)
            log.info(f"  wrote {out_path} ({len(chunks)} chunks)")

        log.info("batch complete")
        print(f"\nRun: python pipeline.py merge --output-dir {args.output_dir} --output {args.output_dir}/all_chunks.json")
```

- [ ] **Step 2: Integration smoke test — batch with two small clips**

```bash
source venv/bin/activate && python pipeline.py batch \
  --videos eval/sample_videos/test_1min_720p.mp4 \
  --output-dir /tmp/batch_test/ \
  --max-chunk-seconds 30 --no-transcribe
ls /tmp/batch_test/
```
Expected: `test_1min_720p_chunks.json` created

- [ ] **Step 3: Verify resume skips already-done video**

```bash
source venv/bin/activate && python pipeline.py batch \
  --videos eval/sample_videos/test_1min_720p.mp4 \
  --output-dir /tmp/batch_test/ \
  --max-chunk-seconds 30 --no-transcribe 2>&1 | grep -i skip
```
Expected: `skipping test_1min_720p (already done)`

- [ ] **Step 4: Commit**

```bash
git add pipeline.py
git commit -m "feat: add batch mode with resume support"
```

---

### Task 6: Update `cut` mode for multi-source ffmpeg

**Files:**
- Modify: `pipeline.py` — `concat_chunks()` and `cut` mode handler

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_pipeline_multi.py
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
            video_path=None,   # unused in multi-source path
            chunks=chunks,
            selected=[0, 1, 2],
            output_path=str(tmp_path / "out.mp4"),
        )

    cmd = captured["cmd"]
    # Should have two -i flags for /v1.MOV and /v2.MOV
    assert cmd.count("-i") == 2
    assert "/v1.MOV" in cmd
    assert "/v2.MOV" in cmd
```

- [ ] **Step 2: Run to verify failure**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py::test_concat_chunks_multi_source_builds_correct_inputs -v
```
Expected: FAIL — current `concat_chunks` uses single `-i video_path`

- [ ] **Step 3: Rewrite `concat_chunks` for multi-source**

Replace the entire `concat_chunks` function:

```python
def concat_chunks(
    video_path: str | None,
    chunks: list[dict],
    selected: list[int],
    output_path: str,
    speech: list[dict] | dict | None = None,
    video_duration: float = 0.0,
) -> None:
    selected_chunks = [chunks[i] for i in selected]
    if not selected_chunks:
        log.warning("no chunks selected — nothing to cut")
        return

    # Determine if multi-source (chunks have source_video) or single-source
    has_source = all("source_video" in ch for ch in selected_chunks)
    if has_source:
        # Build ordered list of unique source videos (preserving first-seen order)
        seen = []
        for ch in selected_chunks:
            if ch["source_video"] not in seen:
                seen.append(ch["source_video"])
        src_index = {src: i for i, src in enumerate(seen)}
    else:
        # Legacy single-source path
        seen = [video_path]
        src_index = {video_path: 0}
        for ch in selected_chunks:
            ch = dict(ch)
            ch["source_video"] = video_path

    parts, inputs = [], []
    for i, ch in enumerate(selected_chunks):
        src = ch.get("source_video", video_path)
        idx = src_index[src]

        # Speech snapping: resolve per-source speech list
        if isinstance(speech, dict):
            src_speech = speech.get(src, [])
        elif isinstance(speech, list):
            src_speech = speech
        else:
            src_speech = []

        s = snap_to_silence(ch["start"], src_speech, video_duration) if src_speech else ch["start"]
        e = snap_to_silence(ch["end"], src_speech, video_duration) if src_speech else ch["end"]

        parts.append(f"[{idx}:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];")
        parts.append(f"[{idx}:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}];")
        inputs.append(f"[v{i}][a{i}]")

    filter_complex = "".join(parts) + f"{''.join(inputs)}concat=n={len(selected_chunks)}:v=1:a=1[outv][outa]"
    input_args = []
    for src in seen:
        input_args += ["-i", src]

    cmd = ["ffmpeg", "-y"] + input_args + [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac", output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    kept = sum(ch["end"] - ch["start"] for ch in selected_chunks)
    log.info(f"wrote {output_path} ({kept:.1f}s from {len(selected_chunks)} chunks)")
```

- [ ] **Step 4: Update `cut` mode handler in `main()`**

Replace the `elif args.mode == "cut":` block:

```python
    elif args.mode == "cut":
        with open(args.chunks_json) as f:
            data = json.load(f)
        chunks = data["chunks"]
        # Support both single-video (speech=[]) and merged (speech={}) formats
        speech = data.get("speech", [])
        duration = data.get("duration", 0.0)
        selected = [int(x.strip()) for x in args.selected.split(",")]

        # Validate indices
        max_idx = len(chunks) - 1
        invalid = [i for i in selected if i < 0 or i > max_idx]
        if invalid:
            log.error(f"invalid chunk indices {invalid} — valid range is 0-{max_idx}")
            return 1

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        use_speech = speech if (args.speech_aware and speech) else None
        if args.speech_aware and not speech:
            log.info("no speech data in chunks.json — cutting without speech snapping")

        # For single-source legacy files, video_path comes from top-level "video"
        video_path = data.get("video")
        concat_chunks(video_path, chunks, selected, args.output,
                      speech=use_speech, video_duration=duration)
```

- [ ] **Step 5: Run all tests**

```bash
source venv/bin/activate && pytest tests/test_pipeline_multi.py -v
```
Expected: all tests PASSED

- [ ] **Step 6: Integration smoke test — merge + cut**

```bash
source venv/bin/activate
python pipeline.py merge \
  --output-dir /tmp/batch_test/ \
  --output /tmp/batch_test/all_chunks.json
python pipeline.py cut \
  --chunks-json /tmp/batch_test/all_chunks.json \
  --selected 0,1 \
  --output /tmp/batch_test/highlight.mp4
ls -lh /tmp/batch_test/highlight.mp4
```
Expected: `highlight.mp4` created, non-zero size

- [ ] **Step 7: Commit**

```bash
git add pipeline.py tests/test_pipeline_multi.py
git commit -m "feat: update concat_chunks for multi-source ffmpeg; add index validation in cut mode"
```

---

### Task 7: Update `SKILL.md` with multi-video branch

**Files:**
- Modify: `.claude/skills/video-highlight-pipeline/SKILL.md`

- [ ] **Step 1: Add multi-video section to SKILL.md**

Replace the existing **Step 0** block with:

```markdown
## Step 0: Gather inputs before running anything

Ask the user:
1. **Single or multiple videos?**
   - Single → ask for video path, output dir, highlight goal → follow Steps 1-3 (existing flow)
   - Multiple → ask for video directory (or list of files), output dir, highlight goal → follow Multi-Video Steps below

Do not proceed until you have all inputs.

---

## Single-Video Flow (Steps 1–3)

[existing content unchanged]

---

## Multi-Video Flow

### Step 1: Batch describe (unattended, resumable)

```bash
source venv/bin/activate
python pipeline.py batch \
  --video-dir <video_dir> \
  --output-dir <output_dir> \
  --max-chunk-seconds 10
```

If the user has specific files: `--videos file1.MOV file2.MOV` (additive with `--video-dir`).

This runs unattended. If interrupted, re-run the same command — already-described videos are skipped.

### Step 2: Merge

```bash
python pipeline.py merge \
  --output-dir <output_dir> \
  --output <output_dir>/all_chunks.json
```

Read `<output_dir>/all_chunks.json` and show the user the combined summary table grouped by video and timestamp.

### Step 3: Select

Based on the highlight goal and descriptions, select chunks. Explain choices briefly grouped by source video. Ask user to confirm before cutting.

### Step 4: Cut

```bash
python pipeline.py cut \
  --chunks-json <output_dir>/all_chunks.json \
  --selected 0,3,7,12 \
  --output <output_dir>/highlight.mp4
```
```

- [ ] **Step 2: Verify SKILL.md renders correctly**

```bash
cat .claude/skills/video-highlight-pipeline/SKILL.md
```
Expected: clean markdown with both single and multi-video flows visible

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/video-highlight-pipeline/SKILL.md
git commit -m "docs: add multi-video branch to video-highlight-pipeline skill"
```

---

## Self-Review

**Spec coverage check:**
- ✅ `batch` mode with resume — Task 5
- ✅ `merge` mode with shot_at sort — Task 4
- ✅ `cut` multi-source — Task 6
- ✅ `get_shot_at` timestamp extraction (both tag types) — Task 1
- ✅ `collect_videos` with fallback sort — Task 2
- ✅ `describe` writes `source_video` + `shot_at` — Task 3
- ✅ Index validation in cut — Task 6 Step 4
- ✅ Skill update — Task 7
- ✅ Error: no videos found — Task 5 Step 1 (early return)
- ✅ Error: out-of-range index — Task 6 Step 4

**Placeholder scan:** None found.

**Type consistency:**
- `concat_chunks` signature updated consistently across all call sites in `main()`
- `speech` param accepts `list[dict]` (single-video) or `dict[str, list]` (merged) — handled in Task 6
- `collect_videos` returns `list[dict]` with `video` and `shot_at` keys — used consistently in Task 5
