# Photos Search — Design

**Date:** 2026-06-01
**Status:** Approved

## Goal

Add `photos.py search <query>` — full-text search over Qwen-generated photo descriptions
already in the DB. Results are printed to the terminal and saved as a markdown file for
later reference.

---

## Pipeline Position

```
scan → describe → dedup → cluster → review → recommend → [flag] → organize
                                                  ↑
                                            search (any time after describe)
```

Search is read-only and can be run at any point after `describe` has populated captions.

---

## Architecture

Two new pieces:

- **`photos/search.py`** — `build_fts(conn)`, `search_photos(conn, query, limit) -> list[dict]`
- **`photos.py`** — `cmd_search`, argparse wiring

No new dependencies — SQLite FTS5 is built into Python's `sqlite3` module.

---

## FTS5 Index

A virtual table `photos_fts` is created in `photos.db`:

```sql
CREATE VIRTUAL TABLE photos_fts USING fts5(
    caption, scene, people, place,
    content='photos',
    content_rowid='id'
)
```

`build_fts(conn)` drops and recreates this table on every `search` call, then populates it:

```sql
INSERT INTO photos_fts(rowid, caption, scene, people, place)
SELECT id, caption, scene, people, place
FROM photos
WHERE discarded=0 AND described_at IS NOT NULL
```

Rebuilding is instant at ~200 rows and avoids stale index issues after new `describe` runs.

---

## Subcommand: `search`

```
python photos.py search <query> [--db photos.db] [--limit 20] [--output output/search-results.md]
```

### Query execution

```sql
SELECT p.id, p.path, p.scene, p.place, p.caption, p.cluster_id,
       bm25(photos_fts) AS score
FROM photos_fts
JOIN photos p ON photos_fts.rowid = p.id
WHERE photos_fts MATCH ?
ORDER BY score
LIMIT ?
```

`bm25()` returns negative values in FTS5 (lower = better match) — results are sorted
ascending so the best matches appear first.

### Terminal output

```
Found 8 photo(s) matching "mountains at sunset"

 id  | score  | file              | scene          | place       | caption
-----|--------|-------------------|----------------|-------------|-----------------------------------
 197 | -8.21  | IMG_1675.HEIC     | mountain trail | Zion NP     | A person climbing a large rock...
 176 | -6.10  | IMG_1674.HEIC     | mountain trail | Zion NP     | Hiker standing on a red rock...
 ...

Saved to output/search-results.md
```

### Markdown output

Same content written to `--output` path (default `output/search-results.md`):

```markdown
# Search Results: "mountains at sunset"
Generated: 2026-06-01  |  8 results

| id | score | file | scene | place | caption |
|----|-------|------|-------|-------|---------|
| 197 | -8.21 | IMG_1675.HEIC | mountain trail | Zion NP | A person climbing... |
...
```

Parent directories created automatically.

---

## Error Handling

- **No results:** Print `No photos found matching "<query>"` — exit cleanly, no file written
- **No described photos:** Print `⚠ No photos have been described yet. Run: python photos.py describe --db <db>` — exit cleanly
- **FTS5 syntax error** (e.g. unmatched quotes): Catch `sqlite3.OperationalError`, print friendly message and raw query for debugging

---

## Testing

`tests/test_search.py`:
- `test_build_fts_creates_table` — table exists after build_fts
- `test_build_fts_excludes_discarded` — discarded photos not in index
- `test_build_fts_excludes_undescribed` — photos with described_at=NULL not in index
- `test_build_fts_is_idempotent` — calling build_fts twice does not error
- `test_search_photos_returns_matches` — query matching a known caption returns that photo
- `test_search_photos_respects_limit` — limit=1 returns exactly 1 result
- `test_search_photos_returns_empty_for_no_match` — no-match query returns []
- `test_search_photos_matches_scene_field` — query matching scene (not caption) finds photo
- `test_search_photos_matches_place_field` — query matching place finds photo

`tests/test_search_cmd.py`:
- `test_cmd_search_prints_results` — output contains photo id and filename
- `test_cmd_search_writes_markdown_file` — output file created with results
- `test_cmd_search_no_results` — no-match query exits cleanly, no file written
- `test_cmd_search_no_descriptions` — warns when no described photos

---

## Hard Constraints

- Read-only — never modifies `photos` table
- FTS index rebuilt from scratch on every call (no incremental sync)
- Videos (`.MOV`, `.MP4` etc.) are excluded automatically because they have `described_at=NULL`
