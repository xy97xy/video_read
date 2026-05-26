import json, subprocess, sys, os
PROJ = os.path.dirname(os.path.dirname(__file__))

def _write_clusters(path, clusters):
    open(path, 'w').write(json.dumps(clusters, indent=2))

def test_review_confirm(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-07 Reykjavik, Iceland",
        "is_trip": True, "confirmed": False,
        "photo_count": 42, "photo_ids": [1,2,3],
        "start": "2024-07-01", "end": "2024-07-07", "place": "Reykjavik, Iceland"
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="c\n", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["confirmed"] is True

def test_review_rename(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-07 Reykjavik, Iceland",
        "is_trip": True, "confirmed": False,
        "photo_count": 10, "photo_ids": [1],
        "start": "2024-07-01", "end": "2024-07-07", "place": "Reykjavik, Iceland"
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="r\nIceland Trip 2024\n",
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["name"] == "Iceland Trip 2024"
    assert data[0]["confirmed"] is True

def test_review_discard(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-03 Quick trip",
        "is_trip": True, "confirmed": False,
        "photo_count": 5, "photo_ids": [1],
        "start": "2024-07-01", "end": "2024-07-03", "place": None
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="d\n", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["is_trip"] is False
    assert data[0]["confirmed"] is True

def test_review_skips_already_confirmed(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-03",
        "is_trip": False, "confirmed": True,
        "photo_count": 20, "photo_ids": [1],
        "start": "2024-03-01", "end": "2024-03-31", "place": None
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0
    assert "nothing to review" in result.stdout.lower() or "0 trip" in result.stdout.lower()
