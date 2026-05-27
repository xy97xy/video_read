# Photos Dedup Design

**Goal:** Add a `photos.py dedup` subcommand that removes exact duplicates automatically and guides the user through keeping the best shot from burst groups.

**Pipeline position:** `scan → dedup → cluster → review → organize`

---

## Architecture

Two new components:

- **`photos/dedup.py`** — pure functions: `hash_file()`, `find_exact_duplicates()`, `find_burst_groups()`
- **`cmd_dedup` in `photos.py`** — subcommand handler: runs both passes, handles user interaction for bursts

DB change: add `discarded INTEGER DEFAULT 0` column to the `photos` table. `cmd_organize` skips photos where `discarded = 1`.

---

## Subcommand Interface

```
photos.py dedup --db photos.db [--burst-window 3]
```

- `--db` — path to SQLite DB (default: `photos.db`)
- `--burst-window` — seconds window for burst detection (default: 3)

---

## Pass 1: Exact Duplicates

1. Load all non-discarded photos from DB with a valid `path`.
2. Pre-group by file size (fast, no I/O) — only files with the same size can be duplicates.
3. For each size group with 2+ files, compute MD5 of file contents.
4. Within each hash group, keep the photo with the lowest `id` (earliest scan); mark the rest `discarded = 1` in DB.
5. Print: `✓ Auto-discarded N exact duplicate(s)`

Files that no longer exist on disk are skipped silently (not marked discarded — they may have been on a device that's now unmounted).

---

## Pass 2: Burst Groups

1. From remaining non-discarded photos, sort by `taken_at`.
2. Group consecutive photos where the gap between any two adjacent photos ≤ `burst_window` seconds.
3. Skip groups of 1 (not a burst).
4. For each burst group, print:

```
Burst group (3 photos):
  IMG_4021.JPG  3.2 MB  2024-10-24 10:40:01  ← recommended
  IMG_4022.JPG  2.9 MB  2024-10-24 10:40:02
  IMG_4023.JPG  2.7 MB  2024-10-24 10:40:03
Actions: [k]eep recommended  [p]ick different  [s]kip  [?]help
```

- Recommended = largest file by size.
- `k` — mark all others in group `discarded = 1`, keep recommended.
- `p` — prompt `Keep which? (1/2/3):`, mark the rest `discarded = 1`.
- `s` — skip group, leave all undiscarded.
- `?` — print help, re-prompt.
- EOFError — save progress and exit cleanly.

5. After all groups: print summary `✓ Kept N, discarded M across B burst group(s)`.

---

## DB Schema Change

```sql
ALTER TABLE photos ADD COLUMN discarded INTEGER DEFAULT 0;
```

Applied in `_init_db()` via `CREATE TABLE IF NOT EXISTS` update AND a migration that adds the column if it doesn't exist (for existing DBs).

---

## `cmd_organize` change

Add `WHERE discarded = 0` (or equivalent Python filter) when loading `id_to_path` so discarded photos are excluded from the organized output.

---

## `photos/dedup.py` functions

```python
def hash_file(path: Path) -> str:
    """MD5 hex digest of file contents."""

def find_exact_duplicates(photos: list[dict]) -> list[list[dict]]:
    """Group photos into duplicate sets by (size, MD5). Groups with 1 member excluded."""

def find_burst_groups(photos: list[dict], window_seconds: int = 3) -> list[list[dict]]:
    """Group consecutive photos by taken_at proximity. Groups with 1 member excluded."""
```

---

## Testing

- `tests/test_dedup.py` — unit tests for `hash_file`, `find_exact_duplicates`, `find_burst_groups`
- `tests/test_dedup_cmd.py` — integration tests for `photos.py dedup` subprocess:
  - exact duplicates auto-discarded
  - burst group: `k` keeps recommended, others discarded
  - burst group: `p` keeps chosen, others discarded
  - burst group: `s` leaves all undiscarded
  - `organize` skips discarded photos

---

## Out of Scope

- Visual comparison (user opens files manually)
- Video dedup (same logic applies but not tested separately)
- Undo (re-run `scan` to reset discarded flags)
