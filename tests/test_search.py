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
