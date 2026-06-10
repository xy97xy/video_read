# Claude Describe Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--provider claude` option to `photos.py describe` that runs concurrent `claude -p` subprocesses to describe photos in parallel, dramatically reducing the ~2-hour Qwen bottleneck.

**Architecture:** `ClaudeDescriber` in `photos/describe.py` uses `asyncio` + `subprocess` to spawn up to N concurrent `claude -p` processes. `cmd_describe` in `photos.py` branches on `args.provider` — the `qwen` path is unchanged, the `claude` path calls `ClaudeDescriber.describe_batch()`. Both write the same DB fields. A `--benchmark` flag re-describes 20 already-described photos with all providers and prints a comparison table without writing to DB.

**Tech Stack:** Python `asyncio`, `subprocess`, `shutil.which`, `tqdm`, `tabulate`, `sqlite3` (existing)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `photos/describe.py` | Modify | Add `ClaudeDescriber` class + `CLAUDE_PROMPT` constant |
| `photos.py` | Modify | Add `--provider`, `--model`, `--workers`, `--benchmark` args; branch `cmd_describe`; add `cmd_benchmark` helper |
| `tests/test_describe_cmd.py` | Modify | Add tests for Claude provider and benchmark mode |
| `tests/test_describe.py` | Modify | Add unit tests for `ClaudeDescriber` |

---

## Task 1: Add `ClaudeDescriber` to `photos/describe.py`

**Files:**
- Modify: `photos/describe.py`
- Test: `tests/test_describe.py`

- [ ] **Step 1: Write failing tests for `ClaudeDescriber`**

Add to `tests/test_describe.py`:

```python
import asyncio
import json
import sys
import os
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

from photos.describe import ClaudeDescriber, _parse_describe_json


def test_claude_describer_init_finds_binary(tmp_path):
    fake_bin = tmp_path / "claude"
    fake_bin.write_text("#!/bin/sh\necho hi")
    fake_bin.chmod(0o755)
    with patch("shutil.which", return_value=str(fake_bin)):
        d = ClaudeDescriber(model="haiku", workers=3)
    assert d.claude_bin == str(fake_bin)
    assert d.model == "haiku"
    assert d.workers == 3


def test_claude_describer_init_raises_if_not_found():
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="claude"):
            ClaudeDescriber()


def test_claude_describer_describe_one_returns_parsed_dict(tmp_path):
    photo = tmp_path / "test.jpg"
    photo.write_bytes(b"fake")
    payload = json.dumps({
        "caption": "a sunny beach",
        "scene": "beach",
        "people": "few",
        "quality": "good",
    })

    async def run():
        d = ClaudeDescriber.__new__(ClaudeDescriber)
        d.claude_bin = "/fake/claude"
        d.model = "haiku"
        d.workers = 1

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            return await d.describe_one(str(photo))

    result = asyncio.run(run())
    assert result["caption"] == "a sunny beach"
    assert result["scene"] == "beach"
    assert result["quality"] == "good"


def test_claude_describer_describe_one_returns_null_on_bad_json(tmp_path):
    photo = tmp_path / "test.jpg"
    photo.write_bytes(b"fake")

    async def run():
        d = ClaudeDescriber.__new__(ClaudeDescriber)
        d.claude_bin = "/fake/claude"
        d.model = "haiku"
        d.workers = 1

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"not json at all", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            return await d.describe_one(str(photo))

    result = asyncio.run(run())
    assert result["caption"] is None
    assert result["quality"] is None


def test_claude_describer_describe_batch_respects_workers(tmp_path):
    photos = []
    for i in range(4):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"fake")
        photos.append({"id": i, "path": str(p)})

    payload = json.dumps({"caption": "test", "scene": "x", "people": "none", "quality": "good"})

    async def run():
        d = ClaudeDescriber.__new__(ClaudeDescriber)
        d.claude_bin = "/fake/claude"
        d.model = "haiku"
        d.workers = 2

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            return await d.describe_batch(photos)

    results = asyncio.run(run())
    assert len(results) == 4
    assert all(r["caption"] == "test" for r in results)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py::test_claude_describer_init_finds_binary tests/test_describe.py::test_claude_describer_init_raises_if_not_found tests/test_describe.py::test_claude_describer_describe_one_returns_parsed_dict -v 2>&1 | tail -20
```

Expected: `ImportError` or `AttributeError` — `ClaudeDescriber` doesn't exist yet.

- [ ] **Step 3: Add `ClaudeDescriber` to `photos/describe.py`**

Add after the existing imports at the top of `photos/describe.py`:

```python
import asyncio
import shutil
import time as _time
```

Add `CLAUDE_PROMPT` constant after `_RETRY_TEMPLATE`:

```python
CLAUDE_PROMPT = (
    "Read this image file and return ONLY a JSON object with no markdown, no extra text:\n"
    '{{"caption": "one sentence describing the main subject and what is happening",\n'
    ' "quality": "one word: good, blurry, dark, overexposed, or obstructed",\n'
    ' "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",\n'
    ' "people": "one word: none, one, few, or many"}}\n\n'
    "Image: {path}"
)
```

Add `ClaudeDescriber` class after the existing constants:

```python
_NULL = {"caption": None, "quality": None, "scene": None, "people": None}


class ClaudeDescriber:
    def __init__(self, model: str = "haiku", workers: int = 5):
        bin_path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        if not Path(bin_path).exists():
            raise RuntimeError(
                f"claude CLI not found. Expected at {bin_path}. "
                "Install Claude Code: https://claude.ai/code"
            )
        self.claude_bin = bin_path
        self.model = model
        self.workers = workers

    async def describe_one(self, photo_path: str) -> dict:
        prompt = CLAUDE_PROMPT.format(path=photo_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.claude_bin, "-p", "--model", self.model,
                "--dangerously-skip-permissions", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            raw = stdout.decode("utf-8", errors="replace")
            result = _parse_describe_json(raw)
            return result if result is not None else _NULL.copy()
        except asyncio.TimeoutError:
            log.warning(f"claude timeout for {photo_path}")
            return _NULL.copy()
        except Exception as e:
            log.warning(f"claude error for {photo_path}: {e}")
            return _NULL.copy()

    async def describe_batch(self, photos: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(self.workers)
        results = [None] * len(photos)

        async def _one(i, photo):
            async with sem:
                results[i] = await self.describe_one(photo["path"])

        await asyncio.gather(*[_one(i, p) for i, p in enumerate(photos)])
        return results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe.py::test_claude_describer_init_finds_binary tests/test_describe.py::test_claude_describer_init_raises_if_not_found tests/test_describe.py::test_claude_describer_describe_one_returns_parsed_dict tests/test_describe.py::test_claude_describer_describe_one_returns_null_on_bad_json tests/test_describe.py::test_claude_describer_describe_batch_respects_workers -v 2>&1 | tail -20
```

Expected: All 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add photos/describe.py tests/test_describe.py
git commit -m "feat: add ClaudeDescriber with async subprocess concurrency"
```

---

## Task 2: Add `--provider`, `--model`, `--workers` flags and branch `cmd_describe`

**Files:**
- Modify: `photos.py` (lines 320–362 for `cmd_describe`, lines 655–657 for subparser)
- Test: `tests/test_describe_cmd.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_describe_cmd.py`:

```python
import asyncio as _asyncio


def test_cmd_describe_claude_provider_writes_db(tmp_path):
    """--provider claude calls ClaudeDescriber.describe_batch and writes results to DB."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="claude", model="haiku", workers=2, benchmark=False
    )

    fake_result = {"caption": "a lake at dusk", "quality": "good", "scene": "lake", "people": "none"}

    async def fake_batch(photos):
        return [fake_result for _ in photos]

    mock_describer = MagicMock()
    mock_describer.describe_batch = fake_batch

    with patch("photos.describe.ClaudeDescriber", return_value=mock_describer):
        cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute(
        "SELECT caption, quality, scene, people, described_at FROM photos WHERE id=1"
    ).fetchone()
    conn.close()

    assert row[0] == "a lake at dusk"
    assert row[1] == "good"
    assert row[4] is not None


def test_cmd_describe_qwen_path_unchanged(tmp_path):
    """--provider qwen (default) still calls load_qwen and describe_photo."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, ?, 1000)",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=5, benchmark=False
    )
    mock_model = MagicMock()
    mock_processor = MagicMock()
    fake_result = {"caption": "a forest path", "quality": "good", "scene": "forest", "people": "none"}

    with patch("photos.describe.load_qwen", return_value=(mock_model, mock_processor)):
        with patch("photos.describe.describe_photo", return_value=fake_result):
            cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute("SELECT caption FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "a forest path"


def test_cmd_describe_new_flags_in_help():
    result = subprocess.run(
        [sys.executable, "photos.py", "describe", "--help"],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert "--provider" in result.stdout
    assert "--model" in result.stdout
    assert "--workers" in result.stdout
    assert "--benchmark" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py::test_cmd_describe_claude_provider_writes_db tests/test_describe_cmd.py::test_cmd_describe_new_flags_in_help -v 2>&1 | tail -20
```

Expected: FAIL — `args` has no `provider` attribute yet.

- [ ] **Step 3: Add new flags to the `describe` subparser in `photos.py`**

Replace lines 655–657 in `photos.py`:

```python
    desc = sub.add_parser("describe", help="Describe photos with Qwen2.5-VL → store in DB")
    desc.add_argument("--db", default="photos.db", metavar="DB")
    desc.add_argument("--force", action="store_true", help="Re-describe already-described photos")
```

With:

```python
    desc = sub.add_parser("describe", help="Describe photos with Qwen2.5-VL or Claude → store in DB")
    desc.add_argument("--db", default="photos.db", metavar="DB")
    desc.add_argument("--force", action="store_true", help="Re-describe already-described photos")
    desc.add_argument("--provider", choices=["qwen", "claude"], default="qwen",
                      help="Vision model provider (default: qwen)")
    desc.add_argument("--model", default="haiku",
                      choices=["haiku", "sonnet", "opus"],
                      help="Claude model to use (only with --provider claude, default: haiku)")
    desc.add_argument("--workers", type=int, default=5, metavar="N",
                      help="Concurrent Claude workers (only with --provider claude, default: 5)")
    desc.add_argument("--benchmark", action="store_true",
                      help="Compare providers on 20 sample photos, no DB writes")
```

- [ ] **Step 4: Branch `cmd_describe` in `photos.py`**

Replace the existing `cmd_describe` function (lines 320–362):

```python
def cmd_describe(args):
    import asyncio
    from photos.describe import load_qwen, describe_photo, ClaudeDescriber

    conn = _init_db(args.db)
    provider = getattr(args, "provider", "qwen")
    benchmark = getattr(args, "benchmark", False)

    try:
        if benchmark:
            _cmd_benchmark(conn, args)
            return

        if getattr(args, "force", False):
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0 AND described_at IS NULL"
            ).fetchall()

        if not rows:
            print("✓ All photos already described. Use --force to re-describe.")
            return

        _VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi'}
        photos = [
            {"id": photo_id, "path": photo_path}
            for photo_id, photo_path in rows
            if Path(photo_path).exists() and Path(photo_path).suffix.lower() not in _VIDEO_EXTS
        ]

        if provider == "claude":
            describer = ClaudeDescriber(
                model=getattr(args, "model", "haiku"),
                workers=getattr(args, "workers", 5),
            )
            print(f"Describing {len(photos)} photos with Claude ({describer.model}, {describer.workers} workers)...")
            results = asyncio.run(describer.describe_batch(photos))
            n_described = 0
            for photo, result in zip(photos, results):
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                n_described += 1
            conn.commit()
            print(f"\n✓ Described {n_described} photo(s) with Claude {describer.model}")
        else:
            print(f"Loading Qwen2.5-VL ({len(photos)} photos to describe)...")
            t0 = time.time()
            model, processor = load_qwen()
            print(f"Model loaded in {time.time() - t0:.0f}s")

            n_described = 0
            bar = tqdm(photos, unit="photo")
            for photo in bar:
                p = Path(photo["path"])
                bar.set_description(p.name[:40])
                result = describe_photo(model, processor, p)
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                conn.commit()
                n_described += 1

            print(f"\n✓ Described {n_described} photo(s)")
    finally:
        conn.close()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py -v 2>&1 | tail -30
```

Expected: All tests PASS (including existing ones).

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_describe_cmd.py
git commit -m "feat: add --provider claude flag with parallel async workers to describe cmd"
```

---

## Task 3: Add benchmark mode

**Files:**
- Modify: `photos.py` — add `_cmd_benchmark` helper function
- Test: `tests/test_describe_cmd.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_describe_cmd.py`:

```python
def test_benchmark_does_not_write_db(tmp_path):
    """Benchmark mode reads DB but never writes described_at."""
    real_file = tmp_path / "photo.jpg"
    real_file.write_bytes(b"fake jpeg data")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, described_at, caption, quality, scene, people) "
        "VALUES (1, ?, 1000, 9999, 'old caption', 'good', 'park', 'none')",
        (str(real_file),),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        db=db, force=False, provider="qwen", model="haiku", workers=2, benchmark=True
    )

    fake_result = {"caption": "new caption", "quality": "good", "scene": "beach", "people": "few"}

    async def fake_batch(photos):
        return [fake_result for _ in photos]

    mock_describer = MagicMock()
    mock_describer.describe_batch = fake_batch

    with patch("photos.describe.ClaudeDescriber", return_value=mock_describer):
        with patch("photos.describe.load_qwen", return_value=(MagicMock(), MagicMock())):
            with patch("photos.describe.describe_photo", return_value=fake_result):
                cmd_describe(args)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    row = conn.execute("SELECT caption, described_at FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "old caption", "benchmark must not overwrite DB"
    assert row[1] == 9999, "described_at must not change"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py::test_benchmark_does_not_write_db -v 2>&1 | tail -15
```

Expected: FAIL — `_cmd_benchmark` not defined.

- [ ] **Step 3: Add `_cmd_benchmark` to `photos.py`**

Add this function before `cmd_describe` in `photos.py`:

```python
def _cmd_benchmark(conn, args):
    import asyncio
    from photos.describe import ClaudeDescriber, describe_photo, load_qwen

    rows = conn.execute(
        "SELECT id, path, caption, scene, people, quality FROM photos "
        "WHERE described_at IS NOT NULL AND discarded=0 ORDER BY RANDOM() LIMIT 20"
    ).fetchall()

    if not rows:
        print("⚠ No described photos found. Run describe first.")
        return

    photos = [{"id": r[0], "path": r[1]} for r in rows]
    existing = {r[0]: {"caption": r[2], "scene": r[3], "people": r[4], "quality": r[5]} for r in rows}

    print(f"Benchmarking {len(photos)} photos across providers...\n")

    results = {"qwen (existing)": [], "claude-haiku": [], "claude-sonnet": []}

    # Existing Qwen descriptions (no re-inference needed)
    for photo in photos:
        results["qwen (existing)"].append(existing[photo["id"]])

    # Claude haiku
    t0 = time.time()
    haiku = ClaudeDescriber(model="haiku", workers=getattr(args, "workers", 5))
    haiku_results = asyncio.run(haiku.describe_batch(photos))
    haiku_time = time.time() - t0
    results["claude-haiku"] = haiku_results

    # Claude sonnet
    t0 = time.time()
    sonnet = ClaudeDescriber(model="sonnet", workers=getattr(args, "workers", 5))
    sonnet_results = asyncio.run(sonnet.describe_batch(photos))
    sonnet_time = time.time() - t0
    results["claude-sonnet"] = sonnet_results

    print(f"claude-haiku:  {haiku_time:.1f}s for {len(photos)} photos ({haiku_time/len(photos):.1f}s/photo)")
    print(f"claude-sonnet: {sonnet_time:.1f}s for {len(photos)} photos ({sonnet_time/len(photos):.1f}s/photo)")
    print()

    # Side-by-side caption comparison for first 5 photos
    print("--- Caption comparison (first 5 photos) ---")
    for i, photo in enumerate(photos[:5]):
        print(f"\nPhoto: {Path(photo['path']).name}")
        for provider, res_list in results.items():
            caption = res_list[i].get("caption") or "(none)"
            print(f"  [{provider}] {caption}")
    print("\n✓ Benchmark complete. No DB writes performed.")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /scratch/video_read && python -m pytest tests/test_describe_cmd.py::test_benchmark_does_not_write_db -v 2>&1 | tail -15
```

Expected: PASS.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
cd /scratch/video_read && python -m pytest tests/ -v --ignore=tests/test_pipeline_multi.py -k "not real_photo" 2>&1 | tail -30
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_describe_cmd.py
git commit -m "feat: add --benchmark mode comparing qwen vs claude-haiku vs claude-sonnet"
```

---

## Task 4: Smoke test end-to-end

- [ ] **Step 1: Verify help output shows new flags**

```bash
cd /scratch/video_read && python photos.py describe --help
```

Expected output includes: `--provider`, `--model`, `--workers`, `--benchmark`

- [ ] **Step 2: Run a quick live describe on 1 photo with Claude haiku**

Find a photo in the DB and run:

```bash
cd /scratch/video_read && python photos.py describe --db photos.db --provider claude --model haiku --workers 1 --force 2>&1 | head -10
```

Expected: Describes photos, prints `✓ Described N photo(s) with Claude haiku`

- [ ] **Step 3: Commit docs and spec**

```bash
git add docs/
git commit -m "docs: add claude-describe-provider spec and implementation plan"
```
