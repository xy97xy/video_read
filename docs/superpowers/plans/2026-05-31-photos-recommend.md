# Photos Recommend & Flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `photos.py recommend` (auto-flag bad-quality + write human-readable cluster report) and `photos.py flag` (mark photos flagged in DB + copy to `output/to-review/` for visual browsing).

**Architecture:** Business logic lives in `photos/recommend.py` (pure DB + file ops, no GPU). Both subcommands wire into `photos.py` following the existing `cmd_*` pattern. A new `flagged` DB column (separate from `discarded`) tracks candidates for deletion without ever removing originals.

**Tech Stack:** Python stdlib only (sqlite3, shutil, json, pathlib). Tests use pytest + tmp_path. No new dependencies.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `photos/recommend.py` | `auto_flag_quality`, `build_report`, `_append_table`, `set_flagged` |
| Modify | `photos.py` | add `flagged` migration, `cmd_recommend`, `cmd_flag`, argparse wiring |
| Create | `tests/test_recommend.py` | unit tests for all `photos/recommend.py` functions + `cmd_recommend` |
| Create | `tests/test_flag_cmd.py` | unit tests for `cmd_flag` |

---

## Task 1: Add `flagged` column to DB schema

**Files:**
- Modify: `photos.py` (migrations list, lines 39–49)
- Test: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_recommend.py`:

```python
import os, sys, importlib.util
from pathlib import Path
import sqlite3, json

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_db_has_flagged_column(tmp_path):
    mod = _load_photos_module()
    conn = mod._init_db(str(tmp_path / "photos.db"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    conn.close()
    assert "flagged" in cols


def test_migration_adds_flagged_to_existing_db(tmp_path):
    db = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER,
        discarded INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

    mod = _load_photos_module()
    conn2 = mod._init_db(db)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(photos)")}
    conn2.close()
    assert "flagged" in cols


def test_flagged_defaults_to_zero(tmp_path):
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    conn.execute("INSERT INTO photos (path) VALUES ('/a.jpg')")
    conn.commit()
    val = conn.execute("SELECT flagged FROM photos WHERE path='/a.jpg'").fetchone()[0]
    conn.close()
    assert val == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py::test_new_db_has_flagged_column tests/test_recommend.py::test_migration_adds_flagged_to_existing_db tests/test_recommend.py::test_flagged_defaults_to_zero -v
```

Expected: FAIL — `flagged` column not in schema yet.

- [ ] **Step 3: Add `flagged` migration to `photos.py`**

In `photos.py`, the `migrations` list (around line 40) currently ends with `described_at`. Add `flagged` entry:

```python
    migrations = [
        ("discarded",    "ALTER TABLE photos ADD COLUMN discarded    INTEGER DEFAULT 0"),
        ("caption",      "ALTER TABLE photos ADD COLUMN caption      TEXT"),
        ("quality",      "ALTER TABLE photos ADD COLUMN quality      TEXT"),
        ("scene",        "ALTER TABLE photos ADD COLUMN scene        TEXT"),
        ("people",       "ALTER TABLE photos ADD COLUMN people       TEXT"),
        ("described_at", "ALTER TABLE photos ADD COLUMN described_at INTEGER"),
        ("flagged",      "ALTER TABLE photos ADD COLUMN flagged      INTEGER DEFAULT 0"),
    ]
```

Also add `flagged INTEGER DEFAULT 0` to the `CREATE TABLE IF NOT EXISTS` statement (the column list after `described_at`):

```python
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
            described_at INTEGER,
            flagged      INTEGER DEFAULT 0
        )
    """)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py::test_new_db_has_flagged_column tests/test_recommend.py::test_migration_adds_flagged_to_existing_db tests/test_recommend.py::test_flagged_defaults_to_zero -v
```

Expected: PASS all 3.

- [ ] **Step 5: Commit**

```bash
git add photos.py tests/test_recommend.py
git commit -m "feat: add flagged column to photos DB schema with migration"
```

---

## Task 2: `auto_flag_quality` in `photos/recommend.py`

**Files:**
- Create: `photos/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_recommend.py`:

```python
from photos.recommend import auto_flag_quality


def _make_conn(tmp_path, rows):
    """rows: list of dicts. Required key: id. Optional: path, quality, scene,
    caption, people, cluster_id, discarded, flagged."""
    mod = _load_photos_module()
    db = str(tmp_path / f"photos_{len(rows)}.db")
    conn = mod._init_db(db)
    for r in rows:
        conn.execute(
            "INSERT INTO photos "
            "(id, path, quality, scene, caption, people, cluster_id, discarded, flagged) "
            "VALUES (:id,:path,:quality,:scene,:caption,:people,:cluster_id,:discarded,:flagged)",
            {
                "id": r["id"],
                "path": r.get("path", f"/fake/{r['id']}.jpg"),
                "quality": r.get("quality"),
                "scene": r.get("scene"),
                "caption": r.get("caption"),
                "people": r.get("people"),
                "cluster_id": r.get("cluster_id"),
                "discarded": r.get("discarded", 0),
                "flagged": r.get("flagged", 0),
            },
        )
    conn.commit()
    return conn


def test_auto_flag_quality_flags_non_good(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "blurry"},
        {"id": 2, "quality": "dark"},
        {"id": 3, "quality": "good"},
    ])
    n = auto_flag_quality(conn)
    assert n == 2
    flags = {r[0]: r[1] for r in conn.execute("SELECT id, flagged FROM photos")}
    assert flags[1] == 1
    assert flags[2] == 1
    assert flags[3] == 0
    conn.close()


def test_auto_flag_quality_skips_discarded(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "blurry", "discarded": 1},
    ])
    n = auto_flag_quality(conn)
    assert n == 0
    conn.close()


def test_auto_flag_quality_is_idempotent(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "blurry"},
    ])
    auto_flag_quality(conn)
    n2 = auto_flag_quality(conn)
    assert n2 == 0  # already flagged on first run
    conn.close()


def test_auto_flag_quality_all_quality_values(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "overexposed"},
        {"id": 2, "quality": "obstructed"},
        {"id": 3, "quality": "good"},
        {"id": 4, "quality": None},  # null quality — not flagged
    ])
    n = auto_flag_quality(conn)
    assert n == 2  # only overexposed and obstructed
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "auto_flag" -v
```

Expected: FAIL — `photos/recommend.py` doesn't exist yet.

- [ ] **Step 3: Create `photos/recommend.py` with `auto_flag_quality`**

```python
from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def auto_flag_quality(conn: sqlite3.Connection) -> int:
    """Flag non-good-quality non-discarded photos. Returns count newly flagged."""
    cur = conn.execute(
        "UPDATE photos SET flagged=1 "
        "WHERE quality != 'good' AND discarded=0 AND flagged=0"
    )
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "auto_flag" -v
```

Expected: PASS all 4.

- [ ] **Step 5: Commit**

```bash
git add photos/recommend.py tests/test_recommend.py
git commit -m "feat: add auto_flag_quality to photos/recommend.py"
```

---

## Task 3: `build_report` in `photos/recommend.py`

**Files:**
- Modify: `photos/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_recommend.py` (the `_make_conn` helper from Task 2 is already defined):

```python
from photos.recommend import auto_flag_quality, build_report


def test_build_report_creates_file(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good", "cluster_id": 1,
         "scene": "beach", "caption": "waves", "people": "none"},
    ])
    clusters = [{"id": 1, "name": "Hawaii-2024", "photo_ids": [1]}]
    clusters_path = tmp_path / "clusters.json"
    clusters_path.write_text(json.dumps(clusters))
    out = tmp_path / "recommendations.md"

    result = build_report(conn, clusters_path, out)

    assert result == out
    assert out.exists()
    conn.close()


def test_build_report_marks_flagged_photos(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good", "flagged": 0},
        {"id": 2, "quality": "blurry", "flagged": 1},
    ])
    out = tmp_path / "recommendations.md"
    build_report(conn, None, out)
    text = out.read_text()

    assert "| 1 |" in text
    assert "| 2 🚩 |" in text
    conn.close()


def test_build_report_unclustered_section(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good", "cluster_id": None},
    ])
    out = tmp_path / "recommendations.md"
    build_report(conn, None, out)

    assert "Unclustered" in out.read_text()
    conn.close()


def test_build_report_excludes_discarded(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good"},
        {"id": 2, "quality": "blurry", "discarded": 1},
    ])
    out = tmp_path / "recommendations.md"
    build_report(conn, None, out)
    text = out.read_text()

    assert "fake/2.jpg" not in text
    conn.close()


def test_build_report_uses_cluster_names(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good", "cluster_id": 7},
    ])
    clusters = [{"id": 7, "name": "Iceland-2024", "photo_ids": [1]}]
    clusters_path = tmp_path / "clusters.json"
    clusters_path.write_text(json.dumps(clusters))
    out = tmp_path / "recommendations.md"
    build_report(conn, clusters_path, out)

    assert "Iceland-2024" in out.read_text()
    conn.close()


def test_build_report_creates_parent_dirs(tmp_path):
    conn = _make_conn(tmp_path, [{"id": 1, "quality": "good"}])
    out = tmp_path / "nested" / "deep" / "recommendations.md"
    build_report(conn, None, out)
    assert out.exists()
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "build_report" -v
```

Expected: FAIL — `build_report` not defined yet.

- [ ] **Step 3: Add `build_report` and `_append_table` to `photos/recommend.py`**

```python
def build_report(
    conn: sqlite3.Connection,
    clusters_path: Path | None,
    output_path: Path,
) -> Path:
    """Write cluster-by-cluster markdown report. Returns output_path."""
    rows = conn.execute(
        "SELECT id, path, quality, scene, caption, people, cluster_id, flagged "
        "FROM photos WHERE discarded=0 ORDER BY cluster_id NULLS LAST, id"
    ).fetchall()

    cluster_names: dict[int, str] = {}
    if clusters_path and clusters_path.exists():
        for c in json.loads(clusters_path.read_text()):
            cluster_names[c["id"]] = c["name"]

    total = len(rows)
    n_flagged = sum(1 for r in rows if r[7])

    groups: dict[int | None, list] = defaultdict(list)
    for r in rows:
        groups[r[6]].append(r)

    lines = [
        "# Photo Recommendations",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d')}  |  {total} photos  |  {n_flagged} flagged",
        "",
    ]

    for cid in sorted(k for k in groups if k is not None):
        photos = groups[cid]
        name = cluster_names.get(cid, f"cluster-{cid}")
        _append_table(lines, f"{name} ({len(photos)} photos)", photos)

    if None in groups:
        _append_table(lines, f"Unclustered ({len(groups[None])} photos)", groups[None])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    return output_path


def _append_table(lines: list[str], heading: str, photos: list) -> None:
    lines += [
        f"## {heading}",
        "",
        "| id | file | quality | scene | people | caption |",
        "|----|------|---------|-------|--------|---------|",
    ]
    for pid, path, quality, scene, caption, people, _, flagged in photos:
        marker = " 🚩" if flagged else ""
        caption_short = (caption or "")[:80]
        lines.append(
            f"| {pid}{marker} | {Path(path).name} | {quality or ''} "
            f"| {scene or ''} | {people or ''} | {caption_short} |"
        )
    lines.append("")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "build_report" -v
```

Expected: PASS all 6.

- [ ] **Step 5: Commit**

```bash
git add photos/recommend.py tests/test_recommend.py
git commit -m "feat: add build_report to photos/recommend.py"
```

---

## Task 4: `set_flagged` in `photos/recommend.py`

**Files:**
- Modify: `photos/recommend.py`
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_recommend.py`:

```python
from photos.recommend import auto_flag_quality, build_report, set_flagged


def test_set_flagged_flags_valid_ids(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good"},
        {"id": 2, "quality": "blurry"},
    ])
    result = set_flagged(conn, [1, 2], flag=True)

    assert sorted(result["done"]) == [1, 2]
    assert result["skipped"] == []
    assert result["not_found"] == []
    flags = {r[0]: r[1] for r in conn.execute("SELECT id, flagged FROM photos")}
    assert flags[1] == 1
    assert flags[2] == 1
    conn.close()


def test_set_flagged_skips_discarded(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "discarded": 1},
    ])
    result = set_flagged(conn, [1], flag=True)

    assert result["skipped"] == [(1, "already discarded")]
    assert result["done"] == []
    flagged = conn.execute("SELECT flagged FROM photos WHERE id=1").fetchone()[0]
    assert flagged == 0
    conn.close()


def test_set_flagged_not_found(tmp_path):
    conn = _make_conn(tmp_path, [])
    result = set_flagged(conn, [99], flag=True)

    assert result["not_found"] == [99]
    assert result["done"] == []
    conn.close()


def test_set_flagged_unflag_sets_zero(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "flagged": 1},
    ])
    result = set_flagged(conn, [1], flag=False)

    assert result["done"] == [1]
    flagged = conn.execute("SELECT flagged FROM photos WHERE id=1").fetchone()[0]
    assert flagged == 0
    conn.close()


def test_set_flagged_mixed_ids(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "quality": "good"},
        {"id": 2, "discarded": 1},
    ])
    result = set_flagged(conn, [1, 2, 99], flag=True)

    assert result["done"] == [1]
    assert result["skipped"] == [(2, "already discarded")]
    assert result["not_found"] == [99]
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "set_flagged" -v
```

Expected: FAIL — `set_flagged` not defined yet.

- [ ] **Step 3: Add `set_flagged` to `photos/recommend.py`**

```python
def set_flagged(
    conn: sqlite3.Connection,
    ids: list[int],
    flag: bool,
) -> dict:
    """Set flagged=1 or flagged=0 for given IDs.

    Returns {"done": [...], "skipped": [(id, reason),...], "not_found": [...]}.
    When flagging (flag=True), photos with discarded=1 are skipped.
    """
    done: list[int] = []
    skipped: list[tuple[int, str]] = []
    not_found: list[int] = []

    for pid in ids:
        row = conn.execute(
            "SELECT discarded FROM photos WHERE id=?", (pid,)
        ).fetchone()
        if row is None:
            not_found.append(pid)
            continue
        if flag and row[0]:
            skipped.append((pid, "already discarded"))
            continue
        conn.execute("UPDATE photos SET flagged=? WHERE id=?", (int(flag), pid))
        done.append(pid)

    conn.commit()
    return {"done": done, "skipped": skipped, "not_found": not_found}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -k "set_flagged" -v
```

Expected: PASS all 5.

- [ ] **Step 5: Commit**

```bash
git add photos/recommend.py tests/test_recommend.py
git commit -m "feat: add set_flagged to photos/recommend.py"
```

---

## Task 5: `cmd_recommend` in `photos.py`

**Files:**
- Modify: `photos.py` (add `cmd_recommend`, argparse)
- Modify: `tests/test_recommend.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_recommend.py`:

```python
def test_cmd_recommend_auto_flags_and_writes_report(tmp_path):
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    conn.execute(
        "INSERT INTO photos (id, path, quality) VALUES (1, '/a.jpg', 'blurry')"
    )
    conn.execute(
        "INSERT INTO photos (id, path, quality) VALUES (2, '/b.jpg', 'good')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "recommendations.md"
    clusters_path = tmp_path / "clusters.json"
    clusters_path.write_text("[]")

    import argparse
    args = argparse.Namespace(
        db=db,
        clusters=str(clusters_path),
        output=str(out),
    )
    mod.cmd_recommend(args)

    # blurry photo flagged in DB
    conn2 = sqlite3.connect(db)
    flagged = conn2.execute("SELECT flagged FROM photos WHERE id=1").fetchone()[0]
    conn2.close()
    assert flagged == 1

    # report written
    assert out.exists()
    assert "🚩" in out.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py::test_cmd_recommend_auto_flags_and_writes_report -v
```

Expected: FAIL — `cmd_recommend` not defined.

- [ ] **Step 3: Add `cmd_recommend` to `photos.py`**

Add this function to `photos.py` (after `cmd_describe`):

```python
def cmd_recommend(args):
    from photos.recommend import auto_flag_quality, build_report

    conn = _init_db(args.db)
    try:
        n = auto_flag_quality(conn)
        print(f"✓ Auto-flagged {n} photo(s) with non-good quality")

        clusters_path = Path(args.clusters) if Path(args.clusters).exists() else None
        output_path = build_report(conn, clusters_path, Path(args.output))
        print(f"✓ Report written to {output_path}")
        print(f"  Review it, then run: python photos.py flag <id> [id ...]")
    finally:
        conn.close()
```

Add the argparse entry in `main()` (before `args = p.parse_args()`):

```python
    rec = sub.add_parser("recommend", help="Auto-flag bad quality photos and write review report")
    rec.add_argument("--db", default="photos.db", metavar="DB")
    rec.add_argument("--clusters", default="clusters.json", metavar="FILE")
    rec.add_argument("--output", default="output/recommendations.md", metavar="FILE")
```

Wire up in the dispatch dict in `main()`:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend}[args.subcommand](args)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py::test_cmd_recommend_auto_flags_and_writes_report -v
```

Expected: PASS.

- [ ] **Step 5: Run full test_recommend.py to check no regressions**

```bash
cd /scratch/video_read && python -m pytest tests/test_recommend.py -v
```

Expected: PASS all tests.

- [ ] **Step 6: Commit**

```bash
git add photos.py tests/test_recommend.py
git commit -m "feat: add cmd_recommend subcommand to photos.py"
```

---

## Task 6: `cmd_flag` in `photos.py`

**Files:**
- Modify: `photos.py` (add `cmd_flag`, argparse)
- Create: `tests/test_flag_cmd.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_flag_cmd.py`:

```python
import argparse, json, shutil, sqlite3, os, sys, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path, rows, clusters=None):
    """rows: list of dicts with id, path (must be real file), optional quality/cluster_id/discarded/flagged."""
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    for r in rows:
        conn.execute(
            "INSERT INTO photos (id, path, quality, cluster_id, discarded, flagged) "
            "VALUES (?,?,?,?,?,?)",
            (r["id"], r["path"], r.get("quality", "good"),
             r.get("cluster_id"), r.get("discarded", 0), r.get("flagged", 0)),
        )
    conn.commit()
    conn.close()

    clusters_path = str(tmp_path / "clusters.json")
    Path(clusters_path).write_text(json.dumps(clusters or []))
    return db, clusters_path


def _flag_map(db):
    conn = sqlite3.connect(db)
    result = {r[0]: r[1] for r in conn.execute("SELECT id, flagged FROM photos")}
    conn.close()
    return result


def test_cmd_flag_sets_flagged_in_db(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src)}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 1


def test_cmd_flag_copies_file_to_to_review(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo content")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src)}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    copies = list(out_dir.rglob("a.jpg"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"photo content"


def test_cmd_flag_uses_cluster_name_as_subfolder(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    clusters = [{"id": 1, "name": "Iceland-2024", "photo_ids": [1]}]
    db, clusters_path = _make_db(
        tmp_path,
        [{"id": 1, "path": str(src), "cluster_id": 1}],
        clusters,
    )
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert (out_dir / "Iceland-2024" / "a.jpg").exists()


def test_cmd_flag_unclustered_goes_to_unclustered_folder(tmp_path):
    src = tmp_path / "b.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "cluster_id": None}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert (out_dir / "unclustered" / "b.jpg").exists()


def test_cmd_flag_skips_discarded(tmp_path):
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": "/fake.jpg", "discarded": 1}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 0  # discarded photo not flagged


def test_cmd_flag_unflag_sets_flagged_to_zero(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "flagged": 1}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=True,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 0


def test_cmd_flag_unflag_removes_copy(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "flagged": 1}])
    out_dir = tmp_path / "to-review"
    copy = out_dir / "unclustered" / "a.jpg"
    copy.parent.mkdir(parents=True)
    shutil.copy2(src, copy)

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=True,
    )
    mod.cmd_flag(args)

    assert not copy.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_flag_cmd.py -v
```

Expected: FAIL — `cmd_flag` not defined.

- [ ] **Step 3: Add `cmd_flag` to `photos.py`**

Add after `cmd_recommend` in `photos.py`:

```python
def cmd_flag(args):
    import shutil as _shutil
    from photos.recommend import set_flagged

    ids = [int(x) for x in args.ids]
    conn = _init_db(args.db)
    try:
        result = set_flagged(conn, ids, flag=not args.unflag)

        if args.unflag:
            out = Path(args.output_dir)
            for pid in result["done"]:
                row = conn.execute("SELECT path FROM photos WHERE id=?", (pid,)).fetchone()
                if not row:
                    continue
                fname = Path(row[0]).name
                for f in out.rglob(fname):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            print(f"✓ Unflagged {len(result['done'])} photo(s)")
        else:
            cluster_names: dict[int, str] = {}
            clusters_path = Path(args.clusters)
            if clusters_path.exists():
                for c in json.loads(clusters_path.read_text()):
                    cluster_names[c["id"]] = c["name"]

            out = Path(args.output_dir)
            n_copied = 0
            for pid in result["done"]:
                row = conn.execute(
                    "SELECT path, cluster_id FROM photos WHERE id=?", (pid,)
                ).fetchone()
                if not row:
                    continue
                src, cluster_id = row
                cname = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
                dest_dir = out / cname
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _dest_path(dest_dir, Path(src).name)
                try:
                    _shutil.copy2(src, dest)
                    n_copied += 1
                except OSError as e:
                    print(f"  Warning: could not copy {src}: {e}")

            print(f"✓ Flagged {len(result['done'])} photo(s), {n_copied} copied to {args.output_dir}")

        for pid, reason in result["skipped"]:
            print(f"  Warning: skipped photo {pid} ({reason})")
        for pid in result["not_found"]:
            print(f"  Warning: photo {pid} not found in DB")
    finally:
        conn.close()
```

Add the argparse entry in `main()` (after the `recommend` parser):

```python
    fl = sub.add_parser("flag", help="Flag photos for review and copy to to-review directory")
    fl.add_argument("ids", nargs="+", metavar="ID")
    fl.add_argument("--db", default="photos.db", metavar="DB")
    fl.add_argument("--clusters", default="clusters.json", metavar="FILE")
    fl.add_argument("--output-dir", default="output/to-review", metavar="DIR")
    fl.add_argument("--unflag", action="store_true", help="Unflag photos and remove copies")
```

Add `"flag": cmd_flag` to the dispatch dict in `main()`:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag}[args.subcommand](args)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_flag_cmd.py -v
```

Expected: PASS all 8 tests.

- [ ] **Step 5: Run full test suite**

```bash
cd /scratch/video_read && python -m pytest tests/ -v --ignore=tests/test_describe_cmd.py
```

(`test_describe_cmd.py` has a GPU integration test that requires CUDA — skip it. All others should pass.)

Expected: PASS all non-GPU tests.

- [ ] **Step 6: Smoke test CLI help**

```bash
cd /scratch/video_read && python photos.py recommend --help && python photos.py flag --help
```

Expected: both print usage without errors.

- [ ] **Step 7: Commit**

```bash
git add photos.py tests/test_flag_cmd.py
git commit -m "feat: add cmd_flag subcommand to photos.py"
```
