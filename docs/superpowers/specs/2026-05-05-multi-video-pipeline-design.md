# Multi-Video Highlight Pipeline — Design Spec

**Date:** 2026-05-05  
**Status:** Approved

## Overview

Extend `pipeline.py` with two new modes (`batch`, `merge`) and update the `cut` mode to support highlights drawn from multiple source videos. The existing single-video flow is unchanged. The `video-highlight-pipeline` skill gains a multi-video branch.

## Goals

- Process a directory (or explicit list) of iPhone MOV files unattended
- Resume interrupted batch runs without re-describing already-processed videos
- Merge per-video chunk files into one combined selection pool
- Sort clips chronologically by real shot timestamp from file metadata
- Cut a final highlight reel that draws from multiple source files

## Data Model

### Per-video chunks file (`<output-dir>/<stem>_chunks.json`)

Same format as today, with one addition: `source_video` on every chunk and `shot_at` at the top level.

```json
{
  "video": "/abs/path/IMG_8435.MOV",
  "shot_at": "2025-11-16T20:19:29-08:00",
  "duration": 142.3,
  "chunks": [
    {
      "start": 0.0,
      "end": 10.0,
      "source_video": "/abs/path/IMG_8435.MOV",
      "description": "..."
    }
  ],
  "speech": [...]
}
```

### Merged file (`all_chunks.json`)

```json
{
  "sources": [
    { "video": "/abs/path/IMG_8435.MOV", "shot_at": "2025-11-16T20:19:29-08:00", "duration": 142.3 },
    { "video": "/abs/path/IMG_8436.MOV", "shot_at": "2025-11-16T20:21:42-08:00", "duration": 87.1 }
  ],
  "chunks": [
    { "index": 0, "source_video": "/abs/path/IMG_8435.MOV", "start": 0.0, "end": 10.0, "description": "..." },
    { "index": 1, "source_video": "/abs/path/IMG_8435.MOV", "start": 10.0, "end": 20.0, "description": "..." },
    { "index": 2, "source_video": "/abs/path/IMG_8436.MOV", "start": 0.0, "end": 8.5, "description": "..." }
  ],
  "speech": {
    "/abs/path/IMG_8435.MOV": [...],
    "/abs/path/IMG_8436.MOV": [...]
  }
}
```

Chunks are ordered by `shot_at` across sources, then by `start` within each source. `index` is the global selection key passed to `--selected`.

### Timestamp extraction

Priority order per source file:
1. `com.apple.quicktime.creationdate` (raw iPhone footage, includes local timezone)
2. `creation_time` from ffprobe format tags (UTC, present on iMovie exports)
3. Fall back to filename sort order if neither is available

## New Commands

### `pipeline.py batch`

```bash
python pipeline.py batch \
  --video-dir eval/sample_videos/ \       # scan dir for .MOV/.mp4
  --videos IMG_8467.MOV IMG_8468.MOV \    # optional: explicit list (additive with --video-dir)
  --output-dir eval/trip/ \
  --max-chunk-seconds 10 \
  --scene-threshold 0.5 \
  --whisper-model base \
  --no-transcribe                         # optional: skip whisper
```

**Behavior:**
- Collects all input videos, resolves absolute paths, sorts by `shot_at`
- For each video: if `<output-dir>/<stem>_chunks.json` already exists, skip (resume)
- Runs full describe pipeline: TransNetV2 → split → Qwen → Whisper
- Logs progress: `[2/5] IMG_8436.MOV — Nov 16 8:21 PM`
- On completion, prints per-video chunk count and prompts to run `merge`

### `pipeline.py merge`

```bash
python pipeline.py merge \
  --output-dir eval/trip/ \              # auto-discovers *_chunks.json (excludes all_chunks.json)
  --output eval/trip/all_chunks.json
```

**Behavior:**
- Discovers all `*_chunks.json` files in `--output-dir` (excludes `all_chunks.json` itself)
- Reads `shot_at` from each, sorts sources chronologically
- Assigns global `index` across all chunks
- Writes `all_chunks.json`
- Prints combined summary grouped by source video with timestamp header:

```
=== IMG_8435.MOV — Nov 16, 8:19 PM ===
[0]  0.0s-10.0s: [speech: "You see the first clip..."]
     Person grips rock face, close-up of hands...
[1] 10.0s-20.0s:
     Wide shot of boulder field...

=== IMG_8436.MOV — Nov 16, 8:21 PM ===
[2]  0.0s-8.5s:
     Climber traverses left on overhang...
```

### `pipeline.py cut` — updated

No interface change. Internal change: build one ffmpeg input per unique `source_video` and reference the correct input index per chunk in the filter graph.

Speech snapping reads from `all_chunks.json`'s per-source `speech` dict using each chunk's `source_video` key.

## Skill Updates (`SKILL.md`)

Add a **Multi-Video** branch at Step 0. The skill asks: single video or multiple?

**Multi-video flow:**

```
Step 0: Ask for video dir (or list), output dir, highlight goal
Step 1: Run pipeline.py batch  — unattended, resumable
Step 2: Run pipeline.py merge  — produces all_chunks.json
Step 3: Show combined summary grouped by date; ask user to select chunks
Step 4: Run pipeline.py cut on all_chunks.json
```

The existing single-video steps (1-3) are unchanged.

## Error Handling

| Scenario | Behavior |
|---|---|
| OOM mid-batch | Process exits; next `batch` run skips completed videos and resumes |
| Video has no metadata timestamp | Fall back to filename sort; log a warning |
| Selected index out of range | `cut` prints clear error with valid range |
| Source video moved/deleted at cut time | ffmpeg errors with path; user must fix path |

## Out of Scope

- True GPU parallelism (VRAM limit: one Qwen instance at a time on RTX 3070)
- Automatic highlight scoring (no manual selection step removed)
- Cross-video speech-aware snapping (each video's speech data is independent)
