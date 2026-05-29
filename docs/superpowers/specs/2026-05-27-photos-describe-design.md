# Photos Describe Design

**Goal:** Add a `photos.py describe` subcommand that runs Qwen2.5-VL on every photo and stores structured descriptions in the DB, forming the foundation for smart dedup, album naming, deletion suggestions, and search.

**Pipeline position:** `scan → describe → dedup → cluster → review → organize`

---

## Architecture

Two new components:

- **`photos/describe.py`** — pure functions: `load_qwen()`, `describe_photo()`, `_build_schema()`
- **`cmd_describe` in `photos.py`** — subcommand handler: loads model once, iterates undescribed photos, stores results

**JSON output strategy:**
- **Qwen (local):** prompt-based with retry loop — ask for JSON, try to parse, if invalid retry up to 3 times with feedback showing Qwen exactly what it got wrong. After 3 failures store nulls and set `described_at`.
- **Claude CLI hooks (future, Phase 2+):** recommendation review steps (deletion suggestions, album names, burst picks) will surface via Claude Code hooks so the user has final say before any action is taken

DB change: add `caption`, `quality`, `scene`, `people`, `described_at` columns to the `photos` table. `_init_db()` handles migration for existing DBs.

---

## Subcommand Interface

```
photos.py describe --db photos.db [--force]
```

- `--db` — path to SQLite DB (default: `photos.db`)
- `--force` — re-describe photos that already have `described_at` set

---

## DB Schema Change

Five new columns added to `photos` table:

```sql
ALTER TABLE photos ADD COLUMN caption      TEXT;
ALTER TABLE photos ADD COLUMN quality      TEXT;
ALTER TABLE photos ADD COLUMN scene        TEXT;
ALTER TABLE photos ADD COLUMN people       TEXT;
ALTER TABLE photos ADD COLUMN described_at INTEGER;
```

Applied in `_init_db()` via migration: check `PRAGMA table_info(photos)` and `ALTER TABLE` for each missing column.

`described_at` is a Unix timestamp (set when Qwen runs, even on failure). This is the resumability key — `cmd_describe` skips photos where `described_at IS NOT NULL` unless `--force` is passed.

---

## Qwen Prompt and Retry Loop

**Prompt:**
```
Describe this photo. Reply with ONLY this JSON — no markdown, no extra text:
{"caption": "one sentence describing the main subject and what is happening",
 "quality": "one word: good, blurry, dark, overexposed, or obstructed",
 "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",
 "people": "one word: none, one, few, or many"}
```

**Retry loop (up to 3 attempts):**
If the response cannot be parsed as valid JSON with the required fields, retry with feedback:
```
Your response was not valid JSON. Required format:
{"caption": "...", "quality": "good|blurry|dark|overexposed|obstructed", "scene": "...", "people": "none|one|few|many"}
Your response was: <previous raw output>
Please try again with ONLY the JSON object.
```
After 3 failed attempts: store all fields as NULL, set `described_at` to now, log a warning.

---

## `photos/describe.py` Functions

```python
def load_qwen() -> tuple[model, processor]:
    """Load Qwen2.5-VL-7B-Instruct 4-bit quantized. ~30s, ~5GB VRAM."""

def describe_photo(model, processor, path: Path) -> dict:
    """Run Qwen on a single photo with up to 3 retry attempts on invalid JSON.
    Returns {caption, quality, scene, people}.
    HEIC files are converted to a JPEG temp file via pillow-heif before inference.
    Returns dict with all None values on any failure (file missing, CUDA OOM, 3 parse failures)."""

def _parse_describe_json(raw: str) -> dict | None:
    """Try to parse Qwen output as valid JSON with required fields.
    Returns dict on success, None on failure (caller handles retry)."""
```

---

## `cmd_describe` Behaviour

1. Call `_init_db(args.db)` — runs migration.
2. Load Qwen model once (log loading time).
3. Query: `SELECT id, path FROM photos WHERE discarded = 0 AND described_at IS NULL` (or all non-discarded if `--force`).
4. For each photo:
   - If file does not exist on disk → skip silently (do not set `described_at`).
   - Call `describe_photo(model, processor, path)`.
   - `UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?`
   - `conn.commit()` immediately — progress survives interruption.
5. Show tqdm progress bar with current filename.
6. Print: `✓ Described N photo(s)`

---

## HEIC Handling

`pillow-heif` is registered at import time:

```python
from pillow_heif import register_heif_opener
register_heif_opener()
```

After registration, `Image.open()` handles `.heic`/`.HEIC` files transparently. For Qwen inference, HEIC files are saved to a `tempfile.NamedTemporaryFile` as JPEG before being passed to the processor.

---

## Error Handling

| Situation | Behaviour |
|-----------|-----------|
| File not found on disk | Skip silently, leave `described_at` NULL |
| Qwen returns malformed JSON | Store all fields as NULL, set `described_at` to now |
| HEIC conversion fails | Store all fields as NULL, set `described_at` to now, log warning |
| CUDA OOM | Crash with informative message — user should free VRAM and retry |

---

## Dependencies

Add to `requirements.txt`:
```
pillow-heif>=0.13.0
```

---

## Testing

### `tests/test_describe.py` — unit tests (no GPU required)

- `test_parse_describe_json_valid` — valid JSON → correct dict
- `test_parse_describe_json_missing_fields` — JSON missing required fields → returns None (triggers retry)
- `test_parse_describe_json_malformed` — non-JSON string → returns None
- `test_parse_describe_json_strips_markdown` — ` ```json\n{...}\n``` ` → correctly parsed
- `test_db_migration_adds_describe_columns` — old DB without describe columns → `_init_db` adds all 5
- `test_describe_skips_missing_file` — photo with non-existent path → skipped, `described_at` stays NULL

### `tests/test_describe_cmd.py` — GPU integration test (skipped if no CUDA)

```python
@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires GPU")
def test_describe_real_photo(tmp_path):
    # copies one real JPG from output/takeout, runs describe, checks caption is non-null
```

---

## Out of Scope

- Batch inference (multiple photos per Qwen call) — single-photo inference is simpler and sufficient
- Embedding-based semantic search — Phase 3
- Near-duplicate detection — Phase 2
- Album naming — Phase 2
- Deletion suggestions — Phase 2
