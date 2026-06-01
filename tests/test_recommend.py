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


from photos.recommend import auto_flag_quality, build_report


def _make_conn(tmp_path, rows):
    """rows: list of dicts. Required key: id. Optional: path, quality, scene,
    caption, people, cluster_id, discarded, flagged."""
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
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
