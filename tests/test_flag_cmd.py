import argparse, json, shutil, sqlite3, os, sys, importlib.util
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _load_photos_module():
    spec = importlib.util.spec_from_file_location("photos_module", f"{PROJ}/photos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path, rows, clusters=None):
    """rows: list of dicts with id, path (must be real file), optional quality/cluster_id/discarded/flagged."""
    mod = _load_photos_module()
    db = str(tmp_path / "photos.db")
    conn = mod._init_db(db)
    for r in rows:
        conn.execute(
            "INSERT INTO photos (id, path, quality, cluster_id, discarded, flagged) "
            "VALUES (?,?,?,?,?,?)",
            (r["id"], r["path"], r.get("quality", "good"),
             r.get("cluster_id"), r.get("discarded", 0), r.get("flagged", 0)),
        )
    conn.commit()
    conn.close()

    clusters_path = str(tmp_path / "clusters.json")
    Path(clusters_path).write_text(json.dumps(clusters or []))
    return db, clusters_path


def _flag_map(db):
    conn = sqlite3.connect(db)
    result = {r[0]: r[1] for r in conn.execute("SELECT id, flagged FROM photos")}
    conn.close()
    return result


def test_cmd_flag_sets_flagged_in_db(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src)}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 1


def test_cmd_flag_copies_file_to_to_review(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo content")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src)}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    copies = list(out_dir.rglob("a.jpg"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"photo content"


def test_cmd_flag_uses_cluster_name_as_subfolder(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    clusters = [{"id": 1, "name": "Iceland-2024", "photo_ids": [1]}]
    db, clusters_path = _make_db(
        tmp_path,
        [{"id": 1, "path": str(src), "cluster_id": 1}],
        clusters,
    )
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert (out_dir / "Iceland-2024" / "a.jpg").exists()


def test_cmd_flag_unclustered_goes_to_unclustered_folder(tmp_path):
    src = tmp_path / "b.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "cluster_id": None}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert (out_dir / "unclustered" / "b.jpg").exists()


def test_cmd_flag_skips_discarded(tmp_path):
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": "/fake.jpg", "discarded": 1}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=False,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 0  # discarded photo not flagged


def test_cmd_flag_unflag_sets_flagged_to_zero(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "flagged": 1}])
    out_dir = tmp_path / "to-review"

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=True,
    )
    mod.cmd_flag(args)

    assert _flag_map(db)[1] == 0


def test_cmd_flag_unflag_removes_copy(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    db, clusters_path = _make_db(tmp_path, [{"id": 1, "path": str(src), "flagged": 1}])
    out_dir = tmp_path / "to-review"
    copy = out_dir / "unclustered" / "a.jpg"
    copy.parent.mkdir(parents=True)
    shutil.copy2(src, copy)

    mod = _load_photos_module()
    args = argparse.Namespace(
        ids=["1"], db=db, clusters=clusters_path,
        output_dir=str(out_dir), unflag=True,
    )
    mod.cmd_flag(args)

    assert not copy.exists()
