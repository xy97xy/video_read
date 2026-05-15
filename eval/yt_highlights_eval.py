#!/usr/bin/env python3
"""Evaluate pipeline.py score against YouTube Highlights ground truth.

The YouTube Highlights dataset (Sun et al. ECCV 2014) provides ternary
labels per 5-second segment: 1=highlight, 0=normal, -1=non-highlight.
We compute mAP treating label=1 as positive.

Dataset: https://github.com/aliensunmin/DomainSpecificHighlight
Expected layout:
  <data-dir>/
    <domain>/
      <video_id>_label.txt   — one int per line (1/0/-1), one per 5s segment
    <domain>_<video_id>.json — chunks.json from pipeline.py describe

Usage:
  python eval/yt_highlights_eval.py \
    --data-dir /tmp/yt_highlights \
    --domain skiing \
    --profile /tmp/profile_skiing.json
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline import compute_chunk_score

SEGMENT_DURATION = 5.0  # YouTube Highlights uses 5s segments


def load_labels(label_path: str) -> list[int]:
    return [int(line.strip()) for line in open(label_path) if line.strip()]


def average_precision(scores: list[float], labels: list[int]) -> float:
    """Compute AP at full recall depth. Labels: 1=positive, others=negative."""
    paired = sorted(zip(scores, labels), key=lambda x: -x[0])
    n_pos = sum(1 for _, l in paired if l == 1)
    if n_pos == 0:
        return 0.0
    hits, ap = 0, 0.0
    for rank, (_, label) in enumerate(paired, 1):
        if label == 1:
            hits += 1
            ap += hits / rank
    return ap / n_pos


def score_chunks_for_video(chunks_json: str, profile: dict | None) -> list[tuple[float, float, float]]:
    """Return list of (start, end, score) for each chunk."""
    data = json.load(open(chunks_json))
    chunks = data["chunks"]
    result = []
    for ch in chunks:
        s = ch.get("score") if ch.get("score") is not None \
            else compute_chunk_score(ch, loudness_db=ch.get("loudness_db"), profile=profile)
        result.append((ch["start"], ch["end"], s))
    return result


def align_scores_to_segments(
    chunk_scores: list[tuple[float, float, float]],
    n_segments: int,
) -> list[float]:
    """Map chunk scores onto 5s segments by overlap."""
    seg_scores = [0.0] * n_segments
    for start, end, score in chunk_scores:
        seg_start = int(start / SEGMENT_DURATION)
        seg_end   = min(math.ceil(end / SEGMENT_DURATION), n_segments)
        for s in range(seg_start, seg_end):
            seg_scores[s] = max(seg_scores[s], score)
    return seg_scores


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--domain",   required=True, help="e.g. skiing, surfing, skating")
    ap.add_argument("--profile",  default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    label_dir = data_dir / args.domain
    if not label_dir.exists():
        print(f"Label dir not found: {label_dir}", file=sys.stderr)
        return 1

    profile = None
    if args.profile:
        profile = json.load(open(args.profile))

    aps = []
    print(f"{'Video':20} {'AP':>8} {'n_segs':>8} {'n_pos':>7}")
    print("-" * 48)
    for label_file in sorted(label_dir.glob("*_label.txt")):
        vid_id = label_file.stem.replace("_label", "")
        chunks_path = data_dir / f"{args.domain}_{vid_id}.json"
        if not chunks_path.exists():
            print(f"{vid_id:20} no chunks.json — skipping")
            continue

        labels = load_labels(str(label_file))
        chunk_scores = score_chunks_for_video(str(chunks_path), profile)
        seg_scores = align_scores_to_segments(chunk_scores, len(labels))
        ap = average_precision(seg_scores, labels)
        n_pos = sum(1 for l in labels if l == 1)
        aps.append(ap)
        print(f"{vid_id:20} {ap:8.3f} {len(labels):8} {n_pos:7}")

    if aps:
        print("-" * 48)
        print(f"{'mAP':20} {np.mean(aps):8.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
