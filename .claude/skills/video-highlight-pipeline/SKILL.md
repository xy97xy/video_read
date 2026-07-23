---
name: video-highlight-pipeline
description: Use when creating highlight reels from travel or action videos using the local AI pipeline in this repo. Covers single-video (describe → select → cut) and multi-video (batch → merge → select → cut) workflows.
---

# Video Highlight Pipeline

## Overview

Local pipeline: describe video chunks with AI + transcribe speech, then cut selected chunks into a highlight reel with speech-aware boundaries. Supports single videos and multi-video batch processing.

## Prerequisites: venv setup

Before running anything, check that the venv exists:

```bash
ls venv/bin/activate
```

If missing, set it up first:

```bash
bash setup_venv.sh
```

This installs all dependencies (Qwen, TransNetV2, faster-whisper, ffmpeg bindings). Takes ~5 minutes on first run.

---

## Step 0: Gather inputs before running anything

Ask the user:
1. **Single or multiple videos?**
   - Single video → ask for video path, output dir → follow **Single-Video Flow** below
   - Multiple videos / directory → ask for video directory (or list of files), output dir → follow **Multi-Video Flow** below
2. **Theme** — what is this highlight reel about? Ask explicitly. Examples:
   - "Best moments and variety of scenes"
   - "Energetic action moments only, under 90 seconds"
   - "Scenic and cultural moments, no talking-head clips"
   - "Food and nightlife"

Write the theme down. Every selection decision in Step 2/3 must reference it — explain per chunk why it fits or doesn't fit the theme. Do not proceed until you have all inputs.

---

## Single-Video Flow

### Step 0: Get context + generate profile

Ask the user:
> "Give me one sentence describing this footage — e.g. 'skiing powder day at Chamonix' or 'Kilimanjaro summit attempt'."

From their answer, write `<output_dir>/profile.json` with domain-calibrated weights and a selection prompt. Use the scoring weight table below as guidance:

| Domain | energy_weights (high/medium/low) | quality_penalty | loudness_weight | shot_type_bonus |
|---|---|---|---|---|
| Mountaineering | 3.0 / 2.5 / 1.5 | −1.0 | 0.3 | aerial +1.0, wide shot +0.5 |
| Skiing | 4.0 / 1.5 / 0.5 | −2.0 | 1.5 | aerial +1.0 |
| Rock climbing | 3.0 / 2.5 / 1.0 | −1.5 | 0.3 | close-up +0.5 |
| Travel / general | 3.0 / 2.0 / 1.0 | −1.5 | 1.0 | (none) |

Write the profile JSON directly — no pipeline.py command needed. Example:

```json
{
  "context": "skiing powder day at Chamonix",
  "scoring": {
    "energy_weights": {"high": 4.0, "medium": 1.5, "low": 0.5},
    "quality_penalty": -2.0,
    "loudness_weight": 1.5,
    "shot_type_bonus": {"aerial": 1.0}
  },
  "selection_prompt": "You are selecting highlights for a skiing reel at Chamonix. Prefer: fast downhill runs, powder shots, aerial views, steep terrain. Avoid: gondola/lift rides, gear preparation, flat beginner slopes, consecutive similar runs on the same piste."
}
```

If the context is ambiguous (e.g. "mountain trip"), ask one clarifying question: "Was this more technical climbing/mountaineering, or recreational hiking with great views?"

### Step 1: Describe

```bash
source venv/bin/activate
python pipeline.py describe \
  --video <video_path> \
  --output <output_dir>/chunks.json \
  --max-chunk-seconds 10
```

- TransNetV2 detects scene boundaries (frame-accurate, ~1s to run)
- Each scene/chunk described by Qwen2.5-VL 7B (~12s per chunk)
- faster-whisper transcribes audio with word-level timestamps (~2s for 1min clip)
- Single continuous shot → falls back to 10s time-based chunks automatically

**Key flags:**
- `--scene-threshold 0.5` — lower = more sensitive to cuts
- `--max-chunk-seconds 30` — use for multi-scene footage with real cuts
- `--no-transcribe` — skip audio transcription
- `--whisper-model base` — whisper model size (tiny/base/small/medium/large)

After it finishes, tell the user to open `<output_dir>/thumbs.html` in a browser — it shows every chunk as a thumbnail grid with index, time, speech, and structured metadata. Then read `<output_dir>/chunks.json` and show the summary table:

| # | Time | Speech | Action | Setting | Shot | Energy | Quality |
|---|------|--------|--------|---------|------|--------|---------|
| 0 | 0s-10s | "..." | what is happening | beach at sunset | wide shot | high | good |
| 1 | 10s-20s | (none) | ... | indoor market | close-up | low | shaky |

Qwen outputs structured JSON with five fields: `action`, `shot`, `energy`, `setting`, `quality`. Flag any chunk where quality is not "good" — those are candidates to skip. If a chunk was described before this feature existed, it will only have a `description` field — display that in the Action column and leave the rest empty.

### Step 2: Select

Load `<output_dir>/profile.json`. Use the profile's `selection_prompt` as your selection guide when reading through chunks. For each selected chunk explain in one line why it fits — reference the profile criteria explicitly (e.g. "aerial shot of steep couloir — matches profile preference for dramatic terrain"). Ask the user to confirm or adjust before cutting.

### Step 3: Cut

```bash
python pipeline.py cut \
  --chunks-json <output_dir>/chunks.json \
  --selected 0,2,5 \
  --output <output_dir>/highlight.mp4
```

Chunks are always concatenated in chronological order.

Speech-aware cutting is on by default: if a chunk boundary falls mid-sentence, the cut point automatically snaps to the nearest sentence gap (±0.3s margin). Use `--no-speech-aware` to disable.

## Single Continuous Shot vs Multi-Scene

| Footage type | TransNetV2 result | Strategy |
|---|---|---|
| Multi-scene (travel clips) | Many scenes | Describe per scene, pick best |
| Single continuous shot | 1 scene | Split into 10s chunks, pick most visually distinct |

## VRAM Notes

- Qwen 7B 4-bit uses ~5-6GB VRAM — fits on RTX 3070
- **GPU cap: `load_qwen()` in `pipeline.py` sets `max_memory={0: "6144MiB"}` (75% of 8 GB) by default.** Do not remove this — running at 100% GPU caused the worker to crash.
- `expandable_segments` is already set inside `pipeline.py`
- If OOM: restart the Python process to fully clear VRAM
- faster-whisper uses ~500MB on GPU (CUDA 12 cublas path is baked into `venv/bin/activate`)

## Common Issues

| Problem | Fix |
|---|---|
| OOM on model load | Restart process — previous run may still hold VRAM |
| Chunks extend past video duration | Fixed in `detect_scenes()` via duration cap |
| All chunks look the same | Single continuous shot — pick based on camera angle, subject, or lighting variety |
| TransNetV2 finds no scenes | Normal for uncut footage — chunking falls back to time-based |
| Whisper CUDA error (libcublas.so.12) | `source venv/bin/activate` — the activate script sets LD_LIBRARY_PATH; whisper also auto-falls back to CPU |

---

## Multi-Video Flow

### Step 0: Get context + generate profile

Ask the user:
> "Give me one sentence describing this footage — e.g. 'skiing powder day at Chamonix' or 'Kilimanjaro summit attempt'."

From their answer, write `<output_dir>/profile.json` with domain-calibrated weights and a selection prompt. Use the scoring weight table below as guidance:

| Domain | energy_weights (high/medium/low) | quality_penalty | loudness_weight | shot_type_bonus |
|---|---|---|---|---|
| Mountaineering | 3.0 / 2.5 / 1.5 | −1.0 | 0.3 | aerial +1.0, wide shot +0.5 |
| Skiing | 4.0 / 1.5 / 0.5 | −2.0 | 1.5 | aerial +1.0 |
| Rock climbing | 3.0 / 2.5 / 1.0 | −1.5 | 0.3 | close-up +0.5 |
| Travel / general | 3.0 / 2.0 / 1.0 | −1.5 | 1.0 | (none) |

Write the profile JSON directly — no pipeline.py command needed. Example:

```json
{
  "context": "skiing powder day at Chamonix",
  "scoring": {
    "energy_weights": {"high": 4.0, "medium": 1.5, "low": 0.5},
    "quality_penalty": -2.0,
    "loudness_weight": 1.5,
    "shot_type_bonus": {"aerial": 1.0}
  },
  "selection_prompt": "You are selecting highlights for a skiing reel at Chamonix. Prefer: fast downhill runs, powder shots, aerial views, steep terrain. Avoid: gondola/lift rides, gear preparation, flat beginner slopes, consecutive similar runs on the same piste."
}
```

If the context is ambiguous (e.g. "mountain trip"), ask one clarifying question: "Was this more technical climbing/mountaineering, or recreational hiking with great views?"

### Step 1: Batch describe (unattended, resumable)

```bash
source venv/bin/activate
python pipeline.py batch \
  --video-dir <video_dir> \
  --output-dir <output_dir> \
  --max-chunk-seconds 10
```

If the user has specific files rather than a directory: add `--videos file1.MOV file2.MOV` (additive with `--video-dir`).

This runs unattended — Qwen processes each video sequentially (~12s/chunk). If interrupted, re-run the same command — already-described videos are skipped automatically. iPhone MOV files are sorted by real shot timestamp from metadata.

### Step 2: Merge

```bash
python pipeline.py merge \
  --output-dir <output_dir> \
  --output <output_dir>/all_chunks.json
```

Tell the user to open `<output_dir>/thumbs.html` in a browser — it shows all chunks across all videos as a thumbnail grid with global indices, grouped by source. Then show the combined summary table (grouped by source video):

| # | Time | Speech | Action | Setting | Shot | Energy | Quality |
|---|------|--------|--------|---------|------|--------|---------|
| 0 | ... | ... | ... | ... | ... | ... | ... |

### Step 3: Select

Load `<output_dir>/profile.json`. Use the profile's `selection_prompt` as your selection guide when reading through chunks. Group your selection by source video. For each selected chunk explain in one line why it fits — reference the profile criteria explicitly (e.g. "aerial shot of steep couloir — matches profile preference for dramatic terrain"). Ask the user to confirm or adjust before cutting.

### Step 4: Cut

```bash
python pipeline.py cut \
  --chunks-json <output_dir>/all_chunks.json \
  --selected 0,3,7,12 \
  --output <output_dir>/highlight.mp4
```

Chunks are cut from their original source videos and concatenated in the order specified by `--selected`. Speech-aware boundary snapping is on by default.
