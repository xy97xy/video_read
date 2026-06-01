# Photos Recommend & Flag — Design

**Date:** 2026-05-31
**Status:** Approved

## Goal

After `describe` runs Qwen captions on every photo, use those descriptions to surface
low-quality and redundant photos for human review. Photos are never deleted — they are
flagged in the DB and copied to `output/to-review/` so the user can browse and decide.

The review workflow is chat-driven: the user opens a new Claude Code session, Claude reads
`output/recommendations.md`, and the user gives natural-language instructions ("flag all
blurry ones in the hiking cluster"). Claude then runs `photos.py flag <ids>` directly.

---

## Pipeline Position

```
scan → describe → dedup → cluster → review [--yes] → recommend → [chat: flag] → organize
```

---

## DB Change

Add `flagged INTEGER DEFAULT 0` to the `photos` table via the existing migration pattern.

- `discarded=1` — set by `dedup`, means exact duplicate; excluded from all downstream steps
- `flagged=1`   — set by `recommend` (pass 1) or `flag`; means candidate for deletion; still
                  included in `organize` unless the user also marks it discarded

---

## Subcommand: `recommend`

```
python photos.py recommend [--db photos.db] [--clusters clusters.json] [--output recommendations.md]
```

### Pass 1 — Auto-flag bad quality (automatic)

```sql
UPDATE photos SET flagged=1
WHERE quality != 'good' AND discarded=0 AND flagged=0
```

Prints: `✓ Auto-flagged N photo(s) with quality: blurry/dark/overexposed/obstructed`

### Pass 2 — Write cluster summary (read-only)

Query all non-discarded photos, join cluster names from `clusters.json`, group by cluster.
Write `output/recommendations.md` (default path, overridable with `--output`).

**Report structure:**

```markdown
# Photo Recommendations
Generated: 2026-05-31  |  347 photos  |  N flagged by quality pass

## 2024-07-Zion-National-Park (42 photos)

| id  | file              | quality | scene         | people | caption                                      |
|-----|-------------------|---------|---------------|--------|----------------------------------------------|
| 12  | IMG_1234.jpg      | good    | mountain trail | none  | Hiker standing on a red rock overlook ...    |
| 45  | IMG_1235.jpg 🚩   | blurry  | mountain trail | one   | Out-of-focus shot of a trail sign            |
...

## Unclustered (23 photos)
...
```

`🚩` marks already-flagged photos (from pass 1 or prior runs) so they stand out.

Prints path to the report when done.

---

## Subcommand: `flag`

```
python photos.py flag <id> [id ...] [--db photos.db] [--clusters clusters.json] [--output-dir output/to-review]
```

1. Validate: IDs must exist and not be `discarded=1` (warn and skip if already discarded)
2. `UPDATE photos SET flagged=1 WHERE id IN (...)`
3. For each photo: resolve cluster name → copy file to `output/to-review/<cluster-name>/<filename>`
   - Photos with no cluster go to `output/to-review/unclustered/`
   - Use the same `_dest_path` collision-avoidance logic as `organize`
4. Print: `✓ Flagged N photo(s), copies in output/to-review/`

Unflagging: `photos.py flag --unflag <id> [id ...]` sets `flagged=0` and removes the copy
from `output/to-review/` if it exists.

---

## Error Handling

- `recommend` with no described photos: warn and exit cleanly ("Run describe first")
- `flag` with an unknown ID: print warning, skip, continue with valid IDs
- Copy failure (disk full, src missing): print warning, skip, DB flag still applied

---

## Testing

- `tests/test_recommend.py` — unit tests:
  - Pass 1 auto-flags correct rows (quality != 'good', respects discarded=0)
  - Pass 1 is idempotent (re-running doesn't double-flag)
  - Markdown report includes all clusters, correct row counts, 🚩 on flagged photos
  - Report handles photos with no cluster (unclustered section)
- `tests/test_flag_cmd.py` — unit tests:
  - Sets flagged=1 in DB for valid IDs
  - Copies files to correct `to-review/<cluster>/` paths
  - Skips discarded photos with warning
  - Skips unknown IDs with warning
  - `--unflag` sets flagged=0 and removes copy

---

## Hard Constraints

- Never delete original files
- `organize` continues to use `discarded=0` filter only — flagged photos still appear in organized output unless explicitly discarded
- All file operations are copies, not moves
