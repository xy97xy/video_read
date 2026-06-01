# Photos Organizer

Organize a Google Photos Takeout export into descriptively named trip folders using a 7-phase pipeline. Claude runs commands, reads output, and names clusters from Qwen captions.

## Prerequisites

Check before starting:

```bash
ls venv/bin/activate
```

If missing:
```bash
bash setup_venv.sh
```

Ask the user: path to their extracted Google Takeout directory.

Working directory for all commands: `/scratch/video_read`

Run `source venv/bin/activate` at the start of every new shell session before any phase. The activation persists within a session — you only need to do this once per session.

Output layout:
```
output/
  photos.db          ← scan + describe state
  clusters.json      ← cluster + naming state
  organized/         ← named trip folders
  to-delete/         ← discarded photos awaiting review
```

---

## Phase 1: Scan

```bash
source venv/bin/activate
python photos.py scan --takeout-dir <dir> --db output/photos.db
```

Report to user: total photos found, how many have GPS, how many have dates. Then proceed.

---

## Phase 2: Describe

```bash
python photos.py describe --db output/photos.db
```

Runs Qwen2.5-VL on all undescribed photos (~12s each on GPU). Already-described photos are skipped — safe to re-run after interruption.

Report: count described. Then proceed.

If Qwen crashes mid-run and a photo has corrupt data, clear it and re-run:
```sql
UPDATE photos SET caption=NULL, quality=NULL, scene=NULL, people=NULL, described_at=NULL WHERE id=<id>;
```
Then re-run `photos.py describe` — it will re-process that photo.

---

## Phase 3: Dedup + copy discards

```bash
yes k | python photos.py dedup --db output/photos.db
```

Auto-accepts the largest file per burst group (`k` = keep recommended). Report: exact duplicates discarded, burst groups thinned.

⚠️ If a burst group has 20+ photos, it may be a metadata artifact (Google Takeout sometimes sets all `taken_at` to the scan date). Flag this to the user.

Then copy all discarded photos to `output/to-delete/` for user review before permanent deletion:

Run with: `python` (venv already activated from Phase 1)

```python
import sqlite3, shutil
from pathlib import Path

conn = sqlite3.connect('output/photos.db')
rows = conn.execute('SELECT path FROM photos WHERE discarded=1').fetchall()
out = Path('output/to-delete')
out.mkdir(exist_ok=True)
copied = 0
for (path,) in rows:
    src = Path(path)
    if not src.exists():
        continue
    dest = out / src.name
    if dest.exists():
        counter = 1
        while True:
            candidate = out / f'{src.stem}_{counter}{src.suffix}'
            if not candidate.exists():
                dest = candidate
                break
            counter += 1
    shutil.copy2(src, dest)
    copied += 1
print(f'Copied {copied} to output/to-delete/')
```

Originals remain untouched in the Takeout directory. Then proceed.

---

## Phase 4: Cluster

```bash
python photos.py cluster --db output/photos.db --output output/clusters.json
```

Report: number of trip clusters found. Then proceed.

If 0 trip clusters are found, all photos may be near the detected home location, or timestamps/GPS may be missing. Check with: `SELECT COUNT(*), SUM(taken_at IS NULL), SUM(lat IS NULL) FROM photos;`

---

## Phase 5: Claude names clusters

Read captions and scenes from the DB for each trip cluster:

```python
import sqlite3, json
from pathlib import Path

conn = sqlite3.connect('output/photos.db')
clusters = json.loads(Path('output/clusters.json').read_text())

for c in clusters:
    if not c.get('is_trip'):
        continue
    ids = c['photo_ids']
    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(f'''
        SELECT caption, scene, people FROM photos
        WHERE id IN ({placeholders}) AND discarded=0 AND described_at IS NOT NULL
    ''', ids).fetchall()
    print(f"=== Cluster {c['id']}: {c['name']} ===")
    for caption, scene, people in rows[:8]:
        print(f'  scene={scene} | people={people}')
        if caption:
            print(f'  caption: {caption[:100]}')
    print()
```

Based on the output, assign a descriptive name to each trip cluster. Examples: "China Trip", "Canada Ski Trip", "Joshua Tree Desert", "New York City". Clusters with no usable descriptions keep their date-range name.

Show the user a table of proposed names and wait for confirmation:

| Cluster | Date Range | Proposed Name |
|---------|------------|---------------|
| 11 | 2024-10-23–2024-11-01 | China Trip |
| 16 | 2025-02-23–2025-02-24 | Canada Ski Trip |
| ... | ... | ... |

After the user confirms the table, construct `name_map` as a Python dict with integer cluster IDs as keys and confirmed name strings as values, then run:

```python
# After user confirms the name table above, fill name_map with confirmed names:
name_map = {
    # e.g. 11: 'China Trip', 16: 'Canada Ski Trip'
    # Fill with integer cluster ID → confirmed name string for each renamed cluster
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
python photos.py organize --db output/photos.db --output-dir output/organized --clusters output/clusters.json
```

Non-trip clusters (home/everyday photos) are organized into monthly folders by `organize` automatically — they appear as `YYYY-MM` folders alongside the named trip folders.

After organizing, remove leftover date-only folders that were replaced by named ones. To identify them: any folder in `output/organized/` whose name matches the pattern `YYYY-MM-DD-YYYY-MM-DD` and whose cluster now has a descriptive name.

```python
import re, shutil, json
from pathlib import Path

clusters = json.loads(Path('output/clusters.json').read_text())
date_range_re = re.compile(r'^\d{4}-\d{2}-\d{2}')

# Derive old date-folder names only for clusters that were renamed to a descriptive name
old_date_folders = set()
for c in clusters:
    if c.get('is_trip') and c.get('start') and c.get('end'):
        if not date_range_re.match(c['name']):  # name was changed from date-range to descriptive
            old_date_folders.add(f"{c['start']}-{c['end']}")

removed = []
for folder in Path('output/organized').iterdir():
    if folder.is_dir() and folder.name in old_date_folders:
        shutil.rmtree(folder)
        removed.append(folder.name)
print(f'Removed {len(removed)} old date folders: {removed}')
```

Report the final folder list to the user:
```bash
ls output/organized/
```
Then proceed to Phase 7.

---

## Phase 7: Summary

Report to user:
- Number of named folders in `output/organized/`
- Total photos in organized folders: `find output/organized -type f | wc -l`
- Number of photos in `output/to-delete/` awaiting review

Remind user: **review `output/to-delete/` and delete manually when satisfied. Nothing is auto-deleted.**
