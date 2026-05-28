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
    assert "Auto-discarded 1" in result.stdout

    disc = _discarded_map(db)
    assert disc[1] == 0  # id=1 kept (lowest id in dup group)
    assert disc[2] == 1  # id=2 discarded
    assert disc[3] == 0  # different file, untouched
