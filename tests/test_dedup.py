import os, sys, sqlite3
from pathlib import Path
import importlib.util

PROJ = os.path.dirname(os.path.dirname(__file__))


def _load_photos_module():
    """Load photos.py from the project root to avoid package name collision."""
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    photos_mod = _load_photos_module()
    conn2 = photos_mod._init_db(db)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(photos)")}
    conn2.close()
    assert "discarded" in cols


def test_new_db_has_discarded_column(tmp_path):
    photos_mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = photos_mod._init_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    conn.close()
    assert "discarded" in cols


def test_discarded_defaults_to_zero(tmp_path):
    photos_mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = photos_mod._init_db(db)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    conn.execute("INSERT INTO photos (path, taken_at) VALUES (?,?)", (str(f), 1000))
    conn.commit()
    val = conn.execute("SELECT discarded FROM photos").fetchone()[0]
    conn.close()
    assert val == 0


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
