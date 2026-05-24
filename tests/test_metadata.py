import json, sys, os, time, sqlite3, subprocess
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from photos.metadata import find_media_files, extract_metadata

def test_find_media_files(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.JPG").write_bytes(b"x")
    (tmp_path / "c.mp4").write_bytes(b"x")
    (tmp_path / "d.txt").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "e.heic").write_bytes(b"x")

    files = find_media_files(str(tmp_path))
    names = {f.name.lower() for f in files}
    assert names == {"a.jpg", "b.jpg", "c.mp4", "e.heic"}
    assert len(files) == 4


def test_extract_metadata_sidecar(tmp_path):
    img = tmp_path / "IMG_0001.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000000"},
        "geoData": {"latitude": 64.1355, "longitude": -21.8954, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000000
    assert abs(lat - 64.1355) < 0.001
    assert abs(lon - (-21.8954)) < 0.001


def test_extract_metadata_sidecar_zero_gps_ignored(tmp_path):
    img = tmp_path / "IMG_0002.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0002.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000001"},
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000001
    assert lat is None
    assert lon is None


def test_extract_metadata_mtime_fallback(tmp_path):
    img = tmp_path / "IMG_0003.jpg"
    img.write_bytes(b"fake")
    # no sidecar, no valid EXIF

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at is not None
    assert abs(taken_at - int(img.stat().st_mtime)) <= 2
    assert lat is None
    assert lon is None


def test_extract_metadata_no_sidecar_stem_fallback(tmp_path):
    """Google sometimes writes sidecar as IMG_0001.json not IMG_0001.jpg.json"""
    img = tmp_path / "IMG_0004.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0004.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000099"},
        "geoData": {"latitude": 10.0, "longitude": 20.0, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000099


def test_scan_creates_db(tmp_path):
    takeout = tmp_path / "Takeout" / "Google Photos" / "Photos from 2024"
    takeout.mkdir(parents=True)
    img = takeout / "IMG_0001.jpg"
    img.write_bytes(b"fake")
    sidecar = takeout / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000000"},
        "geoData": {"latitude": 64.1, "longitude": -21.9, "altitude": 0.0}
    }))

    db_path = str(tmp_path / "photos.db")
    result = subprocess.run(
        [sys.executable, "photos.py", "scan",
         "--takeout-dir", str(tmp_path / "Takeout"),
         "--db", db_path],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(__file__))
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT path, taken_at, lat, lon FROM photos").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == 1720000000
    assert abs(rows[0][2] - 64.1) < 0.01
