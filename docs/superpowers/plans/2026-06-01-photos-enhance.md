# Photos Enhance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `photos.py enhance` — apply PIL color correction to all described photos, saving `_enhanced.jpg` and `_compare.jpg` alongside each original.

**Architecture:** `photos/enhance.py` holds `enhance_photo` and `make_comparison` (pure PIL, no DB). `cmd_enhance` in `photos.py` queries the DB, iterates with tqdm, and writes output files. No new dependencies — PIL, cv2, and pillow-heif are already in the venv.

**Tech Stack:** PIL (`ImageOps`, `ImageEnhance`), pillow-heif (HEIC support), tqdm, Python stdlib.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `photos/enhance.py` | `enhance_photo`, `make_comparison` |
| Modify | `photos.py` | `cmd_enhance`, argparse wiring |
| Create | `tests/test_enhance.py` | 5 unit tests for enhance_photo + make_comparison |
| Create | `tests/test_enhance_cmd.py` | 6 integration tests for cmd_enhance |

---

## Task 1: `enhance_photo` in `photos/enhance.py`

**Files:**
- Create: `photos/enhance.py`
- Create: `tests/test_enhance.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enhance.py`:

```python
import os, sys
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

from PIL import Image
from photos.enhance import enhance_photo


def _make_image(width=100, height=80, color=(100, 100, 100)):
    return Image.new("RGB", (width, height), color=color)


def test_enhance_photo_returns_same_size():
    img = _make_image(200, 150)
    result = enhance_photo(img, quality="good")
    assert result.size == (200, 150)


def test_enhance_photo_standard_changes_pixels():
    img = _make_image(100, 100, color=(80, 80, 80))
    result = enhance_photo(img, quality="good")
    orig_px = img.getpixel((50, 50))
    new_px = result.getpixel((50, 50))
    assert new_px != orig_px


def test_enhance_photo_dark_brighter_than_standard():
    img = _make_image(100, 100, color=(50, 50, 50))
    standard = enhance_photo(img.copy(), quality="good")
    dark = enhance_photo(img.copy(), quality="dark")
    std_brightness = sum(standard.getpixel((50, 50)))
    dark_brightness = sum(dark.getpixel((50, 50)))
    assert dark_brightness > std_brightness
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance.py -v
```

Expected: FAIL — `photos/enhance.py` doesn't exist yet.

- [ ] **Step 3: Create `photos/enhance.py` with `enhance_photo`**

```python
from __future__ import annotations
from PIL import Image, ImageEnhance, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def enhance_photo(img: Image.Image, quality: str | None) -> Image.Image:
    """Apply PIL color correction. Dark photos get stronger brightness/contrast params."""
    img = img.convert("RGB")
    if quality == "dark":
        img = ImageOps.autocontrast(img, cutoff=2)
        img = ImageEnhance.Brightness(img).enhance(1.6)
        img = ImageEnhance.Color(img).enhance(1.3)
        img = ImageEnhance.Sharpness(img).enhance(1.1)
    else:
        img = ImageOps.autocontrast(img, cutoff=1)
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(1.2)
        img = ImageEnhance.Sharpness(img).enhance(1.1)
    return img
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance.py -v
```

Expected: PASS all 3.

- [ ] **Step 5: Commit**

```bash
git add photos/enhance.py tests/test_enhance.py
git commit -m "feat: add enhance_photo to photos/enhance.py"
```

---

## Task 2: `make_comparison` in `photos/enhance.py`

**Files:**
- Modify: `photos/enhance.py`
- Modify: `tests/test_enhance.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_enhance.py`:

```python
from photos.enhance import enhance_photo, make_comparison


def test_make_comparison_correct_width():
    orig = _make_image(200, 150)
    enh = _make_image(200, 150)
    result = make_comparison(orig, enh)
    # width = orig.width + 4px divider + enh.width
    assert result.width == 200 + 4 + 200


def test_make_comparison_correct_height():
    orig = _make_image(200, 150)
    enh = _make_image(200, 150)
    result = make_comparison(orig, enh)
    assert result.height == 150
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance.py -k "comparison" -v
```

Expected: FAIL — `make_comparison` not defined.

- [ ] **Step 3: Add `make_comparison` to `photos/enhance.py`**

```python
def make_comparison(original: Image.Image, enhanced: Image.Image) -> Image.Image:
    """Side-by-side image: original left, enhanced right, 4px grey divider."""
    orig = original.convert("RGB")
    enh = enhanced.convert("RGB")

    h = max(orig.height, enh.height)
    if orig.height != h:
        orig = orig.resize((int(orig.width * h / orig.height), h), Image.LANCZOS)
    if enh.height != h:
        enh = enh.resize((int(enh.width * h / enh.height), h), Image.LANCZOS)

    divider = 4
    out = Image.new("RGB", (orig.width + divider + enh.width, h), color=(128, 128, 128))
    out.paste(orig, (0, 0))
    out.paste(enh, (orig.width + divider, 0))
    return out
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance.py -v
```

Expected: PASS all 5.

- [ ] **Step 5: Commit**

```bash
git add photos/enhance.py tests/test_enhance.py
git commit -m "feat: add make_comparison to photos/enhance.py"
```

---

## Task 3: `cmd_enhance` in `photos.py`

**Files:**
- Modify: `photos.py` (add `cmd_enhance`, argparse)
- Create: `tests/test_enhance_cmd.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enhance_cmd.py`:

```python
import argparse, os, sys, importlib.util, time
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
            "INSERT INTO photos (id, path, quality, discarded, described_at) "
            "VALUES (?,?,?,?,?)",
            (r["id"], r["path"], r.get("quality", "good"),
             r.get("discarded", 0), r.get("described_at", 1)),
        )
    conn.commit()
    conn.close()
    return db


def _make_photo(tmp_path, name="IMG_1234.jpg"):
    p = tmp_path / name
    Image.new("RGB", (100, 80), color=(100, 100, 100)).save(str(p))
    return p


def test_cmd_enhance_creates_enhanced_file(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=False))
    assert (tmp_path / "IMG_1234_enhanced.jpg").exists()


def test_cmd_enhance_creates_compare_file(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=False))
    assert (tmp_path / "IMG_1234_compare.jpg").exists()


def test_cmd_enhance_skips_existing(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    enhanced = tmp_path / "IMG_1234_enhanced.jpg"
    compare = tmp_path / "IMG_1234_compare.jpg"
    Image.new("RGB", (10, 10)).save(str(enhanced))
    Image.new("RGB", (10, 10)).save(str(compare))
    mtime_before = enhanced.stat().st_mtime

    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=False))
    assert enhanced.stat().st_mtime == mtime_before


def test_cmd_enhance_force_reruns(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo)}])
    enhanced = tmp_path / "IMG_1234_enhanced.jpg"
    compare = tmp_path / "IMG_1234_compare.jpg"
    Image.new("RGB", (10, 10)).save(str(enhanced))
    Image.new("RGB", (10, 10)).save(str(compare))
    mtime_before = enhanced.stat().st_mtime

    time.sleep(0.05)
    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=True))
    assert enhanced.stat().st_mtime > mtime_before


def test_cmd_enhance_skips_undescribed(tmp_path):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "described_at": None}])
    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=False))
    assert not (tmp_path / "IMG_1234_enhanced.jpg").exists()


def test_cmd_enhance_warns_no_descriptions(tmp_path, capsys):
    photo = _make_photo(tmp_path)
    db = _make_db(tmp_path, [{"id": 1, "path": str(photo), "described_at": None}])
    mod = _load_photos_module()
    mod.cmd_enhance(argparse.Namespace(db=db, force=False))
    out = capsys.readouterr().out
    assert "described" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance_cmd.py -v
```

Expected: FAIL — `cmd_enhance` not defined.

- [ ] **Step 3: Add `cmd_enhance` to `photos.py`**

Add after `cmd_search`:

```python
def cmd_enhance(args):
    from photos.enhance import enhance_photo, make_comparison
    from PIL import Image as _Image

    _VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi'}

    conn = _init_db(args.db)
    try:
        rows = conn.execute(
            "SELECT id, path, quality FROM photos "
            "WHERE discarded=0 AND described_at IS NOT NULL"
        ).fetchall()

        if not rows:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        n_enhanced = n_skipped = 0
        bar = tqdm(rows, unit="photo")
        for _photo_id, photo_path, quality in bar:
            p = Path(photo_path)
            if not p.exists() or p.suffix.lower() in _VIDEO_EXTS:
                n_skipped += 1
                continue

            enhanced_path = p.parent / f"{p.stem}_enhanced.jpg"
            compare_path = p.parent / f"{p.stem}_compare.jpg"

            if not args.force and enhanced_path.exists() and compare_path.exists():
                n_skipped += 1
                continue

            try:
                img = _Image.open(p).convert("RGB")
                enhanced = enhance_photo(img, quality)
                enhanced.save(str(enhanced_path), "JPEG", quality=95)
                comparison = make_comparison(img, enhanced)
                comparison.save(str(compare_path), "JPEG", quality=95)
                n_enhanced += 1
                bar.set_description(p.name[:40])
            except Exception as e:
                print(f"\n  Warning: could not enhance {p.name}: {e}")
                n_skipped += 1

        print(f"\n✓ Enhanced {n_enhanced} photo(s), {n_skipped} skipped")
    finally:
        conn.close()
```

Add argparse entry in `main()` (after the `search` parser):

```python
    en = sub.add_parser("enhance", help="Apply color correction to all described photos")
    en.add_argument("--db", default="photos.db", metavar="DB")
    en.add_argument("--force", action="store_true", help="Re-enhance already-enhanced photos")
```

Update the dispatch dict in `main()`:

```python
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag,
     "search": cmd_search, "enhance": cmd_enhance}[args.subcommand](args)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance_cmd.py -v
```

Expected: PASS all 6.

- [ ] **Step 5: Run full test suite**

```bash
cd /scratch/video_read && python -m pytest tests/test_enhance.py tests/test_enhance_cmd.py tests/test_search.py tests/test_recommend.py tests/test_flag_cmd.py -v
```

Expected: PASS all 52 tests.

- [ ] **Step 6: Smoke test on real data**

```bash
cd /scratch/video_read && python photos.py enhance --db output/photos.db
```

Expected: tqdm progress bar, `✓ Enhanced N photo(s)` at the end. Check a few `_enhanced.jpg` and `_compare.jpg` files appear next to originals in `output/takeout/`.

- [ ] **Step 7: Smoke test CLI help**

```bash
cd /scratch/video_read && python photos.py enhance --help
```

- [ ] **Step 8: Commit**

```bash
git add photos.py tests/test_enhance_cmd.py
git commit -m "feat: add cmd_enhance subcommand to photos.py"
```
