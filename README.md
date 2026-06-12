# video_read

Two local AI tools for managing personal media libraries:

- **`pipeline.py`** — video highlight extractor (scene detection → AI descriptions → cut highlights)
- **`photos.py`** — photo library organizer (scan → cluster → deduplicate → describe → search)

Both run fully offline using local models (Qwen2.5-VL-7B). `photos.py` optionally calls Claude as a describe provider.

---

## Requirements

- Python 3.10+
- CUDA GPU (recommended — Qwen2.5-VL is ~16 GB VRAM at 4-bit)
- ffmpeg (for video cutting)

```bash
bash setup_venv.sh
source venv/bin/activate
```

For `photos.py` with Google Photos: place your `credentials.json` (OAuth desktop app) in the project root.

---

## Video highlight pipeline (`pipeline.py`)

Extracts highlights from one or more videos using TransNetV2 scene detection and Qwen2.5-VL scene descriptions.

```bash
# 1. Detect scenes + describe each chunk → chunks.json
python pipeline.py describe video.mp4 --output output/

# 2. Cut selected chunks into a highlight reel
python pipeline.py cut video.mp4 --chunks output/chunks.json --select 2,5,8 --output highlight.mp4

# 3. Score chunks by energy / loudness / AI quality
python pipeline.py score --chunks output/chunks.json

# 4. Batch describe multiple videos
python pipeline.py batch --video-dir /path/to/videos/ --output output/

# 5. Merge per-video chunks.json into one
python pipeline.py merge output/video1_chunks.json output/video2_chunks.json
```

Each chunk in `chunks.json` has: `start`, `end`, `description`, `score`, `thumbnail`.

---

## Photo library organizer (`photos.py`)

Manages a local photo/video library backed by SQLite (`photos.db`). Designed for Google Takeout exports but works with any directory of JPEGs, HEICs, and MP4s.

### Workflow

```bash
# 1. Scan a directory → photos.db
python photos.py scan --dir /path/to/photos/

# 2. Cluster photos into trips by date + GPS
python photos.py cluster

# 3. Interactively review and name trip clusters
python photos.py review

# 4. Copy into organized folders (by trip name or YYYY-MM)
python photos.py organize --dest /path/to/organized/

# 5. Find and discard duplicates (exact + perceptual hash)
python photos.py dedup

# 6. AI-describe photos (Qwen or Claude)
python photos.py describe --provider qwen
python photos.py describe --provider claude

# 7. Full-text search by description
python photos.py search "sunset on the water"

# 8. Auto-flag low-quality photos, write HTML report
python photos.py recommend

# 9. Apply color correction to described photos
python photos.py enhance

# 10. Export discarded photos for manual review
python photos.py export-discarded --dest /path/to/review/
```

### Database

`photos.db` is a local SQLite file (gitignored). Schema: `photos` table with `path`, `taken_at`, `lat`, `lon`, `cluster_id`, `description`, `score`, `flagged`, `discarded`.

---

## Architecture

```
pipeline.py          # video pipeline (self-contained)
photos.py            # photo CLI entry point
photos/
  metadata.py        # EXIF extraction, GPS reverse geocoding
  cluster.py         # date+GPS trip clustering
  dedup.py           # exact + perceptual hash dedup
  describe.py        # Qwen2.5-VL + Claude describe providers
  recommend.py       # quality scoring + HTML report
  search.py          # SQLite FTS5 full-text search
  enhance.py         # color correction
```

---

## License

MIT
