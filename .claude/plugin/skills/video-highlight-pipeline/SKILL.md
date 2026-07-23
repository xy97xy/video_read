---
name: video-highlight-pipeline
description: Use when creating highlight reels from travel or action videos using the local AI pipeline in this repo. Covers running TransNetV2 scene detection, Qwen descriptions, selecting chunks, and cutting the final video.
---

# Video Highlight Pipeline

## Overview

Two-stage local pipeline: describe video chunks with AI, then cut selected chunks into a highlight reel. No external API needed for the core pipeline.

## Workflow

```
pipeline.py describe  →  chunks.json  →  (review in CLI)  →  pipeline.py cut  →  highlight.mp4
```

## Stage 1: Describe

```bash
source venv/bin/activate
python pipeline.py describe \
  --video path/to/video.mp4 \
  --output eval/run_name/chunks.json \
  --max-chunk-seconds 10        # 10s = good balance of accuracy vs speed
```

**What it does:**
- TransNetV2 detects scene boundaries (frame-accurate)
- Long scenes split into `--max-chunk-seconds` sub-chunks
- Qwen2.5-VL 7B (4-bit, ~5GB VRAM) describes each chunk
- Prints chunk summary at the end for review

**Key flags:**
- `--scene-threshold 0.5` — TransNetV2 cut confidence (lower = more sensitive)
- `--max-chunk-seconds 30` — for multi-scene footage with real cuts

## Stage 2: Cut

After reviewing the chunk summary, select indices and cut:

```bash
python pipeline.py cut \
  --chunks-json eval/run_name/chunks.json \
  --selected 0,2,5 \
  --output eval/run_name/highlight.mp4
```

Chunks are always concatenated in chronological order regardless of selection order.

## Single Continuous Shot vs Multi-Scene

| Footage type | TransNetV2 result | Strategy |
|---|---|---|
| Multi-scene (travel clips) | Many scenes | Describe per scene, pick best |
| Single continuous shot | 1 scene | Split into 10s chunks, pick best sub-sections |

## VRAM Notes

- Qwen 7B 4-bit uses ~5-6GB VRAM on RTX 3070
- **GPU cap: `load_qwen()` in `pipeline.py` sets `max_memory={0: "6144MiB"}` (75% of 8 GB) by default.** Do not remove this — running at 100% GPU caused the worker to crash.
- Always run with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` if OOM
- Previous model must be fully unloaded before reloading (restart Python process)

## Eval Outputs

Store runs under `eval/` with a descriptive name:
```
eval/
  run_name/
    chunks.json       # scene descriptions + timestamps
    highlight.mp4     # final cut
```

## Common Issues

| Problem | Fix |
|---|---|
| OOM on model load | Set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` or restart process |
| Chunks extend past video duration | Fixed — duration cap applied in `detect_scenes()` |
| All chunks look the same | Clip is a single continuous shot — use 10s chunks and pick most visually distinct |
| TransNetV2 finds no scenes | Normal for single continuous shots — falls back to time-based chunking |
