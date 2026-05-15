#!/usr/bin/env python3
"""Evaluate pipeline.py score against TVSum ground-truth annotations.

Usage:
  python eval/tvsum_eval.py
  python eval/tvsum_eval.py --chunks-dir /tmp/tvsum/chunks \
                             --mat /tmp/tvsum/ydata-tvsum50.mat \
                             --profile /tmp/tvsum/profile_generic.json

Prints Kendall's tau and Spearman's rho per video and average.
Requires: h5py, scipy (both in venv).
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import h5py
import numpy as np
from scipy.stats import kendalltau, spearmanr
import sys, os
sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline import compute_chunk_score

DEFAULT_MAT   = "/tmp/tvsum/ydata-tvsum50.mat"
DEFAULT_CHUNKS = "/tmp/tvsum/chunks"


def load_tvsum(mat_path: str) -> dict[str, np.ndarray]:
    """Return {youtube_id: gt_score_array} from TVSum .mat file."""
    f = h5py.File(mat_path, "r")
    tv = f["tvsum50"]
    out = {}
    for i in range(50):
        vid = "".join(chr(c) for c in f[tv["video"][i, 0]][:].flatten())
        gt  = f[tv["gt_score"][i, 0]][:].flatten()
        out[vid] = gt
    return out


def score_chunks(chunks_json: str, profile: dict | None) -> list[float]:
    data = json.load(open(chunks_json))
    chunks = data["chunks"]
    for i, ch in enumerate(chunks):
        if "index" not in ch:
            ch["index"] = i
    return [
        ch.get("score") if ch.get("score") is not None
        else compute_chunk_score(ch, loudness_db=ch.get("loudness_db"), profile=profile)
        for ch in chunks
    ]


def gt_chunk_means(chunks_json: str, gt_frames: np.ndarray) -> list[float]:
    data = json.load(open(chunks_json))
    chunks = data["chunks"]
    duration = data["duration"]
    fps = len(gt_frames) / duration
    means = []
    for ch in chunks:
        sf = int(ch["start"] * fps)
        ef = min(int(ch["end"] * fps), len(gt_frames))
        means.append(float(np.mean(gt_frames[max(0, sf): max(sf + 1, ef)])))
    return means


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks-dir", default=DEFAULT_CHUNKS)
    ap.add_argument("--mat",        default=DEFAULT_MAT)
    ap.add_argument("--profile",    default=None, help="Path to profile.json")
    args = ap.parse_args()

    gt_map = load_tvsum(args.mat)

    profile = None
    if args.profile:
        profile = json.load(open(args.profile))

    chunk_files = sorted(Path(args.chunks_dir).glob("*.json"))
    if not chunk_files:
        print(f"No *.json files found in {args.chunks_dir}", file=sys.stderr)
        return 1

    results = []
    print(f"{'Video':15} {'τ':>8} {'ρ':>8} {'chunks':>7}")
    print("-" * 42)
    for cf in chunk_files:
        vid = cf.stem
        if vid not in gt_map:
            print(f"{vid:15} not in TVSum annotations — skipping")
            continue
        our = score_chunks(str(cf), profile)
        gt  = gt_chunk_means(str(cf), gt_map[vid])
        tau, _ = kendalltau(our, gt)
        rho, _ = spearmanr(our, gt)
        print(f"{vid:15} {tau:8.3f} {rho:8.3f} {len(our):7}")
        results.append((tau, rho))

    if results:
        avg_tau = np.mean([r[0] for r in results])
        avg_rho = np.mean([r[1] for r in results])
        print("-" * 42)
        print(f"{'AVERAGE':15} {avg_tau:8.3f} {avg_rho:8.3f}")
        print(f"\nBaseline (no profile): τ=0.124  ρ=0.150")
    return 0


if __name__ == "__main__":
    sys.exit(main())
