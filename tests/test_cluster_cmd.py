import importlib.util, json, sqlite3, subprocess, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PROJ = os.path.dirname(os.path.dirname(__file__))
DAY = 86400

_spec = importlib.util.spec_from_file_location("photos_main", os.path.join(PROJ, "photos.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_init_db = _mod._init_db

def _make_db(db_path, rows):
    conn = _init_db(db_path)
    for r in rows:
        conn.execute("INSERT INTO photos (path,taken_at,lat,lon,place) VALUES (?,?,?,?,?)", r)
    conn.commit()
    conn.close()

def test_cluster_writes_json(tmp_path):
    db = str(tmp_path / "photos.db")
    base = 1704067200  # 2024-01-01
    _make_db(db, [
        (f"/fake/{i}.jpg", base + i * DAY, 37.7, -122.4, "San Francisco, US")
        for i in range(5)
    ] + [
        (f"/fake/{i+10}.jpg", base + (15+i)*DAY, 64.1, -21.9, "Reykjavik, Iceland")
        for i in range(5)
    ])
    clusters_path = str(tmp_path / "clusters.json")
    result = subprocess.run(
        [sys.executable, "photos.py", "cluster",
         "--db", db, "--clusters", clusters_path,
         "--gap-days", "3", "--radius-km", "50"],
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    clusters = json.loads(open(clusters_path).read())
    assert len(clusters) >= 1
    trips = [c for c in clusters if c["is_trip"]]
    assert len(trips) == 1
    assert clusters_path  # file exists
