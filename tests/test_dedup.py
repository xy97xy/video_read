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
