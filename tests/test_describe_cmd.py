import os, sys, shutil, sqlite3, subprocess
from pathlib import Path
import pytest

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)


def _cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


@pytest.mark.skipif(not _cuda_available(), reason="requires GPU")
def test_describe_real_photo(tmp_path):
    takeout = Path(PROJ) / "output" / "takeout" / "Takeout" / "Google Photos"
    jpgs = list(takeout.rglob("*.JPG")) + list(takeout.rglob("*.jpg"))
    if not jpgs:
        pytest.skip("No JPG files found in output/takeout")

    src = jpgs[0]
    dest = tmp_path / src.name
    shutil.copy2(src, dest)

    from photos import _init_db
    db = str(tmp_path / "photos.db")
    conn = _init_db(db)
    conn.execute("INSERT INTO photos (id, path, taken_at) VALUES (1,?,1000)", (str(dest),))
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "photos.py", "describe", "--db", db],
        capture_output=True, text=True, cwd=PROJ,
    )
    assert result.returncode == 0, result.stderr
    assert "Described 1" in result.stdout

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT caption, described_at FROM photos WHERE id=1").fetchone()
    conn.close()
    assert row[1] is not None, "described_at should be set"
    assert row[0] is not None, "caption should be non-null"
