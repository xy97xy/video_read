# Claude Describe Provider — Design Spec
Date: 2026-06-07

## Problem

The `describe` command uses Qwen2.5-VL running locally on GPU. It processes photos
sequentially and takes ~2 hours for a large Google Takeout library. The GPU is the
bottleneck — no parallelism is possible within Qwen.

## Solution

Add a `claude` provider to the `describe` command that invokes `claude -p` as
concurrent subprocesses. Claude reads images natively via the Claude Code CLI.
Up to N workers run in parallel (default 5), dramatically reducing wall-clock time.
Both providers write the same DB schema so results are interchangeable.

## CLI Interface

```bash
# Default (unchanged)
python photos.py describe --db photos.db

# Claude provider, 5 parallel workers
python photos.py describe --db photos.db --provider claude --model haiku --workers 5

# Sonnet for higher quality
python photos.py describe --db photos.db --provider claude --model sonnet --workers 3

# Benchmark: compare all providers on 20 random photos (no DB writes)
python photos.py describe --db photos.db --benchmark --workers 5
```

New flags on the `describe` subcommand:
- `--provider [qwen|claude]` — default `qwen`
- `--model [haiku|sonnet|opus]` — default `haiku`, only used with `--provider claude`
- `--workers N` — number of concurrent Claude subprocesses, default 5 (ignored for qwen)
- `--benchmark` — compare providers on 20 random already-described photos, print table, exit

## Architecture

### `photos/describe.py`

Add `ClaudeDescriber` class alongside existing Qwen logic:

```python
class ClaudeDescriber:
    def __init__(self, model: str = "haiku", workers: int = 5):
        self.model = model
        self.workers = workers
        self.claude_bin = # resolved at init: ~/.local/bin/claude

    async def describe_one(self, photo_path: str) -> dict:
        # Runs: claude -p --model haiku --dangerously-skip-permissions
        #         "Read this image and return JSON: ..."
        # Returns: {"caption": ..., "scene": ..., "people": ..., "quality": ...}

    async def describe_batch(self, photos: list[dict]) -> list[dict]:
        # asyncio.Semaphore(self.workers) limits concurrency
        # progress bar via tqdm
```

Prompt sent to Claude (same structured output as Qwen):
```
Read this image file and return ONLY a JSON object with these fields:
- caption: 1-2 sentence description of what is happening
- scene: setting/location (e.g. "beach", "mountain trail", "indoor party")
- people: number of people visible (integer, 0 if none)
- quality: one of "good", "dark", "blurry", "duplicate"

Image: <path>
```

### `photos.py` changes

- Add `--provider`, `--model`, `--workers`, `--benchmark` to `describe` subparser
- `cmd_describe`: branch on `args.provider` — `qwen` path unchanged, `claude` path uses `ClaudeDescriber`
- Benchmark mode: fetch 20 random rows where `described_at IS NOT NULL`, re-describe with each provider, print comparison table (speed, side-by-side captions), no DB writes

### DB schema

No changes. `ClaudeDescriber` writes the same fields Qwen writes:
`caption`, `scene`, `people`, `quality`, `described_at`.

## Data Flow

```
describe --provider claude --model haiku --workers 5
  → fetch undescribed photos from DB
  → ClaudeDescriber.describe_batch(photos)
    → asyncio semaphore (5 slots)
    → per photo: subprocess [claude -p --model haiku ...]
    → parse JSON from stdout
    → write to DB
  → tqdm progress bar
  → done
```

## Error Handling

- If `claude` binary not found: fail fast with clear message pointing to `~/.local/bin/claude`
- If subprocess returns non-JSON or times out (30s): log warning, skip photo (leave undescribed)
- If model overloaded: retry once with 5s backoff, then skip

## Testing

- Unit test `ClaudeDescriber.describe_one` by mocking subprocess
- Integration test: benchmark mode runs without DB writes
- Existing Qwen tests unchanged

## Out of Scope

- Qwen parallelism (GPU bound, separate problem)
- Automatic Qwen overwrite of Claude descriptions (can re-run `describe` with `--provider qwen` manually)
- Video pipeline (pipeline.py unchanged)
