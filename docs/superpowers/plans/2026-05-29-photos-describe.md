# Photos Describe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `photos.py describe` subcommand that runs Qwen2.5-VL on every photo and stores structured descriptions (caption, quality, scene, people) in the SQLite DB.

**Architecture:** New `photos/describe.py` module holds pure functions (JSON parsing, Qwen loading, photo inference with multi-turn retry). `cmd_describe` in `photos.py` orchestrates the batch run with tqdm progress and per-photo commits for resumability. GPU imports are lazy so `photos.py` stays importable without torch for non-GPU subcommands.

**Tech Stack:** Python 3.10+, SQLite3, Qwen2.5-VL-7B-Instruct (4-bit via bitsandbytes), transformers, qwen-vl-utils, pillow-heif, tqdm

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `photos/describe.py` | Create | `_parse_describe_json`, `load_qwen`, `_call_qwen`, `describe_photo` |
| `photos.py` | Modify | `_init_db` (5 new columns + migration), `cmd_describe`, `main()` registration |
| `requirements.txt` | Modify | Add `pillow-heif>=0.13.0` |
| `tests/test_describe.py` | Create | Unit tests: JSON parsing, DB migration, missing-file skip |
| `tests/test_describe_cmd.py` | Create | CLI tests (mock-based) + GPU integration test (skipif) |

---

## Task 1: DB Schema Migration

**Files:**
- Modify: `photos.py:19-37` (`_init_db`)
- Test: `tests/test_describe.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_describe.py`:

```python
import os, sys, sqlite3
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def test_migration_adds_describe_columns(tmp_path):
    db = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT,
        cluster_id INTEGER, discarded INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

    from photos import _init_db
    conn2 = _init_db(db)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(photos)")}
    conn2.close()
    for col in ("caption", "quality", "scene", "people", "described_at"):
        assert col in cols, f"Missing column: {col}"


def test_new_db_has_describe_columns(tmp_path):
    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    conn.close()
    for col in ("caption", "quality", "scene", "people", "described_at"):
        assert col in cols, f"Missing column: {col}"


def test_described_at_defaults_to_null(tmp_path):
    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    conn.execute("INSERT INTO photos (path, taken_at) VALUES (?,?)", (str(f), 1000))
    conn.commit()
    val = conn.execute("SELECT described_at FROM photos").fetchone()[0]
    conn.close()
    assert val is None
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py -v
```

Expected: 3 FAIL — `AssertionError: Missing column: caption`

- [ ] **Step 3: Update `_init_db` in `photos.py`**

Replace the current `_init_db` function (lines 19–37) with:

```python
def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id           INTEGER PRIMARY KEY,
            path         TEXT UNIQUE,
            taken_at     INTEGER,
            lat          REAL,
            lon          REAL,
            place        TEXT,
            cluster_id   INTEGER,
            discarded    INTEGER DEFAULT 0,
            caption      TEXT,
            quality      TEXT,
            scene        TEXT,
            people       TEXT,
            described_at INTEGER
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    migrations = [
        ("discarded",    "ALTER TABLE photos ADD COLUMN discarded    INTEGER DEFAULT 0"),
        ("caption",      "ALTER TABLE photos ADD COLUMN caption      TEXT"),
        ("quality",      "ALTER TABLE photos ADD COLUMN quality      TEXT"),
        ("scene",        "ALTER TABLE photos ADD COLUMN scene        TEXT"),
        ("people",       "ALTER TABLE photos ADD COLUMN people       TEXT"),
        ("described_at", "ALTER TABLE photos ADD COLUMN described_at INTEGER"),
    ]
    for col, sql in migrations:
        if col not in cols:
            conn.execute(sql)
    conn.commit()
    return conn
```

- [ ] **Step 4: Run tests**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Full suite — ensure no regressions**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/ --ignore=tests/test_scoring.py --ignore=tests/test_pipeline_multi.py -q
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
cd /home/xiaoyu/git/video_read && git add photos.py tests/test_describe.py && git commit -m "feat: add describe columns to photos DB schema with migration"
```

---

## Task 2: `_parse_describe_json` Pure Function

**Files:**
- Create: `photos/describe.py`
- Test: `tests/test_describe.py` (append)

- [ ] **Step 1: Append tests to `tests/test_describe.py`**

```python
from photos.describe import _parse_describe_json


def test_parse_describe_json_valid():
    raw = '{"caption": "two people hiking", "quality": "good", "scene": "mountain trail", "people": "few"}'
    result = _parse_describe_json(raw)
    assert result == {
        "caption": "two people hiking",
        "quality": "good",
        "scene": "mountain trail",
        "people": "few",
    }


def test_parse_describe_json_missing_fields():
    raw = '{"caption": "hikers", "quality": "good"}'
    assert _parse_describe_json(raw) is None


def test_parse_describe_json_malformed():
    assert _parse_describe_json("not json at all") is None
    assert _parse_describe_json("") is None


def test_parse_describe_json_strips_markdown():
    raw = '```json\n{"caption": "sunset", "quality": "good", "scene": "beach", "people": "none"}\n```'
    result = _parse_describe_json(raw)
    assert result is not None
    assert result["caption"] == "sunset"


def test_parse_describe_json_embedded_in_text():
    raw = 'Here is the JSON: {"caption": "park", "quality": "good", "scene": "city park", "people": "many"} Done.'
    result = _parse_describe_json(raw)
    assert result is not None
    assert result["scene"] == "city park"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py -k "parse" -v
```

Expected: 5 FAIL — `ModuleNotFoundError: No module named 'photos.describe'`

- [ ] **Step 3: Create `photos/describe.py`**

```python
from __future__ import annotations
import json
import logging
import re

log = logging.getLogger(__name__)

_DESCRIBE_PROMPT = (
    "Describe this photo. Reply with ONLY this JSON — no markdown, no extra text:\n"
    '{"caption": "one sentence describing the main subject and what is happening",\n'
    ' "quality": "one word: good, blurry, dark, overexposed, or obstructed",\n'
    ' "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",\n'
    ' "people": "one word: none, one, few, or many"}'
)

_RETRY_TEMPLATE = (
    "That was not valid JSON. Required format:\n"
    '{{"caption": "...", "quality": "good|blurry|dark|overexposed|obstructed", '
    '"scene": "...", "people": "none|one|few|many"}}\n'
    "Your response was: {prev}\n"
    "Output ONLY the JSON object."
)


def _parse_describe_json(raw: str) -> dict | None:
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()

    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not {"caption", "quality", "scene", "people"}.issubset(parsed.keys()):
            continue
        return {
            "caption": str(parsed["caption"]).strip() or None,
            "quality": str(parsed["quality"]).strip() or None,
            "scene":   str(parsed["scene"]).strip() or None,
            "people":  str(parsed["people"]).strip() or None,
        }
    return None
```

- [ ] **Step 4: Run tests**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py -v
```

Expected: 8 PASS (3 migration + 5 parse)

- [ ] **Step 5: Commit**

```bash
cd /home/xiaoyu/git/video_read && git add photos/describe.py tests/test_describe.py && git commit -m "feat: add photos/describe.py with _parse_describe_json"
```

---

## Task 3: `load_qwen`, `_call_qwen`, `describe_photo`

**Files:**
- Modify: `photos/describe.py` (append GPU functions)
- Modify: `requirements.txt`
- Test: `tests/test_describe.py` (append 1 no-GPU test)
- Test: `tests/test_describe_cmd.py` (new file, GPU integration test)

- [ ] **Step 1: Append the no-GPU unit test to `tests/test_describe.py`**

```python
def test_describe_photo_returns_nulls_for_missing_file():
    from photos.describe import describe_photo
    result = describe_photo(None, None, Path("/nonexistent/ghost.jpg"))
    assert result == {"caption": None, "quality": None, "scene": None, "people": None}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py::test_describe_photo_returns_nulls_for_missing_file -v
```

Expected: FAIL — `ImportError` or `AttributeError` (describe_photo not defined yet)

- [ ] **Step 3: Add GPU functions to `photos/describe.py`**

Append to the existing `photos/describe.py` (after `_parse_describe_json`):

```python
import os
import tempfile
from pathlib import Path

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"


def load_qwen():
    import torch
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    processor = AutoProcessor.from_pretrained(QWEN_MODEL)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        QWEN_MODEL, quantization_config=bnb, device_map="cuda:0"
    )
    model.eval()
    return model, processor


def _call_qwen(model, processor, messages: list[dict]) -> str:
    import torch
    from qwen_vl_utils import process_vision_info
    text_input = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(messages, return_video_kwargs=True)
    if isinstance(video_kwargs.get("fps"), list):
        video_kwargs["fps"] = video_kwargs["fps"][0]
    inputs = processor(
        text=[text_input], images=image_inputs, videos=video_inputs,
        return_tensors="pt", **video_kwargs,
    ).to(model.device)
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=200, do_sample=False)
    decoded = processor.decode(outputs[0], skip_special_tokens=True)
    for marker in ("assistant\n", "Assistant: ", "assistant: "):
        if marker in decoded:
            decoded = decoded.split(marker, 1)[1].strip()
            break
    return decoded.strip()


def describe_photo(model, processor, path: Path) -> dict:
    _NULL = {"caption": None, "quality": None, "scene": None, "people": None}
    if not path.exists():
        return _NULL
    tmp_name = None
    try:
        from PIL import Image
        img_path = str(path)
        if path.suffix.lower() == ".heic":
            img = Image.open(path)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img.save(tmp.name, "JPEG")
            tmp_name = tmp.name
            img_path = tmp_name
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": img_path},
                {"type": "text", "text": _DESCRIBE_PROMPT},
            ],
        }]
        for _ in range(3):
            raw = _call_qwen(model, processor, messages)
            result = _parse_describe_json(raw)
            if result is not None:
                return result
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": _RETRY_TEMPLATE.format(prev=raw[:300]),
            })
        log.warning(f"describe_photo: 3 parse failures for {path.name}")
        return _NULL
    except Exception as e:
        log.warning(f"describe_photo failed for {path.name}: {e}")
        return _NULL
    finally:
        if tmp_name:
            os.unlink(tmp_name)
```

- [ ] **Step 4: Add `pillow-heif` to `requirements.txt`**

Append to `/home/xiaoyu/git/video_read/requirements.txt`:
```
pillow-heif>=0.13.0
```

- [ ] **Step 5: Run the no-GPU unit test**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py -v
```

Expected: 9 PASS

- [ ] **Step 6: Create `tests/test_describe_cmd.py` with GPU integration test**

```python
import os, sys, shutil, sqlite3, subprocess
from pathlib import Path
import pytest

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(not _cuda_available(), reason="requires GPU")
def test_describe_real_photo(tmp_path):
    takeout = Path(PROJ) / "output" / "takeout" / "Takeout" / "Google Photos"
    jpgs = list(takeout.rglob("*.JPG")) + list(takeout.rglob("*.jpg"))
    if not jpgs:
        pytest.skip("No JPG files found in output/takeout")

    src = jpgs[0]
    dest = tmp_path / src.name
    shutil.copy2(src, dest)

    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (1,?,1000)", (str(dest),))
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "photos.py", "describe", "--db", db],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    assert "Described 1" in result.stdout

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT caption, described_at FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[1] is not None, "described_at should be set"
    assert row[0] is not None, "caption should be non-null"
```

- [ ] **Step 7: Run non-GPU tests only**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe.py tests/test_describe_cmd.py -v
```

Expected: 9 PASS from test_describe.py, 0 collected (skipped) from test_describe_cmd.py if no GPU, or 1 PASS if GPU available

- [ ] **Step 8: Commit**

```bash
cd /home/xiaoyu/git/video_read && git add photos/describe.py requirements.txt tests/test_describe.py tests/test_describe_cmd.py && git commit -m "feat: add load_qwen and describe_photo with multi-turn retry loop"
```

---

## Task 4: `cmd_describe` Subcommand

**Files:**
- Modify: `photos.py` (add `cmd_describe`, register in `main()`)
- Test: `tests/test_describe_cmd.py` (append 3 tests)

- [ ] **Step 1: Append tests to `tests/test_describe_cmd.py`**

```python
import argparse
import sqlite3
from unittest.mock import patch, MagicMock


def test_describe_subcommand_in_help():
    result = subprocess.run(
        [sys.executable, "photos.py", "--help"],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0
    assert "describe" in result.stdout


def test_cmd_describe_exits_early_when_all_described(tmp_path):
    """When all photos already have described_at set, model is never loaded."""
    from photos import cmd_describe, _init_db

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, described_at) VALUES (1, '/a.jpg', 1000, 9999)"
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(db=db, force=False)
    with patch("photos.describe.load_qwen") as mock_load:
        cmd_describe(args)
    mock_load.assert_not_called()


def test_cmd_describe_skips_missing_file(tmp_path):
    """Files that don't exist on disk are skipped — described_at stays NULL."""
    from photos import cmd_describe, _init_db

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at) VALUES (1, '/nonexistent/ghost.jpg', 1000)"
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(db=db, force=False)
    mock_model = MagicMock()
    mock_processor = MagicMock()

    with patch("photos.describe.load_qwen", return_value=(mock_model, mock_processor)):
        with patch("photos.describe.describe_photo") as mock_describe:
            cmd_describe(args)

    mock_describe.assert_not_called()

    conn = sqlite3.connect(db)
    val = conn.execute("SELECT described_at FROM photos WHERE id=1").fetchone()[0]
    conn.close()
    assert val is None, "described_at should stay NULL for missing file"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe_cmd.py::test_describe_subcommand_in_help tests/test_describe_cmd.py::test_cmd_describe_exits_early_when_all_described tests/test_describe_cmd.py::test_cmd_describe_skips_missing_file -v
```

Expected: FAIL — `argument subcommand: invalid choice: 'describe'` or `ImportError: cannot import name 'cmd_describe'`

- [ ] **Step 3: Add `cmd_describe` to `photos.py`**

Add before `main()`:

```python
def cmd_describe(args):
    from photos.describe import load_qwen, describe_photo

    conn = _init_db(args.db)
    try:
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

        print(f"Loading Qwen2.5-VL ({len(rows)} photos to describe)...")
        t0 = time.time()
        model, processor = load_qwen()
        print(f"Model loaded in {time.time() - t0:.0f}s")

        n_described = 0
        bar = tqdm(rows, unit="photo")
        for photo_id, photo_path in bar:
            p = Path(photo_path)
            if not p.exists():
                continue
            bar.set_description(p.name[:40])
            result = describe_photo(model, processor, p)
            conn.execute(
                "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                (result["caption"], result["quality"], result["scene"], result["people"],
                 int(time.time()), photo_id),
            )
            conn.commit()
            n_described += 1

        print(f"\n✓ Described {n_described} photo(s)")
    finally:
        conn.close()
```

- [ ] **Step 4: Register the subcommand in `main()`**

After the existing `dedup` parser block, add:

```python
    desc = sub.add_parser("describe", help="Describe photos with Qwen2.5-VL → store in DB")
    desc.add_argument("--db", default="photos.db", metavar="DB")
    desc.add_argument("--force", action="store_true", help="Re-describe already-described photos")
```

Update the dispatch dict:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe}[args.subcommand](args)
```

- [ ] **Step 5: Run the new tests**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_describe_cmd.py -v -k "not real_photo"
```

Expected: 3 PASS

- [ ] **Step 6: Full suite**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/ --ignore=tests/test_scoring.py --ignore=tests/test_pipeline_multi.py -q
```

Expected: all passing

- [ ] **Step 7: Commit**

```bash
cd /home/xiaoyu/git/video_read && git add photos.py tests/test_describe_cmd.py && git commit -m "feat: cmd_describe — batch Qwen description of photos with tqdm and resumability"
```

---

## Self-Review

**Spec coverage:**
- ✅ `photos/describe.py` with `load_qwen`, `describe_photo`, `_parse_describe_json` (Tasks 2, 3)
- ✅ DB migration: 5 new columns (caption, quality, scene, people, described_at) (Task 1)
- ✅ Multi-turn retry loop up to 3 attempts (Task 3, `describe_photo`)
- ✅ HEIC → JPEG temp file via pillow-heif (Task 3)
- ✅ `--db` and `--force` flags (Task 4)
- ✅ Skip missing files silently, leave `described_at` NULL (Task 4)
- ✅ Commit after each photo — resumable (Task 4)
- ✅ tqdm progress bar with filename (Task 4)
- ✅ `✓ Described N photo(s)` output (Task 4)
- ✅ Lazy GPU imports — `photos.py` importable without torch (Task 3, 4)
- ✅ `pillow-heif` added to requirements.txt (Task 3)
- ✅ Unit tests: JSON parsing, migration, missing file (Tasks 1, 2, 3)
- ✅ GPU integration test with `skipif(no CUDA)` (Task 3)
- ✅ Hard constraint: no files deleted anywhere (organize uses copy2, describe only reads)
