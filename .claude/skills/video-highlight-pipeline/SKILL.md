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

State the theme at the top, then go through the chunks and select based on it. For each selected chunk explain in one line why it fits the theme. For rejected chunks, only explain if the reason is non-obvious. Ask the user to confirm or adjust before cutting.

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

State the theme at the top. Group your selection by source video. For each selected chunk explain in one line why it fits the theme. Ask the user to confirm before cutting.

### Step 4: Cut

```bash
python pipeline.py cut \
  --chunks-json <output_dir>/all_chunks.json \
  --selected 0,3,7,12 \
  --output <output_dir>/highlight.mp4
```

Chunks are cut from their original source videos and concatenated in the order specified by `--selected`. Speech-aware boundary snapping is on by default.
