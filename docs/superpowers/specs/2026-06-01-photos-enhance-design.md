# Photos Enhance — Design

**Date:** 2026-06-01
**Status:** Approved

## Goal

Add `photos.py enhance` — apply PIL/cv2 color correction and brightness enhancement to all
described non-discarded photos. Save an enhanced copy and a side-by-side comparison image
alongside each original, so results can be visually evaluated before deciding to keep them.

Originals are never modified.

---

## Pipeline Position

```
scan → describe → dedup → cluster → review → recommend → [flag] → enhance → organize
```

Enhance is optional and non-destructive — `organize` continues to use original paths unless
explicitly changed.

---

## Architecture

Two new pieces:

- **`photos/enhance.py`** — `enhance_photo(img, quality) -> Image`, `make_comparison(original, enhanced) -> Image`
- **`photos.py`** — `cmd_enhance`, argparse wiring

No new dependencies — PIL and cv2 are already in the venv.

---

## Enhancement Pipeline

`enhance_photo(img: Image, quality: str | None) -> Image`

Two parameter sets based on quality:

**Standard pass** (all photos with `quality != "dark"`):
1. `ImageOps.autocontrast(img, cutoff=1)` — clip 1% from each histogram end
2. `ImageEnhance.Brightness(img).enhance(1.15)` — +15% brightness
3. `ImageEnhance.Color(img).enhance(1.2)` — +20% saturation
4. `ImageEnhance.Sharpness(img).enhance(1.1)` — +10% sharpness

**Dark pass** (`quality == "dark"`):
1. `ImageOps.autocontrast(img, cutoff=2)` — more aggressive clipping
2. `ImageEnhance.Brightness(img).enhance(1.6)` — +60% brightness
3. `ImageEnhance.Color(img).enhance(1.3)` — +30% saturation
4. `ImageEnhance.Sharpness(img).enhance(1.1)` — +10% sharpness

---

## Comparison Image

`make_comparison(original: Image, enhanced: Image) -> Image`

- Resize both to the same height if they differ
- Place original on the left, enhanced on the right
- Add a 4px grey dividing line between them
- Return as a single PIL Image (saved as JPEG)

---

## Subcommand: `enhance`

```
python photos.py enhance [--db photos.db] [--force]
```

1. Query all non-discarded described photos (`described_at IS NOT NULL AND discarded=0`)
2. For each photo:
   - Skip videos (`.mp4`, `.mov`, `.m4v`, `.avi`)
   - Compute output paths:
     - `<stem>_enhanced<ext>` alongside original (e.g. `IMG_1234_enhanced.HEIC` → `IMG_1234_enhanced.jpg`)
     - `<stem>_compare.jpg` alongside original
   - Skip if both output files already exist and `--force` is not set
   - Open image (HEIC supported via pillow-heif already registered in describe.py — same import)
   - Apply `enhance_photo(img, quality)`
   - Save enhanced as JPEG
   - Apply `make_comparison(original_img, enhanced_img)`
   - Save comparison as JPEG
3. Print progress with tqdm
4. Print summary: `✓ Enhanced N photo(s), M skipped`

**Note:** Enhanced files are saved as `.jpg` regardless of original extension to avoid HEIC
write complexity. The filename uses the original stem: `IMG_1234.HEIC` → `IMG_1234_enhanced.jpg`.

---

## Error Handling

- Corrupt/unreadable image: warn and skip, continue with remaining photos
- Disk full during write: raise (let it propagate — user needs to know)
- `described_at IS NULL` photos (videos): silently skipped

---

## Testing

`tests/test_enhance.py`:
- `test_enhance_photo_returns_same_size` — output PIL Image has same dimensions as input
- `test_enhance_photo_standard_changes_pixels` — standard pass produces different pixel values
- `test_enhance_photo_dark_brighter_than_standard` — dark pass produces brighter output than standard pass on same input
- `test_make_comparison_double_width` — output width equals sum of both inputs' widths
- `test_make_comparison_same_height` — output height equals max of both inputs' heights

`tests/test_enhance_cmd.py`:
- `test_cmd_enhance_creates_enhanced_file` — `_enhanced.jpg` created alongside original
- `test_cmd_enhance_creates_compare_file` — `_compare.jpg` created alongside original
- `test_cmd_enhance_skips_existing` — re-run without `--force` skips already-enhanced photos
- `test_cmd_enhance_force_reruns` — `--force` re-processes existing enhanced files
- `test_cmd_enhance_skips_undescribed` — photos with `described_at=NULL` not processed
- `test_cmd_enhance_warns_no_descriptions` — warns and exits if no described photos

---

## Hard Constraints

- Originals never modified or deleted
- Enhanced files are copies (JPEG), not replacements
- `organize` continues to copy original files unless the pipeline is explicitly updated
- Videos always skipped (they have `described_at=NULL`)

---

## Future: InstructIR Upgrade

When ML enhancement is desired, `enhance_photo` can be replaced with an
[InstructIR](https://huggingface.co/marcosv/InstructIR) (ECCV 2024) based implementation:

```python
# Future drop-in replacement — same signature, ML implementation
def enhance_photo(img: Image, quality: str | None) -> Image:
    instruction = "Significantly brighten and enhance" if quality == "dark" else "Enhance exposure and add vibrancy"
    return instructir_enhance(img, instruction)  # loads marcosv/InstructIR via transformers
```

The `make_comparison` function and all CLI/test code remain unchanged.
InstructIR requires torch (already in venv) and downloads ~200MB weights on first run.
