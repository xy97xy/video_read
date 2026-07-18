import json, sqlite3, subprocess, sys, os, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

# Import _init_db from photos.py (the top-level script, not the photos/ package)
_spec = importlib.util.spec_from_file_location("photos_main", os.path.join(PROJ, "photos.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_init_db = _mod._init_db


def _make_db(tmp_path, photos_data):
    """photos_data: list of (path_obj, taken_at_int)."""
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    try:
        for i, (p, t) in enumerate(photos_data, 1):
            conn.execute(
                "INSERT INTO photos (id, path, taken_at) VALUES (?,?,?)",
                (i, str(p), t),
            )
        conn.commit()
    finally:
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
    assert "auto-discarded 1 byte-identical" in result.stdout

    disc = _discarded_map(db)
    assert disc[1] == 0  # id=1 kept (lowest id in dup group)
    assert disc[2] == 1  # id=2 discarded
    assert disc[3] == 0  # different file, untouched


def _make_burst_db(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"a" * 1000)
    b = tmp_path / "b.jpg"; b.write_bytes(b"b" * 3000)
    c = tmp_path / "c.jpg"; c.write_bytes(b"c" * 2000)
    return _make_db(tmp_path, [(a, 1000), (b, 1002), (c, 1004)])


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


def test_organize_skips_discarded(tmp_path):
    a = tmp_path / "keep.jpg";    a.write_bytes(b"keep this")
    b = tmp_path / "discard.jpg"; b.write_bytes(b"throw away")

    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    try:
        conn.execute(
            "INSERT INTO photos (id, path, taken_at, cluster_id, discarded) VALUES (1,?,1000,1,0)",
            (str(a),),
        )
        conn.execute(
            "INSERT INTO photos (id, path, taken_at, cluster_id, discarded) VALUES (2,?,1001,1,1)",
            (str(b),),
        )
        conn.commit()
    finally:
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
    assert len(files) == 1  # discarded photo not copied
