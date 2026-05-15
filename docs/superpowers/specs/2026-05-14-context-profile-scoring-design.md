# Context-Driven Profile Scoring Design

**Date:** 2026-05-14  
**Status:** Approved  

---

## Problem

The current chunk scorer (`compute_chunk_score`) uses flat weights for all footage types:
- `energy`: high=3 / medium=2 / low=1
- `quality_penalty`: −1.5 for any non-good quality
- `loudness_weight`: 0–1 bonus from audio dBFS

This works reasonably for generic content but fails for domain-specific footage:
- Mountaineering highlights include slow, careful summit pushes that score "medium energy" — but they're the best moments
- Skiing highlights require steep energy differentiation (gondola ride ≈ 0, powder run ≈ max)
- Rock climbing highlights often come from precise, quiet crux moments — low energy, low loudness, but high interest
- Selection criteria also differ: skiing wants terrain variety; mountaineering wants altitude progression and summit moments

The selection step (currently manual or generic Claude prompt) has no domain knowledge either.

---

## Solution

**Context-driven profiles.** The user provides a free-text description of their trip. Claude Code generates a `profile.json` from that description — calibrated scoring weights + a domain-aware selection prompt. The profile is generated once and reused across `score` and `select` runs.

Claude Code is the user interface. The user talks to Claude Code, provides videos and context, and Claude Code runs the full pipeline on their behalf.

---

## Workflow

```
User provides:  videos + context string
                ("Kilimanjaro mountaineering trip, 6 days, summit on day 5")

Claude Code:
  1. Generates profile.json from context
  2. Runs pipeline.py batch + merge  → all_chunks.json
  3. Runs pipeline.py score --profile profile.json  → ranked chunks
  4. Reads all_chunks.json + profile.json, selects clips using
     profile.selection_prompt as selection guide
  5. Shows user selection with per-chunk reasons
  6. User confirms or adjusts
  7. Runs pipeline.py cut --selected ... --output reel.mp4
```

No `pipeline.py select` command is needed — Claude Code does the selection interactively in conversation, which allows for user confirmation and adjustment in the same step.

---

## Profile JSON Schema

Claude Code writes this file directly (no subprocess). Saved alongside `all_chunks.json`, e.g. `output_dir/profile.json`.

```json
{
  "context": "Kilimanjaro mountaineering trip, 6 days, summit on day 5",
  "scoring": {
    "energy_weights": {
      "high":   3.0,
      "medium": 2.5,
      "low":    1.5
    },
    "quality_penalty": -1.0,
    "loudness_weight": 0.3,
    "shot_type_bonus": {
      "wide shot": 0.5,
      "aerial":    1.0
    }
  },
  "selection_prompt": "You are selecting highlights for a Kilimanjaro mountaineering reel. Prefer: summit moments (especially the crater rim and Uhuru Peak), dramatic altitude vistas, glacier views, signs of physical effort and team camaraderie. Avoid: repetitive uphill hiking that looks identical, camp setup and administrative moments, gear-sorting scenes."
}
```

### Scoring weight guidance (for Claude Code when generating profiles)

| Domain | energy curve | quality_penalty | loudness_weight | shot_type_bonus |
|---|---|---|---|---|
| Mountaineering | flat (2.5/2.0/1.5) — slow careful movement ≠ boring | −1.0 (some shake OK) | 0.3 (wind ≠ exciting) | aerial +1.0, wide +0.5 |
| Skiing | steep (4.0/1.5/0.5) — gondola = skip | −2.0 (blur on snow = jarring) | 1.5 (speed sounds matter) | aerial +1.0 |
| Rock climbing | moderate (3.0/2.5/1.0) — crux moves are precise | −1.5 | 0.3 (climbing is quiet) | close-up +0.5 (shows technique) |
| Travel / general | standard (3.0/2.0/1.0) | −1.5 | 1.0 | none |

These are starting points. Claude Code should reason from the context string and adjust — a "technical alpine climbing route" should skew toward the climbing profile even if the user says "mountaineering."

---

## Code Changes Required

### `pipeline.py` — `compute_chunk_score`

Add optional `profile` parameter:

```python
def compute_chunk_score(chunk: dict, loudness_db: float | None = None, profile: dict | None = None) -> float:
    weights = (profile or {}).get("scoring", {})
    
    energy_w = weights.get("energy_weights", {"high": 3.0, "medium": 2.0, "low": 1.0})
    quality_penalty = weights.get("quality_penalty", -1.5)
    loudness_weight = weights.get("loudness_weight", 1.0)
    shot_bonuses = weights.get("shot_type_bonus", {})

    score = 0.0
    energy = (chunk.get("energy") or "").lower()
    if "high" in energy:     score += energy_w.get("high", 3.0)
    elif "medium" in energy: score += energy_w.get("medium", 2.0)
    elif "low" in energy:    score += energy_w.get("low", 1.0)
    else:                    score += (energy_w.get("high", 3.0) + energy_w.get("low", 1.0)) / 2

    if (chunk.get("quality") or "good").lower() != "good":
        score += quality_penalty

    if loudness_db is not None:
        score += max(0.0, min(1.0, (loudness_db + 60) / 50)) * loudness_weight

    shot = (chunk.get("shot") or "").lower()
    for shot_key, bonus in shot_bonuses.items():
        if shot_key.lower() in shot:
            score += bonus
            break

    return round(max(0.0, score), 2)
```

### `pipeline.py` — `score` subcommand

Add `--profile` flag:

```bash
python pipeline.py score \
  --chunks-json all_chunks.json \
  --profile profile.json \
  --audio \
  --update
```

Reads `profile.json`, passes it to `compute_chunk_score`. Falls back to current flat weights if no profile provided (backwards compatible).

### `SKILL.md` — updated workflow

Add profile generation as Step 0 in both Single-Video and Multi-Video flows:

```
Step 0: Context + profile
  Ask user: "Give me a one-sentence description of this footage
  (e.g. 'skiing powder day at Chamonix' or 'Kilimanjaro summit attempt')."
  Generate profile.json from the description.
  Save to <output_dir>/profile.json.
```

Update selection step to load `profile.json` and use `selection_prompt` as the
selection guide when reading through chunks.

---

## What Does NOT Change

- `pipeline.py describe`, `batch`, `merge`, `cut` — unchanged
- `bench.py` — unchanged (benchmarking is separate)
- Profile generation requires no subprocess, no new CLI command
- `--profile` is optional everywhere; omitting it gives current behaviour

---

## File Layout

```
output_dir/
  all_chunks.json     # merged chunk descriptions
  profile.json        # generated by Claude Code from user context  ← new
  thumbs.html
  thumbs/
  highlight.mp4
```

---

## Success Criteria

1. `pipeline.py score --profile profile.json` produces meaningfully different rankings than without profile on domain-specific footage
2. Claude Code's selection reasoning explicitly references the profile's `selection_prompt` criteria (e.g. "selected because it shows summit moment per profile criteria")
3. TVSum τ on the 5 test videos improves or stays the same (profile shouldn't hurt generic content)
4. User can go from "here are my videos" to a cut highlight reel in one Claude Code conversation, providing only: video paths + one context sentence
