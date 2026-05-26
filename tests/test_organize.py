import json, sqlite3, subprocess, sys, os, shutil
from pathlib import Path
PROJ = os.path.dirname(os.path.dirname(__file__))

def _setup(tmp_path):
    # Create fake photo files
    src = tmp_path / "takeout"
    src.mkdir()
    photos = []
    for i in range(4):
        p = src / f"IMG_{i:04d}.jpg"
        p.write_bytes(f"photo{i}".encode())
        photos.append(str(p))

    # Create DB
    db = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER
    )""")
    for i, p in enumerate(photos):
        conn.execute("INSERT INTO photos VALUES (?,?,?,?,?,?,?)",
                     (i+1, p, 1704067200 + i*86400, None, None, None, i//2 + 1))
    conn.commit()
    conn.close()

    # Create clusters.json
    clusters = [
        {"id": 1, "name": "Iceland Trip 2024", "is_trip": True, "confirmed": True,
         "photo_count": 2, "photo_ids": [1, 2], "start": "2024-01-01", "end": "2024-01-02", "place": None},
        {"id": 2, "name": "2024-01", "is_trip": False, "confirmed": True,
         "photo_count": 2, "photo_ids": [3, 4], "start": "2024-01-03", "end": "2024-01-04", "place": None},
    ]
    clusters_path = str(tmp_path / "clusters.json")
    open(clusters_path, 'w').write(json.dumps(clusters))
    return db, clusters_path

def test_organize_creates_folders(tmp_path):
    db, clusters_path = _setup(tmp_path)
    out = str(tmp_path / "organized")
    result = subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", out, "--db", db, "--clusters", clusters_path],
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    out_path = Path(out)
    assert (out_path / "Iceland-Trip-2024").is_dir()
    assert (out_path / "2024-01").is_dir()
    assert len(list((out_path / "Iceland-Trip-2024").iterdir())) == 2
    assert len(list((out_path / "2024-01").iterdir())) == 2

def test_organize_does_not_move_originals(tmp_path):
    db, clusters_path = _setup(tmp_path)
    src_files = list((tmp_path / "takeout").iterdir())
    out = str(tmp_path / "organized")
    subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", out, "--db", db, "--clusters", clusters_path],
        capture_output=True, text=True, cwd=PROJ
    )
    # All original files still exist
    for f in src_files:
        assert f.exists(), f"Original file was moved: {f}"

def test_organize_collision_rename(tmp_path):
    db, clusters_path = _setup(tmp_path)
    out = tmp_path / "organized"
    out.mkdir()
    trip_dir = out / "Iceland-Trip-2024"
    trip_dir.mkdir()
    # Pre-place a file with the same name as one of the photos
    (trip_dir / "IMG_0000.jpg").write_bytes(b"existing")

    subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", str(out), "--db", db, "--clusters", str(tmp_path / "clusters.json")],
        capture_output=True, text=True, cwd=PROJ
    )
    files = list(trip_dir.iterdir())
    names = {f.name for f in files}
    assert "IMG_0000.jpg" in names
    assert any("IMG_0000_2.jpg" in n for n in names)
