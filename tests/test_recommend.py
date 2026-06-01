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
