# Photos Organizer Skill Design

**Date:** 2026-06-01
**Status:** Approved

---

## Overview

A Claude Code skill that guides Claude through organizing a Google Photos Takeout export into descriptively named trip folders. Claude is the actor — it runs commands, reads output, and names clusters from Qwen captions. The user provides the Takeout directory path and reviews the cluster name table before it is saved.

**Trigger:** User wants to organize a Google Takeout export into named trip folders.

---

## Prerequisites

Before starting, Claude checks:
- `venv/bin/activate` exists — if missing, run `bash setup_venv.sh` first
- User provides path to their extracted Takeout directory

**Working directory:** `/scratch/video_read`

---

## File Layout

```
output/
  photos.db          ← scan + describe state (SQLite)
  clusters.json      ← cluster + naming state
  organized/         ← named trip folders
  to-delete/         ← discarded photos awaiting user review
```

---

## The 7 Phases

Each phase ends with a brief status report before Claude proceeds to the next.

### Phase 1: Scan

```bash
source venv/bin/activate
python photos.py scan --takeout-dir <dir> --db output/photos.db
```

Report: total photos found, how many have GPS, how many have dates.

### Phase 2: Describe

```bash
python photos.py describe --db output/photos.db
```

Runs Qwen2.5-VL on all undescribed photos (~12s each). Already-described photos are skipped automatically — safe to re-run. Report: count described.

### Phase 3: Dedup + copy discards

```bash
yes k | python photos.py dedup --db output/photos.db
```

Auto-accepts the largest file per burst group. Report: exact duplicates discarded, burst groups thinned.

Then copy all `discarded=1` photos to `output/to-delete/` for user review before permanent deletion:

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
        dest = out / f'{src.stem}_{src.parent.name}{src.suffix}'
    shutil.copy2(src, dest)
    copied += 1
print(f'Copied {copied} to output/to-delete/')
```

Originals remain untouched in the Takeout directory.

### Phase 4: Cluster

```bash
python photos.py cluster --db output/photos.db --output output/clusters.json
```

Report: number of trip clusters found.

### Phase 5: Claude names clusters

Claude reads captions, scenes, and places from the DB for each trip cluster:

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
        SELECT caption, scene, people, place FROM photos
        WHERE id IN ({placeholders}) AND discarded=0 AND described_at IS NOT NULL
    ''', ids).fetchall()
    print(f"=== Cluster {c['id']}: {c['name']} ===")
    for caption, scene, people, place in rows[:8]:
        print(f'  scene={scene} | place={place}')
        if caption:
            print(f'  caption: {caption[:100]}')
```

Claude assigns descriptive names (e.g. "China Trip", "Canada Ski Trip", "Joshua Tree Desert") based on the captions and scenes. Show a name table to the user for confirmation, then update `clusters.json`:

```python
names = { <id>: '<name>', ... }  # Claude fills this in
for c in clusters:
    if c['id'] in names:
        c['name'] = names[c['id']]
        c['confirmed'] = True
Path('output/clusters.json').write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
```

Clusters with no usable descriptions keep their date-range name.

### Phase 6: Organize

```bash
python photos.py organize --db output/photos.db --output-dir output/organized --clusters output/clusters.json
```

After organizing, delete any leftover date-only folders that correspond to clusters that now have descriptive names (i.e. folders whose name is a date range matching a renamed cluster).

### Phase 7: Summary

Report:
- Number of named folders in `output/organized/`
- Total photos copied
- Number of photos in `output/to-delete/` awaiting review
- Reminder: review `output/to-delete/` and delete manually when satisfied

---

## Notes

- `describe` is the slowest step (~12s/photo on GPU). It is resumable — re-running skips already-described photos.
- The 34-photo false burst group (timestamps all showing scan date) is a known metadata artifact from Google Takeout. The dedup step may lump unrelated photos together. Flag this if the burst group count seems unusually large.
- `to-delete/` is never auto-deleted. The user must delete it manually.
