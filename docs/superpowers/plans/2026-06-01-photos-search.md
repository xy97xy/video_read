# Photos Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `photos.py search <query>` — full-text search over Qwen photo descriptions using SQLite FTS5, printing results to terminal and saving a markdown report.

**Architecture:** A new `photos/search.py` module provides `build_fts(conn)` (rebuilds the FTS5 index) and `search_photos(conn, query, limit)` (queries it). `cmd_search` in `photos.py` wires these together: check for descriptions, build index, query, print table, write markdown.

**Tech Stack:** Python stdlib only — `sqlite3` FTS5 (built-in), no new dependencies.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `photos/search.py` | `build_fts`, `search_photos` |
| Modify | `photos.py` | `cmd_search`, argparse wiring |
| Create | `tests/test_search.py` | 9 unit tests for build_fts + search_photos |
| Create | `tests/test_search_cmd.py` | 4 integration tests for cmd_search |

---

## Task 1: `build_fts` in `photos/search.py`

**Files:**
- Create: `photos/search.py`
- Create: `tests/test_search.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search.py`:

```python
import os, sys, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

from photos.search import build_fts


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_conn(tmp_path, rows):
    """rows: list of dicts. id required. Optional: path, caption, scene, people,
    place, discarded, described_at."""
    mod = _load_photos_module()
    conn = mod._init_db(str(tmp_path / "photos.db"))
    for r in rows:
        conn.execute(
            "INSERT INTO photos "
            "(id, path, caption, scene, people, place, discarded, described_at) "
            "VALUES (:id,:path,:caption,:scene,:people,:place,:discarded,:described_at)",
            {
                "id": r["id"],
                "path": r.get("path", f"/fake/{r['id']}.jpg"),
                "caption": r.get("caption"),
                "scene": r.get("scene"),
                "people": r.get("people"),
                "place": r.get("place"),
                "discarded": r.get("discarded", 0),
                "described_at": r.get("described_at", 1),
            },
        )
    conn.commit()
    return conn


def test_build_fts_creates_table(tmp_path):
    conn = _make_conn(tmp_path, [{"id": 1, "caption": "mountain sunset"}])
    build_fts(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    conn.close()
    assert "photos_fts" in tables


def test_build_fts_excludes_discarded(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "mountain sunset", "discarded": 1},
    ])
    build_fts(conn)
    rows = conn.execute("SELECT rowid FROM photos_fts").fetchall()
    conn.close()
    assert len(rows) == 0


def test_build_fts_excludes_undescribed(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": None, "described_at": None},
    ])
    build_fts(conn)
    rows = conn.execute("SELECT rowid FROM photos_fts").fetchall()
    conn.close()
    assert len(rows) == 0


def test_build_fts_is_idempotent(tmp_path):
    conn = _make_conn(tmp_path, [{"id": 1, "caption": "mountain sunset"}])
    build_fts(conn)
    build_fts(conn)  # second call must not raise
    rows = conn.execute("SELECT rowid FROM photos_fts").fetchall()
    conn.close()
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_search.py -v
```

Expected: FAIL — `photos/search.py` doesn't exist yet.

- [ ] **Step 3: Create `photos/search.py` with `build_fts`**

```python
from __future__ import annotations
import sqlite3


def build_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 full-text search index from described, non-discarded photos."""
    conn.execute("DROP TABLE IF EXISTS photos_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE photos_fts USING fts5(
            caption, scene, people, place,
            content='photos',
            content_rowid='id'
        )
    """)
    conn.execute("""
        INSERT INTO photos_fts(rowid, caption, scene, people, place)
        SELECT id, caption, scene, people, place
        FROM photos
        WHERE discarded=0 AND described_at IS NOT NULL
    """)
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_search.py -v
```

Expected: PASS all 4.

- [ ] **Step 5: Commit**

```bash
git add photos/search.py tests/test_search.py
git commit -m "feat: add build_fts to photos/search.py"
```

---

## Task 2: `search_photos` in `photos/search.py`

**Files:**
- Modify: `photos/search.py`
- Modify: `tests/test_search.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py` (after existing imports and helpers):

```python
from photos.search import build_fts, search_photos


def test_search_photos_returns_matches(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "A hiker on a mountain trail at sunset"},
        {"id": 2, "caption": "A dog playing on the beach"},
    ])
    build_fts(conn)
    results = search_photos(conn, "mountain", limit=10)
    conn.close()
    assert len(results) == 1
    assert results[0]["id"] == 1


def test_search_photos_respects_limit(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "mountain view at dawn"},
        {"id": 2, "caption": "mountain peak in snow"},
        {"id": 3, "caption": "rocky mountain trail"},
    ])
    build_fts(conn)
    results = search_photos(conn, "mountain", limit=1)
    conn.close()
    assert len(results) == 1


def test_search_photos_returns_empty_for_no_match(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "A sunset over the mountains"},
    ])
    build_fts(conn)
    results = search_photos(conn, "ocean", limit=10)
    conn.close()
    assert results == []


def test_search_photos_matches_scene_field(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "Two people walking", "scene": "mountain trail"},
    ])
    build_fts(conn)
    results = search_photos(conn, "mountain", limit=10)
    conn.close()
    assert len(results) == 1
    assert results[0]["id"] == 1


def test_search_photos_matches_place_field(tmp_path):
    conn = _make_conn(tmp_path, [
        {"id": 1, "caption": "Scenic overlook", "place": "Zion National Park"},
    ])
    build_fts(conn)
    results = search_photos(conn, "Zion", limit=10)
    conn.close()
    assert len(results) == 1
    assert results[0]["id"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_search.py -k "search_photos" -v
```

Expected: FAIL — `search_photos` not defined.

- [ ] **Step 3: Add `search_photos` to `photos/search.py`**

```python
def search_photos(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search photos using FTS5 BM25 ranking. Returns list sorted by relevance (best first).

    Each dict has: id, path, scene, place, caption, cluster_id, score.
    bm25() returns negative values — lower (more negative) = better match.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.path, p.scene, p.place, p.caption, p.cluster_id,
               bm25(photos_fts) AS score
        FROM photos_fts
        JOIN photos p ON photos_fts.rowid = p.id
        WHERE photos_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "path": r[1],
            "scene": r[2],
            "place": r[3],
            "caption": r[4],
            "cluster_id": r[5],
            "score": r[6],
        }
        for r in rows
    ]
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_search.py -v
```

Expected: PASS all 9.

- [ ] **Step 5: Commit**

```bash
git add photos/search.py tests/test_search.py
git commit -m "feat: add search_photos to photos/search.py"
```

---

## Task 3: `cmd_search` in `photos.py`

**Files:**
- Modify: `photos.py` (add `cmd_search`, argparse)
- Create: `tests/test_search_cmd.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_cmd.py`:

```python
import argparse, sqlite3, os, sys, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path, rows):
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    for r in rows:
        conn.execute(
            "INSERT INTO photos (id, path, caption, scene, place, discarded, described_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r.get("path", f"/fake/{r['id']}.jpg"),
             r.get("caption"), r.get("scene"), r.get("place"),
             r.get("discarded", 0), r.get("described_at", 1)),
        )
    conn.commit()
    conn.close()
    return db


def test_cmd_search_prints_results(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A hiker on a mountain trail"},
        {"id": 2, "path": "/b.jpg", "caption": "A dog on the beach"},
    ])
    mod = _load_photos_module()
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(tmp_path / "results.md"),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "a.jpg" in out
    assert "Found 1" in out


def test_cmd_search_writes_markdown_file(tmp_path):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A hiker on a mountain trail"},
    ])
    mod = _load_photos_module()
    out_path = tmp_path / "results.md"
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(out_path),
    )
    mod.cmd_search(args)
    assert out_path.exists()
    text = out_path.read_text()
    assert "mountain" in text.lower()
    assert "| 1 |" in text


def test_cmd_search_no_results(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A beach scene"},
    ])
    mod = _load_photos_module()
    out_path = tmp_path / "results.md"
    args = argparse.Namespace(
        db=db, query="unicorn", limit=10,
        output=str(out_path),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "No photos found" in out
    assert not out_path.exists()


def test_cmd_search_no_descriptions(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": None, "described_at": None},
    ])
    mod = _load_photos_module()
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(tmp_path / "results.md"),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "described" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_search_cmd.py -v
```

Expected: FAIL — `cmd_search` not defined.

- [ ] **Step 3: Add `cmd_search` to `photos.py`**

Add after `cmd_flag`:

```python
def cmd_search(args):
    import sqlite3 as _sqlite3
    from photos.search import build_fts, search_photos

    conn = _init_db(args.db)
    try:
        n_described = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE described_at IS NOT NULL AND discarded=0"
        ).fetchone()[0]
        if n_described == 0:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        build_fts(conn)

        try:
            results = search_photos(conn, args.query, args.limit)
        except _sqlite3.OperationalError as e:
            print(f"Search error: {e}")
            print(f"  Query was: {args.query}")
            return

        if not results:
            print(f'No photos found matching "{args.query}"')
            return

        print(f'Found {len(results)} photo(s) matching "{args.query}"\n')
        header = f" {'id':>4} | {'score':>6} | {'file':<25} | {'scene':<18} | {'place':<15} | caption"
        print(header)
        print("-" * len(header))
        for r in results:
            fname = Path(r["path"]).name
            print(
                f" {r['id']:>4} | {r['score']:>6.2f} | {fname:<25} | "
                f"{(r['scene'] or ''):<18} | {(r['place'] or ''):<15} | "
                f"{(r['caption'] or '')[:60]}"
            )

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'# Search Results: "{args.query}"',
            f"Generated: {datetime.now().strftime('%Y-%m-%d')}  |  {len(results)} results",
            "",
            "| id | score | file | scene | place | caption |",
            "|----|-------|------|-------|-------|---------|",
        ]
        for r in results:
            fname = Path(r["path"]).name
            caption_short = (r["caption"] or "")[:80]
            lines.append(
                f"| {r['id']} | {r['score']:.2f} | {fname} | "
                f"{r['scene'] or ''} | {r['place'] or ''} | {caption_short} |"
            )
        out.write_text("\n".join(lines))
        print(f"\nSaved to {out}")
    finally:
        conn.close()
```

Add the argparse entry in `main()` (after the `flag` parser):

```python
    sr = sub.add_parser("search", help="Full-text search photos by description")
    sr.add_argument("query", metavar="QUERY")
    sr.add_argument("--db", default="photos.db", metavar="DB")
    sr.add_argument("--limit", type=int, default=20, metavar="N")
    sr.add_argument("--output", default="output/search-results.md", metavar="FILE")
```

Update the dispatch dict in `main()`:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag,
     "search": cmd_search}[args.subcommand](args)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_search_cmd.py -v
```

Expected: PASS all 4.

- [ ] **Step 5: Run full test suite**

```bash
cd /scratch/video_read && python -m pytest tests/test_search.py tests/test_search_cmd.py tests/test_recommend.py tests/test_flag_cmd.py -v
```

Expected: PASS all 41 tests.

- [ ] **Step 6: Smoke test on real data**

```bash
cd /scratch/video_read && python photos.py search "mountain trail" --db output/photos.db --output output/search-results.md
```

Expected: prints a ranked table of photos and saves `output/search-results.md`.

- [ ] **Step 7: Smoke test CLI help**

```bash
cd /scratch/video_read && python photos.py search --help
```

Expected: prints usage without errors.

- [ ] **Step 8: Commit**

```bash
git add photos.py tests/test_search_cmd.py
git commit -m "feat: add cmd_search subcommand to photos.py"
```
