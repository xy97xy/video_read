import argparse, json, os, sys, importlib.util, time
from pathlib import Path
from PIL import Image

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
            "INSERT INTO photos (id, path, quality, discarded, described_at, flagged, cluster_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r["path"], r.get("quality", "good"),
             r.get("discarded", 0), r.get("described_at", 1),
             r.get("flagged", 0), r.get("cluster_id")),
        )
    conn.commit()
    conn.close()
    return db


def _make_clusters(tmp_path, clusters=None):
    path = tmp_path / "clusters.json"
    path.write_text(json.dumps(clusters or []))
    return str(path)


def _make_photo(tmp_path, name="IMG_1234.jpg"):
    p = tmp_path / name
    Image.new("RGB", (100, 80), color=(100, 100, 100)).save(str(p))
    return p


def _args(tmp_path, db, clusters_path=None, force=False):
    return argparse.Namespace(
        db=db,
        clusters=clusters_path or str(tmp_path / "clusters.json"),
        output_dir=str(tmp_path / "enhanced"),
        force=force,
    )


def test_cmd_enhance_creates_enhanced_file(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert list((tmp_path / "enhanced").rglob("IMG_1234_enhanced.jpg"))


def test_cmd_enhance_creates_compare_file(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert list((tmp_path / "enhanced").rglob("IMG_1234_compare.jpg"))


def test_cmd_enhance_uses_cluster_subfolder(tmp_path):
    photo = _make_photo(tmp_path)
    clusters_data = [{"id": 1, "name": "Zion 2024", "photo_ids": [1]}]
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "cluster_id": 1}])
    clusters = _make_clusters(tmp_path, clusters_data)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    # _sanitize("Zion 2024") -> "Zion-2024"
    assert (tmp_path / "enhanced" / "Zion-2024" / "IMG_1234_enhanced.jpg").exists()


def test_cmd_enhance_unclustered_subfolder(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "cluster_id": None}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert (tmp_path / "enhanced" / "unclustered" / "IMG_1234_enhanced.jpg").exists()


def test_cmd_enhance_skips_existing(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    clusters = _make_clusters(tmp_path)
    out_dir = tmp_path / "enhanced" / "unclustered"
    out_dir.mkdir(parents=True)
    enhanced = out_dir / "IMG_1234_enhanced.jpg"
    compare = out_dir / "IMG_1234_compare.jpg"
    Image.new("RGB", (10, 10)).save(str(enhanced))
    Image.new("RGB", (10, 10)).save(str(compare))
    mtime_before = enhanced.stat().st_mtime

    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert enhanced.stat().st_mtime == mtime_before


def test_cmd_enhance_force_reruns(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    clusters = _make_clusters(tmp_path)
    out_dir = tmp_path / "enhanced" / "unclustered"
    out_dir.mkdir(parents=True)
    enhanced = out_dir / "IMG_1234_enhanced.jpg"
    compare = out_dir / "IMG_1234_compare.jpg"
    Image.new("RGB", (10, 10)).save(str(enhanced))
    Image.new("RGB", (10, 10)).save(str(compare))
    mtime_before = enhanced.stat().st_mtime

    time.sleep(0.05)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters, force=True))
    assert enhanced.stat().st_mtime > mtime_before


def test_cmd_enhance_skips_undescribed(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "described_at": None}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert not list((tmp_path / "enhanced").rglob("*.jpg"))


def test_cmd_enhance_skips_flagged(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "flagged": 1}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    assert not list((tmp_path / "enhanced").rglob("*.jpg"))


def test_cmd_enhance_warns_no_descriptions(tmp_path, capsys):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "described_at": None}])
    clusters = _make_clusters(tmp_path)
    mod = _load_photos_module()
    mod.cmd_enhance(_args(tmp_path, db, clusters))
    out = capsys.readouterr().out
    assert "described" in out.lower()
