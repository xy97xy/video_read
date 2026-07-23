# Photos Organizer

Organize a Google Photos Takeout export into descriptively named trip folders using a pipeline. Claude runs commands, reads output, and names clusters from Google album names + Qwen captions. Videos cluster alongside photos by date; highlight reels are a separate per-trip step.

---

## Intake: New Photos (Google Takeout or iPhone)

When new photos arrive from any source, run the full intake pipeline in order:

1. **Copy source files** into `output/takeout/` (unzip Google Takeout ZIPs, or copy iPhone camera roll folder)
2. **Scan** to register new files in the DB (additive, safe to re-run):
   ```bash
   venv/bin/python3 photos.py scan --takeout-dir output/takeout --db output/photos.db
   ```
3. **Describe** new photos with Qwen (skips already-described):
   ```bash
   export LD_LIBRARY_PATH="..."
   nohup venv/bin/python3 photos.py describe --db output/photos.db > /tmp/describe.log 2>&1 &
   ```
4. **Auto-junk** to discard receipts, black frames, screenshots:
   ```bash
   venv/bin/python3 photos.py auto-junk --db output/photos.db --to-delete-dir output/to-delete
   ```
5. **Dedup** to remove exact and visual duplicates:
   ```bash
   venv/bin/python3 photos.py dedup --db output/photos.db
   ```
6. **Re-cluster** (back up first!):
   ```bash
   cp output/clusters.json output/clusters.backup.json
   venv/bin/python3 photos.py cluster --db output/photos.db --output output/clusters.json
   ```
7. **Name new clusters** — review Unsure clusters, assign trip names
8. **Organize** — new photos get new hex sequence numbers appended to the global counter:
   ```bash
   venv/bin/python3 photos.py organize --db output/photos.db --output-dir output/organized --clusters output/clusters.json
   ```
   Sequence numbers are permanent and ever-increasing. Re-running organize never changes existing numbers.

9. **Review discards** with the paired contact sheet:
   ```bash
   venv/bin/python3 photos.py show-discards --db output/photos.db --page 1
   ```

**File naming convention**: `YYYYMMDD-{5-hex-seq}-Trip-Name.ext`
- Date from EXIF taken_at (UTC)
- Hex sequence: global chronological counter, unique forever
- Example: `20241024-001c6-China-Trip-Oct-2024.JPG`

---

## Prerequisites

Check before starting:

```bash
ls /scratch/video_read/venv/bin/activate
```

If missing:
```bash
bash setup_venv.sh
```

Working directory for all commands: `/scratch/video_read`

Use full venv path — do NOT `source venv/bin/activate` in background/nohup commands, it doesn't propagate:
```bash
/scratch/video_read/venv/bin/python3 photos.py <command>
```

Output layout:
```
output/
  photos.db          ← all state (scan, describe, dedup, cluster)
  clusters.json      ← cluster assignments + trip names
  organized/         ← named trip folders (photos + videos)
  to-delete/         ← discarded copies awaiting manual deletion
    manifest.csv     ← why each file was discarded
```

---

## Metadata priority

Date: **EXIF DateTimeOriginal → sidecar photoTakenTime → filename (PXL_YYYYMMDD) → mtime**
GPS:  **EXIF GPS → sidecar geoData**

Sidecar is deprioritised for dates because Google Takeout stamps the export date when it can't read EXIF (scan-date artifact). EXIF is the original camera timestamp and is more reliable.

After scanning, run `fix-dates` to repair any photos already in the DB with scan-date artifacts:
```bash
venv/bin/python3 photos.py fix-dates --db output/photos.db
```
This detects days where >50 photos share the same calendar date (likely a batch scan stamp), re-extracts their dates from EXIF/filename, and updates the DB. Safe to re-run.

---

## Phase 1: Scan

```bash
venv/bin/python3 photos.py scan --takeout-dir output/takeout --db output/photos.db
```

Safe to re-run — additive, skips already-scanned files. Run again after extracting new ZIPs.

Report: total photos+videos found, how many have GPS, how many have dates.

**Trash folder**: Google Takeout includes a `Trash/` folder of photos the user already deleted. After scanning, immediately discard them:
```bash
python3 -c "
import sqlite3, shutil
from pathlib import Path
conn = sqlite3.connect('output/photos.db')
to_delete = Path('output/to-delete')
to_delete.mkdir(exist_ok=True)
rows = conn.execute(\"SELECT id, path FROM photos WHERE discarded=0 AND path LIKE '%/Trash/%'\").fetchall()
for pid, path in rows:
    src = Path(path)
    if src.exists():
        dest = to_delete / src.name
        if dest.exists():
            dest = to_delete / (src.stem + f'_{pid}' + src.suffix)
        shutil.copy2(src, dest)
    conn.execute('UPDATE photos SET discarded=1, discard_reason=\"google-trash\" WHERE id=?', (pid,))
conn.commit()
print(f'Discarded {len(rows)} Trash photos')
"
```

**ZIP extraction** (if raw ZIPs not yet extracted):
```bash
unzip -n -d output/takeout <zipfile.zip>
# then re-run scan
```

---

## Phase 2: Describe

Qwen2.5-VL 7B describes every undescribed, non-discarded photo (not videos — those are handled separately at reel time). Run in background with nohup so it survives session close:

```bash
export LD_LIBRARY_PATH="/home/xiaoyu/Scripts/python/.venv/lib/python3.13/site-packages/nvidia/cublas/lib:${LD_LIBRARY_PATH:-}"
nohup venv/bin/python3 photos.py describe --db output/photos.db > /tmp/describe.log 2>&1 &
echo "PID: $!"
```

Check progress: `grep -oE "[0-9]+/[0-9]+" /tmp/describe.log | tail -1`

Already-described photos are skipped — safe to re-run after interruption.

**VRAM management**: Qwen uses ~6-7GB of the 8GB GPU. If OOM:
```bash
# Find stale process holding VRAM
fuser /dev/nvidia0 2>/dev/null | tr ' ' '\n' | xargs -I{} ps -p {} --no-headers -o pid,cmd 2>/dev/null | grep python
kill -9 <PID>
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader  # verify freed
# then restart
```

---

## Phase 2b: Auto-junk

After describe, automatically discard receipts, documents, black/blank frames, accidental shots, and screenshots based on Qwen captions:

```bash
venv/bin/python3 photos.py auto-junk --db output/photos.db --to-delete-dir output/to-delete
```

Detected categories:
- **Receipts/documents**: receipt, invoice, bill total, payment due, handwritten note, text on paper, form
- **Bad frames**: all black, completely black, blank, accidental, out of focus, blurry, lens cap, finger/hand covering
- **Screenshots**: screenshot, screen capture, screen recording

Copies originals to `output/to-delete/` and sets `discard_reason='auto-junk'`. Safe to re-run. Only works after `describe` has run.

---

## Phase 3: Dedup

Fully automated — two passes, no Claude review needed:

```bash
venv/bin/python3 photos.py dedup --db output/photos.db
```

- **Pass 1**: Byte-identical duplicates auto-discarded. Keeps whichever copy has a Qwen description; falls back to lower ID. Videos are never touched.
- **Pass 2**: Visually identical (pHash) duplicates auto-discarded. Catches Google Photos `_enhanced` and `_compare` variants.

After dedup, copy discarded files to staging folder with manifest:
```bash
venv/bin/python3 photos.py export-discarded --db output/photos.db --output-dir output/to-delete
```

Originals remain in `output/takeout/` — nothing is permanently deleted until you manually `rm` the to-delete folder.

**Verification**: every discarded photo traces back to a real kept copy on disk. To spot-check:
```python
import hashlib, sqlite3
def md5(p):
    h = hashlib.md5()
    with open(p,'rb') as f:
        for c in iter(lambda: f.read(65536), b''): h.update(c)
    return h.hexdigest()
# compare md5(discarded_path) == md5(kept_path)
```

---

## Phase 4: Cluster

⚠️ **ALWAYS back up clusters.json before re-clustering** — re-clustering overwrites all named trips:
```bash
cp output/clusters.json output/clusters.backup.json
```

```bash
venv/bin/python3 photos.py cluster --db output/photos.db --output output/clusters.json
```

The `--force` flag is required if confirmed trip names already exist in clusters.json (prevents silent overwrite).

Videos are included automatically — they have `taken_at` timestamps and cluster alongside their trip photos.

If 0 trip clusters found, check timestamps/GPS:
```sql
SELECT COUNT(*), SUM(taken_at IS NULL), SUM(lat IS NULL) FROM photos WHERE discarded=0;
```

---

## Phase 5: Name clusters

Read album names, captions and scenes from the DB for each trip cluster:

```python
import sqlite3, json
from pathlib import Path
from collections import Counter

conn = sqlite3.connect('output/photos.db')
clusters = json.loads(Path('output/clusters.json').read_text())

for c in clusters:
    if not c.get('is_trip'):
        continue
    ids = c['photo_ids']
    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(f'''
        SELECT path, caption, scene, people FROM photos
        WHERE id IN ({placeholders}) AND discarded=0
    ''', ids).fetchall()

    albums = []
    for path, *_ in rows:
        parts = Path(path).parts
        try:
            gp_idx = parts.index('Google Photos')
            album = parts[gp_idx + 1]
            if not album.startswith('Photos from'):
                albums.append(album)
        except (ValueError, IndexError):
            pass
    top_albums = [a for a, _ in Counter(albums).most_common(3)]

    print(f"=== Cluster {c['id']}: {c['name']} ===")
    if top_albums:
        print(f"  albums: {', '.join(top_albums)}")
    for _, caption, scene, people in rows[:6]:
        if caption:
            print(f'  scene={scene} | {caption[:100]}')
    print()
```

**Naming priority**:
1. **Google Photos album names** — user-created, most reliable (e.g. `China-10-18-24` → "China Trip", `EU Invasion 7-2025` → "Europe Trip July 2025", `Whistler` → "Whistler Ski Trip")
2. **Qwen captions + scene** — fall back when album is generic (`Photos from YYYY`) or absent
3. **If unsure** — name the cluster `"Unsure-<date-range>"` and present it to the user for manual naming. Never guess a trip name when the signal is weak.

Show user a confirmation table:

| Cluster | Date Range | Album | Proposed Name |
|---------|------------|-------|---------------|
| 11 | 2024-10-23–2024-11-01 | China-10-18-24 | China Trip |
| 16 | 2025-07-01–2025-07-14 | EU Invasion 7-2025 | Europe Trip July 2025 |
| 22 | 2025-09-04–2025-09-06 | Photos from 2025 | Unsure-2025-09-04 |

After confirmation, apply names:

```python
name_map = {
    11: 'China Trip',
    16: 'Europe Trip July 2025',
    # integer cluster ID → confirmed name
}
for c in clusters:
    if c['id'] in name_map:
        c['name'] = name_map[c['id']]
        c['confirmed'] = True
Path('output/clusters.json').write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
print('clusters.json updated')
```

---

## Phase 6: Organize

```bash
venv/bin/python3 photos.py organize --db output/photos.db --output-dir output/organized --clusters output/clusters.json
```

Copies photos **and videos** into named trip folders. Non-trip clusters appear as `YYYY-MM` monthly folders. Organize is copy-based — originals stay in `output/takeout/`.

Remove stale old-name folders if clusters were renamed:
```bash
# e.g. if Canada-Ski-Trip was renamed to Whistler-Ski-Trip:
rm -rf output/organized/Canada-Ski-Trip
```

---

## Phase 7: Export for Google Photos re-upload

Generate a Google Takeout-style export with sidecar `.json` metadata files so Google Photos preserves dates, GPS, captions, and album names on re-upload:

```bash
venv/bin/python3 photos.py export-takeout \
  --db output/photos.db \
  --clusters output/clusters.json \
  --organized-dir output/organized \
  --output-dir output/export
```

Output structure:
```
output/export/
  China-Trip-Oct-2024/
    20241023-00c1a-China-Trip-Oct-2024.JPG
    20241023-00c1a-China-Trip-Oct-2024.JPG.json   ← photoTakenTime, geoData, description
    ...
```

Each sidecar `.json` contains:
- `photoTakenTime` — original EXIF timestamp (not scan date)
- `geoData` / `geoDataExif` — GPS coordinates from DB
- `description` — Qwen caption
- `albumData` — trip name as album

**To re-upload**: zip each album folder and upload via Google Photos web UI, or use a bulk uploader that respects Takeout JSON sidecars (e.g. `gphotos-uploader-cli`).

---

## Phase 8: Videos — highlight reels (per trip, on demand)

Videos are organized into trip folders alongside photos. To make a highlight reel for a specific trip, use the **video-highlight-pipeline** skill interactively.

General flow:
1. Identify which trip folder has interesting video footage: `ls output/organized/<Trip-Name>/`
2. Invoke `/photos-organizer` → hand off to `video-highlight-pipeline` for that folder
3. Pipeline: batch describe → Claude selects best chunks → cut reel

Do NOT batch-describe all 500+ videos upfront — only process the trips you actually want reels for.

---

## Phase 8: Summary and data safety

Report to user:
- Named trip folders: `ls output/organized/ | grep -v "^20"`
- Total files organized: `find output/organized -type f | wc -l`
- Files in to-delete: `wc -l output/to-delete/manifest.csv`

**Data safety rules — NEVER auto-delete photos**:
- ❌ Never run `rm`, `unlink`, or any destructive command on original photo files
- ❌ Never delete from `output/takeout/` — it is the permanent source of truth
- ✅ Discarded photos go to `output/to-delete/` as copies — user reviews and deletes manually
- ✅ `output/organized/` is copies — safe to regenerate by re-running organize
- ✅ Keep Google Takeout ZIPs in Downloads until all photos confirmed in organized/
- ✅ Back up `clusters.json` before every re-cluster run

The pipeline is copy-only. The user manually deletes `output/to-delete/` after review — Claude never does this automatically.
