import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pipeline import compute_chunk_score

def test_no_profile_high_energy():
    chunk = {"energy": "high", "quality": "good"}
    assert compute_chunk_score(chunk) == 3.0

def test_no_profile_quality_penalty():
    chunk = {"energy": "high", "quality": "shaky"}
    assert compute_chunk_score(chunk) == 1.5   # 3.0 - 1.5

def test_no_profile_loudness_bonus():
    chunk = {"energy": "medium", "quality": "good"}
    # -10dBFS maps to full +1 bonus: ((-10+60)/50) = 1.0
    assert compute_chunk_score(chunk, loudness_db=-10.0) == 3.0

def test_profile_skiing_steep_curve():
    profile = {"scoring": {"energy_weights": {"high": 4.0, "medium": 1.5, "low": 0.5},
                           "quality_penalty": -2.0, "loudness_weight": 1.5, "shot_type_bonus": {}}}
    chunk = {"energy": "high", "quality": "good"}
    assert compute_chunk_score(chunk, profile=profile) == 4.0

def test_profile_skiing_low_energy_penalised():
    profile = {"scoring": {"energy_weights": {"high": 4.0, "medium": 1.5, "low": 0.5},
                           "quality_penalty": -2.0, "loudness_weight": 1.5, "shot_type_bonus": {}}}
    chunk = {"energy": "low", "quality": "good"}
    assert compute_chunk_score(chunk, profile=profile) == 0.5

def test_profile_shot_type_bonus():
    profile = {"scoring": {"energy_weights": {"high": 3.0, "medium": 2.0, "low": 1.0},
                           "quality_penalty": -1.5, "loudness_weight": 1.0,
                           "shot_type_bonus": {"aerial": 1.0, "wide shot": 0.5}}}
    chunk = {"energy": "medium", "quality": "good", "shot": "aerial shot"}
    assert compute_chunk_score(chunk, profile=profile) == 3.0   # 2.0 + 1.0

def test_profile_shot_type_only_first_match():
    profile = {"scoring": {"energy_weights": {"high": 3.0, "medium": 2.0, "low": 1.0},
                           "quality_penalty": -1.5, "loudness_weight": 1.0,
                           "shot_type_bonus": {"aerial": 1.0, "wide shot": 0.5}}}
    # "wide aerial shot" matches "aerial" first (dict iteration order) — only one bonus applies
    chunk = {"energy": "medium", "quality": "good", "shot": "wide aerial shot"}
    score = compute_chunk_score(chunk, profile=profile)
    assert score in (3.0, 2.5)   # exactly one bonus applied

def test_profile_mountaineering_flat_curve():
    profile = {"scoring": {"energy_weights": {"high": 3.0, "medium": 2.5, "low": 1.5},
                           "quality_penalty": -1.0, "loudness_weight": 0.3, "shot_type_bonus": {}}}
    high = compute_chunk_score({"energy": "high", "quality": "good"}, profile=profile)
    low  = compute_chunk_score({"energy": "low",  "quality": "good"}, profile=profile)
    assert high - low == 1.5   # flatter than default (3.0 - 1.0 = 2.0)

def test_profile_loudness_weight_applied():
    profile = {"scoring": {"energy_weights": {"high": 3.0, "medium": 2.0, "low": 1.0},
                           "quality_penalty": -1.5, "loudness_weight": 0.3, "shot_type_bonus": {}}}
    chunk = {"energy": "medium", "quality": "good"}
    # -10dBFS → raw loudness bonus 1.0 × 0.3 = 0.3
    score = compute_chunk_score(chunk, loudness_db=-10.0, profile=profile)
    assert abs(score - 2.3) < 0.01

def test_no_profile_unknown_energy_is_midpoint():
    chunk = {"energy": None, "quality": "good"}
    # default midpoint: (3.0 + 1.0) / 2 = 2.0 (rounded)
    assert compute_chunk_score(chunk) == 2.0

def test_score_floored_at_zero():
    chunk = {"energy": "low", "quality": "blurry, shaky"}
    # 1.0 - 1.5 = -0.5 → clamped to 0
    assert compute_chunk_score(chunk) == 0.0

import json, os

def test_score_command_with_profile(tmp_path):
    """pipeline.py score --profile should use profile weights."""
    import subprocess, sys
    chunks_json = tmp_path / "chunks.json"
    profile_json = tmp_path / "profile.json"

    chunks_json.write_text(json.dumps({"video": "x.mp4", "duration": 20.0, "chunks": [
        {"start": 0.0, "end": 10.0, "energy": "high",   "quality": "good", "source_video": "x.mp4"},
        {"start": 10.0, "end": 20.0, "energy": "low",   "quality": "good", "source_video": "x.mp4"},
    ], "speech": []}))

    # Skiing profile — steep energy curve
    profile_json.write_text(json.dumps({"scoring": {
        "energy_weights": {"high": 4.0, "medium": 1.5, "low": 0.5},
        "quality_penalty": -2.0, "loudness_weight": 1.5, "shot_type_bonus": {}
    }}))

    result = subprocess.run(
        [sys.executable, "pipeline.py", "score",
         "--chunks-json", str(chunks_json),
         "--profile", str(profile_json),
         "--update"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    assert result.returncode == 0, result.stderr

    data = json.loads(chunks_json.read_text())
    scores = {ch["index"]: ch["score"] for ch in data["chunks"]}
    assert scores[0] == 4.0   # high energy with skiing profile
    assert scores[1] == 0.5   # low energy with skiing profile
    assert scores[0] - scores[1] == 3.5  # steeper than default (3.0 - 1.0 = 2.0)
