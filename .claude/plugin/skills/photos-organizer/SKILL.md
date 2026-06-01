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

---

## Phase 3: Dedup + copy discards

```bash
yes k | python photos.py dedup --db output/photos.db
```

Auto-accepts the largest file per burst group. Report: exact duplicates discarded, burst groups thinned.

⚠️ If a burst group has 20+ photos, it may be a metadata artifact (Google Takeout sometimes sets all `taken_at` to the scan date). Flag this to the user.

Then copy all discarded photos to `output/to-delete/` for user review before permanent deletion:

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

Originals remain untouched in the Takeout directory. Then proceed.

---

## Phase 4: Cluster

```bash
python photos.py cluster --db output/photos.db --output output/clusters.json
```

Report: number of trip clusters found. Then proceed.

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
        SELECT caption, scene, people, place FROM photos
        WHERE id IN ({placeholders}) AND discarded=0 AND described_at IS NOT NULL
    ''', ids).fetchall()
    print(f"=== Cluster {c['id']}: {c['name']} ===")
    for caption, scene, people, place in rows[:8]:
        print(f'  scene={scene} | place={place}')
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

After user confirms, update `clusters.json`:

```python
names = { <id>: '<name>', ... }  # fill from confirmed table
clusters = json.loads(Path('output/clusters.json').read_text())
for c in clusters:
    if c['id'] in names:
        c['name'] = names[c['id']]
        c['confirmed'] = True
Path('output/clusters.json').write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
print('clusters.json updated')
```

---

## Phase 6: Organize

```bash
python photos.py organize --db output/photos.db --output-dir output/organized --clusters output/clusters.json
```

After organizing, remove leftover date-only folders that were replaced by named ones. To identify them: any folder in `output/organized/` whose name matches the pattern `YYYY-MM-DD-YYYY-MM-DD` and whose cluster now has a descriptive name.

```bash
# Example — adjust to actual renamed clusters
rm -rf output/organized/2024-10-23-2024-11-01
rm -rf output/organized/2025-02-23-2025-02-24
# ... etc for each renamed cluster
```

---

## Phase 7: Summary

Report to user:
- Number of named folders in `output/organized/`
- Total photos copied
- Number of photos in `output/to-delete/` awaiting review

Remind user: **review `output/to-delete/` and delete manually when satisfied. Nothing is auto-deleted.**

---

## Steps

1. Create `.claude/skills/photos-organizer/SKILL.md` with the content above
2. Create `.claude/plugin/skills/photos-organizer/SKILL.md` with identical content
3. Verify both files exist with `ls`
4. Commit with message: `feat: add photos-organizer skill`

Report back: DONE when complete, or BLOCKED with reason if something prevents completion.
