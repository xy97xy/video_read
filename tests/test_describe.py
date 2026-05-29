import os, sys, sqlite3
from pathlib import Path
import importlib.util

PROJ = os.path.dirname(os.path.dirname(__file__))


def _load_photos_module():
    """Load photos.py from the project root to avoid package name collision."""
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_describe_columns(tmp_path):
    db = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT,
        cluster_id INTEGER, discarded INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

    photos = _load_photos_module()
    _init_db = photos._init_db
    conn2 = _init_db(db)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(photos)")}
    conn2.close()
    for col in ("caption", "quality", "scene", "people", "described_at"):
        assert col in cols, f"Missing column: {col}"


def test_new_db_has_describe_columns(tmp_path):
    photos = _load_photos_module()
    _init_db = photos._init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    conn.close()
    for col in ("caption", "quality", "scene", "people", "described_at"):
        assert col in cols, f"Missing column: {col}"


def test_described_at_defaults_to_null(tmp_path):
    photos = _load_photos_module()
    _init_db = photos._init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    conn.execute("INSERT INTO photos (path, taken_at) VALUES (?,?)", (str(f), 1000))
    conn.commit()
    val = conn.execute("SELECT described_at FROM photos").fetchone()[0]
    conn.close()
    assert val is None


from photos.describe import _parse_describe_json


def test_parse_describe_json_valid():
    raw = '{"caption": "two people hiking", "quality": "good", "scene": "mountain trail", "people": "few"}'
    result = _parse_describe_json(raw)
    assert result == {
        "caption": "two people hiking",
        "quality": "good",
        "scene": "mountain trail",
        "people": "few",
    }


def test_parse_describe_json_missing_fields():
    raw = '{"caption": "hikers", "quality": "good"}'
    assert _parse_describe_json(raw) is None


def test_parse_describe_json_malformed():
    assert _parse_describe_json("not json at all") is None
    assert _parse_describe_json("") is None


def test_parse_describe_json_strips_markdown():
    raw = '```json\n{"caption": "sunset", "quality": "good", "scene": "beach", "people": "none"}\n```'
    result = _parse_describe_json(raw)
    assert result is not None
    assert result["caption"] == "sunset"


def test_parse_describe_json_embedded_in_text():
    raw = 'Here is the JSON: {"caption": "park", "quality": "good", "scene": "city park", "people": "many"} Done.'
    result = _parse_describe_json(raw)
    assert result is not None
    assert result["scene"] == "city park"


def test_describe_photo_returns_nulls_for_missing_file():
    from photos.describe import describe_photo
    from pathlib import Path
    result = describe_photo(None, None, Path("/nonexistent/ghost.jpg"))
    assert result == {"caption": None, "quality": None, "scene": None, "people": None}
