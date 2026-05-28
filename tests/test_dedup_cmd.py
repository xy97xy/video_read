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
