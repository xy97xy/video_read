import argparse, sqlite3, os, sys, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path, rows):
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    for r in rows:
        conn.execute(
            "INSERT INTO photos (id, path, caption, scene, place, discarded, described_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r.get("path", f"/fake/{r['id']}.jpg"),
             r.get("caption"), r.get("scene"), r.get("place"),
             r.get("discarded", 0), r.get("described_at", 1)),
        )
    conn.commit()
    conn.close()
    return db


def test_cmd_search_prints_results(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A hiker on a mountain trail"},
        {"id": 2, "path": "/b.jpg", "caption": "A dog on the beach"},
    ])
    mod = _load_photos_module()
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(tmp_path / "results.md"),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "a.jpg" in out
    assert "Found 1" in out


def test_cmd_search_writes_markdown_file(tmp_path):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A hiker on a mountain trail"},
    ])
    mod = _load_photos_module()
    out_path = tmp_path / "results.md"
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(out_path),
    )
    mod.cmd_search(args)
    assert out_path.exists()
    text = out_path.read_text()
    assert "mountain" in text.lower()
    assert "| 1 |" in text


def test_cmd_search_no_results(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": "A beach scene"},
    ])
    mod = _load_photos_module()
    out_path = tmp_path / "results.md"
    args = argparse.Namespace(
        db=db, query="unicorn", limit=10,
        output=str(out_path),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "No photos found" in out
    assert not out_path.exists()


def test_cmd_search_no_descriptions(tmp_path, capsys):
    db = _make_db(tmp_path, [
        {"id": 1, "path": "/a.jpg", "caption": None, "described_at": None},
    ])
    mod = _load_photos_module()
    args = argparse.Namespace(
        db=db, query="mountain", limit=10,
        output=str(tmp_path / "results.md"),
    )
    mod.cmd_search(args)
    out = capsys.readouterr().out
    assert "described" in out.lower()
