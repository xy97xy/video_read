# Photos Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `photos.py dedup` subcommand that auto-removes exact duplicates and interactively trims burst groups, storing discard state in the SQLite DB.

**Architecture:** Pure functions (`photos/dedup.py`) handle hashing and grouping; `cmd_dedup` in `photos.py` orchestrates both passes and owns user interaction. A `discarded` DB column gates `cmd_organize` from copying discarded photos.

**Tech Stack:** Python 3.10+, SQLite3, hashlib (MD5), pathlib, argparse, subprocess (tests)

---

## File Map

| File | Action | What changes |
|------|--------|--------------|
| `photos.py` | Modify | `_init_db` adds `discarded` column + migration; new `cmd_dedup`; `cmd_organize` filters discarded; `main()` registers `dedup` subcommand |
| `photos/dedup.py` | Create | Pure functions: `hash_file`, `find_exact_duplicates`, `find_burst_groups` |
| `tests/test_dedup.py` | Create | Unit tests for migration + all three pure functions |
| `tests/test_dedup_cmd.py` | Create | Integration tests via subprocess: exact-dups, k/p/s burst actions, organize skips discarded |
| `tests/test_organize.py` | Modify | Add `discarded INTEGER DEFAULT 0` to raw `CREATE TABLE` in `_setup` |

---

## Task 1: DB Schema Migration

**Files:**
- Modify: `photos.py:17-31` (`_init_db`)
- Test: `tests/test_dedup.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_dedup.py`:

```python
import os, sys, sqlite3
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def test_migration_adds_discarded_column(tmp_path):
    db = str(tmp_path / "photos.db")
    # Simulate old DB without discarded column
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER
    )""")
    conn.commit()
    conn.close()

    from photos import _init_db
    conn2 = _init_db(db)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(photos)")}
    conn2.close()
    assert "discarded" in cols


def test_new_db_has_discarded_column(tmp_path):
    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    conn.close()
    assert "discarded" in cols


def test_discarded_defaults_to_zero(tmp_path):
    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    conn.execute("INSERT INTO photos (path, taken_at) VALUES (?,?)", (str(f), 1000))
    conn.commit()
    val = conn.execute("SELECT discarded FROM photos").fetchone()[0]
    conn.close()
    assert val == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/xiaoyu/git/video_read && python -m pytest tests/test_dedup.py -v
```

Expected: FAIL — `ImportError` or `OperationalError: no such column: discarded`

- [ ] **Step 3: Update `_init_db` in `photos.py`**

Replace the existing `_init_db` (lines 17–31) with:

```python
def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id         INTEGER PRIMARY KEY,
            path       TEXT UNIQUE,
            taken_at   INTEGER,
            lat        REAL,
            lon        REAL,
            place      TEXT,
            cluster_id INTEGER,
            discarded  INTEGER DEFAULT 0
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    if "discarded" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN discarded INTEGER DEFAULT 0")
    conn.commit()
    return conn
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_dedup.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Full suite — ensure nothing broken**

```bash
python -m pytest -v
```

Expected: all previously passing tests still pass (53+3 total)

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_dedup.py
git commit -m "feat: add discarded column to photos DB schema with migration"
```

---

## Task 2: Pure Dedup Functions

**Files:**
- Create: `photos/dedup.py`
- Test: `tests/test_dedup.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dedup.py`:

```python
from photos.dedup import hash_file, find_exact_duplicates, find_burst_groups


# --- hash_file ---

def test_hash_file_returns_32_char_hex(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"hello")
    result = hash_file(f)
    assert len(result) == 32
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_file_same_content_same_hash(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"hello")
    b = tmp_path / "b.jpg"; b.write_bytes(b"hello")
    assert hash_file(a) == hash_file(b)


def test_hash_file_different_content_different_hash(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"hello")
    b = tmp_path / "b.jpg"; b.write_bytes(b"world")
    assert hash_file(a) != hash_file(b)


# --- find_exact_duplicates ---

def test_find_exact_duplicates_empty_for_unique_files(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"aaa")
    b = tmp_path / "b.jpg"; b.write_bytes(b"bbb")
    photos = [{"id": 1, "path": str(a)}, {"id": 2, "path": str(b)}]
    assert find_exact_duplicates(photos) == []


def test_find_exact_duplicates_groups_identical_files(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"same")
    b = tmp_path / "b.jpg"; b.write_bytes(b"same")
    photos = [{"id": 1, "path": str(a)}, {"id": 2, "path": str(b)}]
    result = find_exact_duplicates(photos)
    assert len(result) == 1
    assert len(result[0]) == 2


def test_find_exact_duplicates_skips_missing_files(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"same")
    photos = [
        {"id": 1, "path": str(a)},
        {"id": 2, "path": "/nonexistent/ghost.jpg"},
    ]
    assert find_exact_duplicates(photos) == []


def test_find_exact_duplicates_three_copies_one_group(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"dup")
    b = tmp_path / "b.jpg"; b.write_bytes(b"dup")
    c = tmp_path / "c.jpg"; c.write_bytes(b"dup")
    photos = [{"id": 1, "path": str(a)}, {"id": 2, "path": str(b)}, {"id": 3, "path": str(c)}]
    result = find_exact_duplicates(photos)
    assert len(result) == 1
    assert len(result[0]) == 3


# --- find_burst_groups ---

def test_find_burst_groups_single_photo_not_grouped():
    photos = [{"id": 1, "path": "a.jpg", "taken_at": 1000}]
    assert find_burst_groups(photos) == []


def test_find_burst_groups_within_window():
    photos = [
        {"id": 1, "path": "a.jpg", "taken_at": 1000},
        {"id": 2, "path": "b.jpg", "taken_at": 1002},
        {"id": 3, "path": "c.jpg", "taken_at": 1003},
    ]
    result = find_burst_groups(photos, window_seconds=3)
    assert len(result) == 1
    assert len(result[0]) == 3


def test_find_burst_groups_splits_on_large_gap():
    photos = [
        {"id": 1, "path": "a.jpg", "taken_at": 1000},
        {"id": 2, "path": "b.jpg", "taken_at": 1001},
        {"id": 3, "path": "c.jpg", "taken_at": 2000},
        {"id": 4, "path": "d.jpg", "taken_at": 2002},
    ]
    result = find_burst_groups(photos, window_seconds=3)
    assert len(result) == 2
    assert len(result[0]) == 2
    assert len(result[1]) == 2


def test_find_burst_groups_skips_no_taken_at():
    photos = [
        {"id": 1, "path": "a.jpg", "taken_at": None},
        {"id": 2, "path": "b.jpg", "taken_at": None},
    ]
    assert find_burst_groups(photos) == []


def test_find_burst_groups_exactly_at_window_boundary():
    photos = [
        {"id": 1, "path": "a.jpg", "taken_at": 1000},
        {"id": 2, "path": "b.jpg", "taken_at": 1003},  # gap = 3 = window (inclusive)
    ]
    result = find_burst_groups(photos, window_seconds=3)
    assert len(result) == 1

    photos2 = [
        {"id": 1, "path": "a.jpg", "taken_at": 1000},
        {"id": 2, "path": "b.jpg", "taken_at": 1004},  # gap = 4 > window
    ]
    assert find_burst_groups(photos2, window_seconds=3) == []
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_dedup.py -v
```

Expected: 12 FAIL — `ModuleNotFoundError: No module named 'photos.dedup'`

- [ ] **Step 3: Create `photos/dedup.py`**

```python
from __future__ import annotations
import hashlib
from collections import defaultdict
from pathlib import Path


def hash_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_exact_duplicates(photos: list[dict]) -> list[list[dict]]:
    by_size: dict[int, list[dict]] = defaultdict(list)
    for p in photos:
        path = Path(p["path"])
        if not path.exists():
            continue
        by_size[path.stat().st_size].append(p)

    by_hash: dict[str, list[dict]] = defaultdict(list)
    for size_group in by_size.values():
        if len(size_group) < 2:
            continue
        for p in size_group:
            h = hash_file(Path(p["path"]))
            by_hash[h].append(p)

    return [g for g in by_hash.values() if len(g) >= 2]


def find_burst_groups(photos: list[dict], window_seconds: int = 3) -> list[list[dict]]:
    dated = sorted(
        [p for p in photos if p.get("taken_at") is not None],
        key=lambda p: p["taken_at"],
    )
    if not dated:
        return []

    groups: list[list[dict]] = []
    current = [dated[0]]
    for p in dated[1:]:
        if p["taken_at"] - current[-1]["taken_at"] <= window_seconds:
            current.append(p)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [p]
    if len(current) >= 2:
        groups.append(current)
    return groups
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_dedup.py -v
```

Expected: all 15 PASS

- [ ] **Step 5: Full suite**

```bash
python -m pytest -v
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add photos/dedup.py tests/test_dedup.py
git commit -m "feat: add photos/dedup.py pure functions (hash_file, find_exact_duplicates, find_burst_groups)"
```

---

## Task 3: cmd_dedup Pass 1 — Exact Duplicates

**Files:**
- Modify: `photos.py` (add `cmd_dedup`, import `find_exact_duplicates`, register subcommand)
- Test: `tests/test_dedup_cmd.py` (new file)

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_dedup_cmd.py`:

```python
import json, sqlite3, subprocess, sys, os
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

from photos import _init_db


def _make_db(tmp_path, photos_data):
    """photos_data: list of (path_obj, taken_at_int) — all unique content assumed."""
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    for i, (p, t) in enumerate(photos_data, 1):
        conn.execute(
            "INSERT INTO photos (id, path, taken_at) VALUES (?,?,?)",
            (i, str(p), t),
        )
    conn.commit()
    conn.close()
    return db


def _discarded_map(db):
    conn = sqlite3.connect(db)
    result = {r[0]: r[1] for r in conn.execute("SELECT id, discarded FROM photos")}
    conn.close()
    return result


def test_exact_duplicates_auto_discarded(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"same content")
    b = tmp_path / "b.jpg"; b.write_bytes(b"same content")  # duplicate of a
    c = tmp_path / "c.jpg"; c.write_bytes(b"different")

    # taken_at spaced 1000s apart so no burst groups form
    db = _make_db(tmp_path, [(a, 1000), (b, 2000), (c, 3000)])

    result = subprocess.run(
        [sys.executable, "photos.py", "dedup", "--db", db],
        input="",
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    assert "Auto-discarded 1" in result.stdout

    disc = _discarded_map(db)
    assert disc[1] == 0  # id=1 kept (lowest id in dup group)
    assert disc[2] == 1  # id=2 discarded
    assert disc[3] == 0  # different file, untouched
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_dedup_cmd.py::test_exact_duplicates_auto_discarded -v
```

Expected: FAIL — `photos.py: error: argument subcommand: invalid choice: 'dedup'`

- [ ] **Step 3: Add `cmd_dedup` (Pass 1 only) and register in `main()`**

Add import at top of `photos.py` (after the existing cluster import):

```python
from photos.dedup import find_exact_duplicates, find_burst_groups
```

Add `cmd_dedup` function before `main()` in `photos.py`:

```python
def cmd_dedup(args):
    from datetime import datetime

    conn = _init_db(args.db)

    # Pass 1: exact duplicates
    rows = conn.execute(
        "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
    ).fetchall()
    photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows]

    dup_groups = find_exact_duplicates(photos)
    n_auto = 0
    for group in dup_groups:
        keep_id = min(p["id"] for p in group)
        for p in group:
            if p["id"] != keep_id:
                conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                n_auto += 1
    conn.commit()
    print(f"✓ Auto-discarded {n_auto} exact duplicate(s)")

    # Pass 2: burst groups (placeholder — implemented in Task 4)
    rows = conn.execute(
        "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
    ).fetchall()
    photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows]
    burst_groups = find_burst_groups(photos, window_seconds=args.burst_window)
    print(f"\n✓ Kept 0, discarded 0 across {len(burst_groups)} burst group(s)")
    conn.close()
```

In `main()`, add the dedup subparser before `args = p.parse_args()`:

```python
    d = sub.add_parser("dedup", help="Remove exact duplicates and thin burst shots")
    d.add_argument("--db", default="photos.db", metavar="DB")
    d.add_argument("--burst-window", type=int, default=3, metavar="SEC")
```

Update the dispatch dict in `main()`:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup}[args.subcommand](args)
```

- [ ] **Step 4: Run the integration test**

```bash
python -m pytest tests/test_dedup_cmd.py::test_exact_duplicates_auto_discarded -v
```

Expected: PASS

- [ ] **Step 5: Full suite**

```bash
python -m pytest -v
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_dedup_cmd.py
git commit -m "feat: cmd_dedup Pass 1 — auto-discard exact duplicates"
```

---

## Task 4: cmd_dedup Pass 2 — Burst Groups UI

**Files:**
- Modify: `photos.py` (`cmd_dedup` — replace the Pass 2 placeholder)
- Test: `tests/test_dedup_cmd.py` (append 3 tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dedup_cmd.py`:

```python
def _make_burst_db(tmp_path):
    """3 photos in a burst (taken_at 1000/1002/1004). Sizes: 1000B, 3000B (largest=recommended), 2000B."""
    a = tmp_path / "a.jpg"; a.write_bytes(b"a" * 1000)   # 1000 bytes
    b = tmp_path / "b.jpg"; b.write_bytes(b"b" * 3000)   # 3000 bytes — LARGEST = recommended
    c = tmp_path / "c.jpg"; c.write_bytes(b"c" * 2000)   # 2000 bytes
    return _make_db(tmp_path, [(a, 1000), (b, 1002), (c, 1004)])


def test_burst_k_keeps_recommended(tmp_path):
    db = _make_burst_db(tmp_path)
    result = subprocess.run(
        [sys.executable, "photos.py", "dedup", "--db", db],
        input="k\n",
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    disc = _discarded_map(db)
    assert disc[1] == 1  # a.jpg (1000B) — discarded
    assert disc[2] == 0  # b.jpg (3000B) — kept (recommended = largest)
    assert disc[3] == 1  # c.jpg (2000B) — discarded


def test_burst_p_keeps_chosen(tmp_path):
    db = _make_burst_db(tmp_path)
    # Pick photo 1 (first in taken_at order = a.jpg, 1000B)
    result = subprocess.run(
        [sys.executable, "photos.py", "dedup", "--db", db],
        input="p\n1\n",
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    disc = _discarded_map(db)
    assert disc[1] == 0  # a.jpg — kept (user picked #1)
    assert disc[2] == 1  # b.jpg — discarded
    assert disc[3] == 1  # c.jpg — discarded


def test_burst_s_skips_group(tmp_path):
    db = _make_burst_db(tmp_path)
    result = subprocess.run(
        [sys.executable, "photos.py", "dedup", "--db", db],
        input="s\n",
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    disc = _discarded_map(db)
    assert disc[1] == 0
    assert disc[2] == 0
    assert disc[3] == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_dedup_cmd.py::test_burst_k_keeps_recommended tests/test_dedup_cmd.py::test_burst_p_keeps_chosen tests/test_dedup_cmd.py::test_burst_s_skips_group -v
```

Expected: 3 FAIL — burst groups are found but no interactive handling (current code prints placeholder and closes)

- [ ] **Step 3: Replace the Pass 2 placeholder in `cmd_dedup`**

In `photos.py`, replace the Pass 2 block in `cmd_dedup` (everything from `# Pass 2: burst groups (placeholder` through `conn.close()`) with:

```python
    # Pass 2: burst groups
    rows = conn.execute(
        "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
    ).fetchall()
    photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows]
    burst_groups = find_burst_groups(photos, window_seconds=args.burst_window)

    def _file_size(p: dict) -> int:
        try:
            return Path(p["path"]).stat().st_size
        except OSError:
            return 0

    n_kept = n_discarded_burst = 0
    for group in burst_groups:
        recommended = max(group, key=_file_size)

        print(f"\nBurst group ({len(group)} photos):")
        for i, p in enumerate(group, 1):
            size_mb = _file_size(p) / 1_048_576
            dt_str = (
                datetime.fromtimestamp(p["taken_at"]).strftime("%Y-%m-%d %H:%M:%S")
                if p["taken_at"] else ""
            )
            arrow = "  ← recommended" if p["id"] == recommended["id"] else ""
            print(f"  {i}. {Path(p['path']).name}  {size_mb:.1f} MB  {dt_str}{arrow}")
        print("Actions: [k]eep recommended  [p]ick different  [s]kip  [?]help")

        while True:
            try:
                action = input("> ").strip().lower()
            except EOFError:
                print("\nAborted.")
                conn.close()
                return

            if action == "k":
                for p in group:
                    if p["id"] != recommended["id"]:
                        conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                        n_discarded_burst += 1
                conn.commit()
                n_kept += 1
                break
            elif action == "p":
                try:
                    choice_str = input(f"  Keep which? (1-{len(group)}): ").strip()
                except EOFError:
                    print("\nAborted.")
                    conn.close()
                    return
                try:
                    idx = int(choice_str) - 1
                    if 0 <= idx < len(group):
                        chosen = group[idx]
                        for p in group:
                            if p["id"] != chosen["id"]:
                                conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                                n_discarded_burst += 1
                        conn.commit()
                        n_kept += 1
                        break
                    else:
                        print(f"  Invalid. Enter 1–{len(group)}.")
                except ValueError:
                    print(f"  Invalid. Enter 1–{len(group)}.")
            elif action == "s":
                break
            elif action == "?":
                print("  k = keep recommended (largest file), discard others")
                print("  p = pick a different photo to keep")
                print("  s = skip this group (keep all)")
                print("  ? = show this help")
            else:
                print("  Unknown action. Type ? for help.")

    print(f"\n✓ Kept {n_kept}, discarded {n_discarded_burst} across {len(burst_groups)} burst group(s)")
    conn.close()
```

Also add `from datetime import datetime` inside `cmd_dedup` (at the top of the function, it's already there from Task 3).

- [ ] **Step 4: Run the burst tests**

```bash
python -m pytest tests/test_dedup_cmd.py -v
```

Expected: all 4 PASS

- [ ] **Step 5: Full suite**

```bash
python -m pytest -v
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_dedup_cmd.py
git commit -m "feat: cmd_dedup Pass 2 — interactive burst group thinning"
```

---

## Task 5: cmd_organize Skips Discarded

**Files:**
- Modify: `photos.py` (`cmd_organize` — use `_init_db`, add `WHERE discarded = 0`)
- Modify: `tests/test_organize.py` (`_setup` — add `discarded` column)
- Test: `tests/test_dedup_cmd.py` (append 1 test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dedup_cmd.py`:

```python
def test_organize_skips_discarded(tmp_path):
    a = tmp_path / "keep.jpg";    a.write_bytes(b"keep this")
    b = tmp_path / "discard.jpg"; b.write_bytes(b"throw away")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, cluster_id, discarded) VALUES (1,?,1000,1,0)",
        (str(a),),
    )
    conn.execute(
        "INSERT INTO photos (id, path, taken_at, cluster_id, discarded) VALUES (2,?,1001,1,1)",
        (str(b),),
    )
    conn.commit()
    conn.close()

    clusters = [{
        "id": 1, "name": "Test Trip", "is_trip": True, "confirmed": True,
        "photo_count": 2, "photo_ids": [1, 2],
        "start": "2024-01-01", "end": "2024-01-02", "place": None,
    }]
    clusters_path = str(tmp_path / "clusters.json")
    open(clusters_path, "w").write(json.dumps(clusters))

    out = str(tmp_path / "out")
    result = subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", out, "--db", db, "--clusters", clusters_path],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr

    files = list((Path(out) / "Test-Trip").iterdir())
    assert len(files) == 1
    assert files[0].name == "keep.jpg"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_dedup_cmd.py::test_organize_skips_discarded -v
```

Expected: FAIL — `OperationalError: no such column: discarded` (because `cmd_organize` uses raw `sqlite3.connect`, not `_init_db`)

- [ ] **Step 3: Update `cmd_organize` in `photos.py`**

In `cmd_organize`, replace:
```python
    conn = sqlite3.connect(args.db)
    id_to_path = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM photos")}
    conn.close()
```
with:
```python
    conn = _init_db(args.db)
    id_to_path = {r[0]: r[1] for r in conn.execute(
        "SELECT id, path FROM photos WHERE discarded = 0"
    )}
    conn.close()
```

- [ ] **Step 4: Update `_setup` in `tests/test_organize.py`**

In `tests/test_organize.py`, in the `_setup` function, replace the `CREATE TABLE` statement:
```python
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER
    )""")
```
with:
```python
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER,
        discarded INTEGER DEFAULT 0
    )""")
```

- [ ] **Step 5: Run the new test**

```bash
python -m pytest tests/test_dedup_cmd.py::test_organize_skips_discarded -v
```

Expected: PASS

- [ ] **Step 6: Full suite — verify test_organize.py still passes**

```bash
python -m pytest -v
```

Expected: all passing (previously 53 + 15 new = 68 total)

- [ ] **Step 7: Commit**

```bash
git add photos.py tests/test_organize.py tests/test_dedup_cmd.py
git commit -m "feat: cmd_organize skips discarded photos"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Pass 1: exact duplicates auto-discarded (Task 3)
- ✅ Pass 2: burst groups interactive k/p/s/? (Task 4)
- ✅ `--db` and `--burst-window` args (Task 3)
- ✅ Recommended = largest file (Task 4 `_file_size`)
- ✅ Keep lowest id in exact-dup group (Task 3)
- ✅ Files not on disk skipped silently — `find_exact_duplicates` checks `path.exists()` (Task 2)
- ✅ DB schema migration for existing DBs (Task 1)
- ✅ `cmd_organize` skips discarded (Task 5)
- ✅ EOFError → save and exit cleanly (Task 4 — both input() calls wrapped)
- ✅ Print `✓ Auto-discarded N exact duplicate(s)` (Task 3)
- ✅ Print `✓ Kept N, discarded M across B burst group(s)` (Task 4)
- ✅ Unit tests: hash_file, find_exact_duplicates, find_burst_groups (Task 2)
- ✅ Integration tests: exact-dup, k, p, s, organize-skips-discarded (Tasks 3–5)
