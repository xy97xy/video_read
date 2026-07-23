#!/usr/bin/env python3
import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from photos.metadata import find_media_files, extract_metadata, reverse_geocode
from photos.cluster import build_clusters
from photos.dedup import find_exact_duplicates, find_phash_duplicates

log = logging.getLogger(__name__)


def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id           INTEGER PRIMARY KEY,
            path         TEXT UNIQUE,
            taken_at     INTEGER,
            lat          REAL,
            lon          REAL,
            place        TEXT,
            cluster_id   INTEGER,
            discarded    INTEGER DEFAULT 0,
            caption      TEXT,
            quality      TEXT,
            scene        TEXT,
            people       TEXT,
            described_at INTEGER,
            flagged      INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_scenes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id    INTEGER NOT NULL REFERENCES photos(id),
            start_sec   REAL NOT NULL,
            end_sec     REAL NOT NULL,
            caption     TEXT,
            score       REAL,
            created_at  INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_scenes_photo_id ON video_scenes (photo_id)"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    migrations = [
        ("discarded",    "ALTER TABLE photos ADD COLUMN discarded    INTEGER DEFAULT 0"),
        ("caption",      "ALTER TABLE photos ADD COLUMN caption      TEXT"),
        ("quality",      "ALTER TABLE photos ADD COLUMN quality      TEXT"),
        ("scene",        "ALTER TABLE photos ADD COLUMN scene        TEXT"),
        ("people",       "ALTER TABLE photos ADD COLUMN people       TEXT"),
        ("described_at", "ALTER TABLE photos ADD COLUMN described_at INTEGER"),
        ("flagged",      "ALTER TABLE photos ADD COLUMN flagged      INTEGER DEFAULT 0"),
        ("discard_reason", "ALTER TABLE photos ADD COLUMN discard_reason TEXT"),
        ("organized_seq",  "ALTER TABLE photos ADD COLUMN organized_seq  INTEGER"),
    ]
    for col, sql in migrations:
        if col not in cols:
            conn.execute(sql)
    conn.commit()
    return conn


def cmd_scan(args):
    conn = _init_db(args.db)
    files = find_media_files(args.takeout_dir)
    print(f"Found {len(files)} media files — scanning...")

    geocode_cache: dict[tuple, str | None] = {}
    n_gps = 0
    n_dated = 0

    try:
        for path in tqdm(files, unit="photo"):
            taken_at, lat, lon = extract_metadata(path)
            place = None

            if lat is not None:
                n_gps += 1
                cell = (round(lat / 0.1) * 0.1, round(lon / 0.1) * 0.1)
                if cell not in geocode_cache:
                    geocode_cache[cell] = reverse_geocode(lat, lon)
                place = geocode_cache[cell]

            if taken_at:
                n_dated += 1

            conn.execute(
                "INSERT OR IGNORE INTO photos (path, taken_at, lat, lon, place) VALUES (?,?,?,?,?)",
                (str(path), taken_at, lat, lon, place)
            )

        conn.commit()
    finally:
        conn.close()

    total = len(files)
    print(f"\n✓ Scanned {total} photos")
    print(f"  {n_dated}/{total} have date info")
    print(f"  {n_gps}/{total}  have GPS")
    print(f"  Saved to {args.db}")


def cmd_fix_dates(args):
    """Re-extract dates/GPS for photos whose taken_at looks like a scan-date artifact."""
    from photos.metadata import extract_metadata
    conn = _init_db(args.db)

    # Detect scan-date artifact days: calendar days shared by >50 photos.
    # Google Takeout stamps batches with the export date when EXIF is unreadable.
    rows = conn.execute("""
        SELECT date(taken_at, 'unixepoch'), COUNT(*)
        FROM photos WHERE discarded=0
        GROUP BY date(taken_at, 'unixepoch')
        HAVING COUNT(*) > 50
        ORDER BY COUNT(*) DESC
    """).fetchall()
    suspect_day_strs = {r[0] for r in rows}
    print(f"Suspect bulk-assigned days: {sorted(suspect_day_strs)}")

    targets = conn.execute("""
        SELECT id, path FROM photos
        WHERE taken_at IS NULL
           OR date(taken_at, 'unixepoch') IN ({})
    """.format(','.join('?' * len(suspect_day_strs))),
        list(suspect_day_strs)
    ).fetchall()

    print(f"Re-checking {len(targets)} photos...")
    n_fixed = 0
    for pid, path in tqdm(targets, unit="photo"):
        p = Path(path)
        if not p.exists():
            continue
        new_date, new_lat, new_lon = extract_metadata(p)
        old_row = conn.execute("SELECT taken_at, lat FROM photos WHERE id=?", (pid,)).fetchone()
        old_date = old_row[0]
        if new_date != old_date or (new_lat is not None and old_row[1] is None):
            conn.execute(
                "UPDATE photos SET taken_at=?, lat=?, lon=? WHERE id=?",
                (new_date, new_lat, new_lon, pid)
            )
            n_fixed += 1

    conn.commit()
    conn.close()
    print(f"✓ Fixed {n_fixed} photos")


def cmd_cluster(args):
    clusters_path = Path(args.clusters)
    if clusters_path.exists() and not getattr(args, "force", False):
        existing = json.loads(clusters_path.read_text())
        confirmed_trips = [c for c in existing if c.get("is_trip") and c.get("confirmed")]
        if confirmed_trips:
            print(f"⚠ {clusters_path} has {len(confirmed_trips)} confirmed trip(s). Use --force to overwrite.")
            return

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT id, path, taken_at, lat, lon, place FROM photos WHERE discarded=0"
    ).fetchall()
    conn.close()

    photos = [
        {"id": r[0], "path": r[1], "taken_at": r[2],
         "lat": r[3], "lon": r[4], "place": r[5]}
        for r in rows
    ]

    clusters = build_clusters(photos, gap_days=args.gap_days, radius_km=args.radius_km)

    conn = sqlite3.connect(args.db)
    try:
        for c in clusters:
            for pid in c["photo_ids"]:
                conn.execute("UPDATE photos SET cluster_id=? WHERE id=?", (c["id"], pid))
        conn.commit()
    finally:
        conn.close()

    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))

    trips    = sum(1 for c in clusters if c["is_trip"])
    catchall = sum(1 for c in clusters if not c["is_trip"])
    total    = sum(c["photo_count"] for c in clusters)
    print(f"✓ {len(clusters)} clusters from {total} photos")
    print(f"  {trips} trip(s), {catchall} monthly/catch-all")
    print(f"  Saved to {args.clusters}")


def cmd_review(args):
    clusters = json.loads(Path(args.clusters).read_text())
    pending = [c for c in clusters if c.get("is_trip") and not c.get("confirmed")]

    if not pending:
        print(f"Nothing to review — 0 unconfirmed trips in {args.clusters}")
        return

    if getattr(args, "yes", False):
        for c in pending:
            c["confirmed"] = True
        Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
        print(f"✓ Auto-confirmed {len(pending)} trip cluster(s)")
        return

    print(f"{len(pending)} trip cluster(s) to review.\n")
    for c in pending:
        print(f"Cluster: {c['name']}  ({c['photo_count']} photos,  {c['start']} → {c['end']})")
        print("Actions: [c]onfirm  [r]ename  [d]iscard  [s]kip  [?]help")

        while True:
            try:
                action = input("> ").strip().lower()
            except EOFError:
                print("\nAborted.")
                Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
                return
            if action == "?":
                print("  c = confirm as trip")
                print("  r = rename (you choose the album name)")
                print("  d = discard (moves photos to monthly catch-all)")
                print("  s = skip for now (leave unconfirmed)")
            elif action == "c":
                c["confirmed"] = True
                print(f"  ✓ Confirmed: {c['name']}")
                break
            elif action == "r":
                try:
                    new_name = input("  New name: ").strip()
                except EOFError:
                    print("\nAborted.")
                    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
                    return
                if new_name:
                    c["name"] = new_name
                c["confirmed"] = True
                print(f"  ✓ Renamed to: {c['name']}")
                break
            elif action == "d":
                c["is_trip"] = False
                c["confirmed"] = True
                print(f"  ✗ Discarded — photos will go to monthly catch-all")
                break
            elif action == "s":
                print(f"  → Skipped")
                break
            else:
                print("  Unknown action. Type ? for help.")
        print()

    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
    confirmed = sum(1 for c in clusters if c.get("confirmed") and c.get("is_trip"))
    print(f"✓ Saved. {confirmed} confirmed trip(s).")


def _sanitize(name: str) -> str:
    name = name.replace('–', '-').replace('—', '-')
    name = re.sub(r'[^\w\-]', '-', name)
    name = re.sub(r'-+', '-', name)
    return name.strip('-')


def _dest_path(folder: Path, filename: str) -> Path:
    dest = folder / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = folder / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def cmd_organize(args):
    from datetime import datetime, timezone as _tz

    clusters = json.loads(Path(args.clusters).read_text())
    conn = _init_db(args.db)
    # Load ALL photos (kept + discarded) — everyone gets a seq number
    id_to_row = {r[0]: r for r in conn.execute(
        "SELECT id, path, taken_at, organized_seq, discarded FROM photos"
    )}

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    confirmed_cluster_ids = {c["id"] for c in clusters if c.get("confirmed")}
    # Include photos via photo_ids list AND all photos with any cluster_id (catches dedup'd + merged clusters)
    all_ids_set = {pid for c in clusters if c.get("confirmed") for pid in c["photo_ids"]}
    extra = conn.execute("SELECT id FROM photos WHERE cluster_id IS NOT NULL").fetchall()
    all_ids_set.update(r[0] for r in extra)
    all_ids = list(all_ids_set)

    def _taken_ts(pid):
        row = id_to_row.get(pid)
        ta = row[2] if row else None
        return ta if isinstance(ta, (int, float)) else float("inf")

    def _date_str(pid):
        row = id_to_row.get(pid)
        ta = row[2] if row else None
        if isinstance(ta, (int, float)):
            try:
                return datetime.fromtimestamp(ta, tz=_tz.utc).strftime("%Y%m%d")
            except Exception:
                pass
        return "00000000"

    # Load existing sequences; assign new ones to photos that don't have one yet
    existing_seq = {pid: id_to_row[pid][3] for pid in all_ids
                    if pid in id_to_row and id_to_row[pid][3] is not None}
    unsequenced = sorted(
        [pid for pid in all_ids if pid not in existing_seq],
        key=lambda pid: (_taken_ts(pid), pid),
    )
    next_seq = max(existing_seq.values(), default=-1) + 1
    new_assignments = {pid: next_seq + i for i, pid in enumerate(unsequenced)}

    # Persist new assignments
    for pid, seq in new_assignments.items():
        conn.execute("UPDATE photos SET organized_seq=? WHERE id=?", (seq, pid))
    conn.commit()

    seq_map = {**existing_seq, **new_assignments}
    max_seq = max(seq_map.values(), default=0)
    n_hex = max(5, len(format(max_seq, "x")))

    def _new_filename(pid, cluster_name, orig_path):
        seq = format(seq_map.get(pid, 0), f"0{n_hex}x")
        ext = Path(orig_path).suffix
        trip = _sanitize(cluster_name)
        return f"{_date_str(pid)}-{seq}-{trip}{ext}"

    n_copied = n_skipped = n_new_seq = 0
    for c in clusters:
        if not c.get("confirmed"):
            continue
        folder_name = _sanitize(c["name"])
        folder = out / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        for pid in c["photo_ids"]:
            row = id_to_row.get(pid)
            if not row:
                n_skipped += 1
                continue
            is_discarded = row[4]
            if is_discarded:
                continue  # seq already assigned; don't copy to organized/
            src = row[1]
            p = Path(src)
            if not p.exists():
                p = Path("/scratch/video_read") / src.lstrip("/")
            if not p.exists():
                n_skipped += 1
                continue
            new_name = _new_filename(pid, c["name"], p)
            dest = folder / new_name
            if dest.exists():
                dest = dest.with_name(dest.stem + "_dup" + dest.suffix)
            shutil.copy2(str(p), dest)
            n_copied += 1

    conn.close()
    confirmed = sum(1 for c in clusters if c.get("confirmed"))
    print(f"✓ {n_copied} photos copied into {confirmed} folder(s) under {args.output_dir}")
    if new_assignments:
        print(f"  {len(new_assignments)} new sequence numbers assigned (next: {format(next_seq + len(new_assignments), f'0{n_hex}x')})")
    if n_skipped:
        print(f"  {n_skipped} skipped (source file not found)")


_VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv'}


def _dedup_keep(group: list[dict], conn) -> dict:
    """Pick the copy to keep: prefer described, then lower ID."""
    described = [p for p in group if conn.execute(
        "SELECT described_at FROM photos WHERE id=?", (p["id"],)
    ).fetchone()[0]]
    return described[0] if described else min(group, key=lambda p: p["id"])


def cmd_dedup(args):
    conn = _init_db(args.db)
    try:
        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows
                  if Path(r[1]).suffix.lower() not in _VIDEO_EXTS]

        # Pass 1: byte-identical duplicates
        dup_groups = find_exact_duplicates(photos)
        n_exact = 0
        for group in dup_groups:
            keep = _dedup_keep(group, conn)
            keep_name = Path(keep["path"]).name
            for p in group:
                if p["id"] != keep["id"]:
                    conn.execute(
                        "UPDATE photos SET discarded=1, discard_reason=? WHERE id=?",
                        (f"exact duplicate of {keep_name}", p["id"]),
                    )
                    n_exact += 1
        conn.commit()
        print(f"✓ Pass 1: auto-discarded {n_exact} byte-identical duplicate(s)")

        # Pass 2: visually identical (pHash distance = 0)
        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows
                  if Path(r[1]).suffix.lower() not in _VIDEO_EXTS]
        print(f"Computing pHash for {len(photos)} photos...")
        phash_groups = find_phash_duplicates(photos)
        n_phash = 0
        for group in phash_groups:
            # Keep both originals and Google _enhanced/_compare variants — never dedup them
            if any("_enhanced" in Path(p["path"]).name or "_compare" in Path(p["path"]).name
                   for p in group):
                continue
            keep = _dedup_keep(group, conn)
            keep_name = Path(keep["path"]).name
            for p in group:
                if p["id"] != keep["id"]:
                    conn.execute(
                        "UPDATE photos SET discarded=1, discard_reason=? WHERE id=?",
                        (f"visually identical to {keep_name}", p["id"]),
                    )
                    n_phash += 1
        conn.commit()
        print(f"✓ Pass 2: auto-discarded {n_phash} visually identical duplicate(s)")
        print(f"  Remaining photos: Qwen + Claude handle quality decisions")
    finally:
        conn.close()


def _cmd_benchmark(conn, args):
    import asyncio
    from photos.describe import ClaudeDescriber

    rows = conn.execute(
        "SELECT id, path, caption, scene, people, quality FROM photos "
        "WHERE described_at IS NOT NULL AND discarded=0 ORDER BY RANDOM() LIMIT 20"
    ).fetchall()

    if not rows:
        print("⚠ No described photos found. Run describe first.")
        return

    photos = [{"id": r[0], "path": r[1]} for r in rows]
    existing = {r[0]: {"caption": r[2], "scene": r[3], "people": r[4], "quality": r[5]} for r in rows}

    n = len(photos)
    print(f"Benchmarking {n} photos across providers...\n")

    # Claude haiku
    t0 = time.time()
    haiku = ClaudeDescriber(model="haiku", workers=getattr(args, "workers", 5))
    haiku_results = asyncio.run(haiku.describe_batch(photos))
    haiku_time = time.time() - t0

    # Claude sonnet
    t0 = time.time()
    sonnet = ClaudeDescriber(model="sonnet", workers=getattr(args, "workers", 5))
    sonnet_results = asyncio.run(sonnet.describe_batch(photos))
    sonnet_time = time.time() - t0

    print(f"claude-haiku:  {haiku_time:.1f}s total  ({haiku_time/n:.1f}s/photo)")
    print(f"claude-sonnet: {sonnet_time:.1f}s total  ({sonnet_time/n:.1f}s/photo)")
    print()

    # Side-by-side caption comparison for first 5 photos
    print("--- Caption comparison (first 5 photos) ---")
    for i, photo in enumerate(photos[:5]):
        print(f"\nPhoto: {Path(photo['path']).name}")
        qwen_cap = existing[photo["id"]].get("caption") or "(none)"
        haiku_cap = haiku_results[i].get("caption") or "(none)"
        sonnet_cap = sonnet_results[i].get("caption") or "(none)"
        print(f"  [qwen (existing)] {qwen_cap}")
        print(f"  [claude-haiku   ] {haiku_cap}")
        print(f"  [claude-sonnet  ] {sonnet_cap}")

    print("\n✓ Benchmark complete. No DB writes performed.")


def cmd_describe(args):
    import asyncio
    from photos.describe import load_qwen, describe_photo, ClaudeDescriber, describe_video

    conn = _init_db(args.db)
    provider = getattr(args, "provider", "qwen")
    benchmark = getattr(args, "benchmark", False)

    try:
        if benchmark:
            _cmd_benchmark(conn, args)
            return

        if getattr(args, "force", False):
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0 AND described_at IS NULL"
            ).fetchall()

        if not rows:
            print("✓ Nothing left to describe. Use --force to re-describe.")
            return

        all_items = [{"id": r[0], "path": r[1]} for r in rows if Path(r[1]).exists()]
        photo_rows = [r for r in all_items if Path(r["path"]).suffix.lower() not in _VIDEO_EXTS]
        video_rows = [r for r in all_items if Path(r["path"]).suffix.lower() in _VIDEO_EXTS]

        # Claude handles both photos and videos; Qwen needed only for its own provider + photos, or videos
        need_qwen = (provider == "qwen" and photo_rows) or (provider != "claude" and bool(video_rows))
        model = processor = None
        if need_qwen:
            n_qwen = (len(photo_rows) if provider == "qwen" else 0) + (len(video_rows) if provider != "claude" else 0)
            print(f"Loading Qwen2.5-VL ({n_qwen} item(s) to describe)...")
            t0 = time.time()
            model, processor = load_qwen()
            print(f"Model loaded in {time.time() - t0:.0f}s")

        describer = None
        if provider == "claude":
            describer = ClaudeDescriber(
                model=getattr(args, "model", "haiku"),
                workers=getattr(args, "workers", 5),
            )

        if provider == "claude" and photo_rows:
            print(f"Describing {len(photo_rows)} photos with Claude ({describer.model}, {describer.workers} workers)...")
            results = asyncio.run(describer.describe_batch(photo_rows))
            n_described = 0
            for photo, result in zip(photo_rows, results):
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                n_described += 1
            conn.commit()
            print(f"\n✓ Described {n_described} photo(s) with Claude {describer.model}")
        elif photo_rows:
            n_described = 0
            bar = tqdm(photo_rows, unit="photo")
            for photo in bar:
                p = Path(photo["path"])
                bar.set_description(p.name[:40])
                result = describe_photo(model, processor, p)
                conn.execute(
                    "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                    (result["caption"], result["quality"], result["scene"], result["people"],
                     int(time.time()), photo["id"]),
                )
                conn.commit()
                n_described += 1
            print(f"\n✓ Described {n_described} photo(s)")

        if video_rows:
            if provider == "claude":
                print(f"Describing {len(video_rows)} videos with Claude ({describer.model}, {describer.workers} workers)...")
                results = asyncio.run(describer.describe_video_batch(video_rows))
                n_videos = 0
                for video, result in zip(video_rows, results):
                    if result["caption"] is None and not result["scenes"]:
                        log.warning(f"describe_video_claude returned empty for {video['path']}")
                        continue
                    conn.execute(
                        "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                        (result["caption"], result["quality"], result["scene"], result["people"],
                         int(time.time()), video["id"]),
                    )
                    conn.execute("DELETE FROM video_scenes WHERE photo_id=?", (video["id"],))
                    for scene in result["scenes"]:
                        conn.execute(
                            "INSERT INTO video_scenes (photo_id, start_sec, end_sec, caption, score, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (video["id"], scene["start_sec"], scene["end_sec"],
                             scene["caption"], scene["score"], int(time.time())),
                        )
                    conn.commit()
                    n_videos += 1
                print(f"\n✓ Described {n_videos} video(s) with Claude {describer.model}")
            else:
                n_videos = 0
                bar = tqdm(video_rows, unit="video")
                for video in bar:
                    p = Path(video["path"])
                    bar.set_description(p.name[:40])
                    try:
                        result = describe_video(model, processor, p)
                    except Exception as e:
                        log.warning(f"describe_video failed for {p.name}: {e}")
                        continue
                    conn.execute(
                        "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                        (result["caption"], result["quality"], result["scene"], result["people"],
                         int(time.time()), video["id"]),
                    )
                    conn.execute("DELETE FROM video_scenes WHERE photo_id=?", (video["id"],))
                    for scene in result["scenes"]:
                        conn.execute(
                            "INSERT INTO video_scenes (photo_id, start_sec, end_sec, caption, score, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (video["id"], scene["start_sec"], scene["end_sec"],
                             scene["caption"], scene["score"], int(time.time())),
                        )
                    conn.commit()
                    n_videos += 1
                print(f"\n✓ Described {n_videos} video(s)")
    finally:
        conn.close()


def cmd_recommend(args):
    from photos.recommend import auto_flag_quality, build_report

    conn = _init_db(args.db)
    try:
        n_described = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE described_at IS NOT NULL AND discarded=0"
        ).fetchone()[0]
        if n_described == 0:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        n = auto_flag_quality(conn)
        print(f"✓ Auto-flagged {n} photo(s) with non-good quality")

        clusters_path = Path(args.clusters) if Path(args.clusters).exists() else None
        output_path = build_report(conn, clusters_path, Path(args.output))
        print(f"✓ Report written to {output_path}")
        print(f"  Review it, then run: python photos.py flag <id> [id ...]")
    finally:
        conn.close()


def cmd_auto_junk(args):
    from photos.recommend import auto_discard_junk

    conn = _init_db(args.db)
    try:
        n_described = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE described_at IS NOT NULL AND discarded=0"
        ).fetchone()[0]
        if n_described == 0:
            print("⚠ No described photos found. Run describe first.")
            return

        to_delete = Path(args.to_delete_dir)
        n = auto_discard_junk(conn, to_delete)
        print(f"✓ Auto-discarded {n} junk photo(s) → {to_delete}/")
        print(f"  Categories: receipts, documents, black frames, accidental shots, screenshots")
    finally:
        conn.close()


def cmd_flag(args):
    import shutil as _shutil
    from photos.recommend import set_flagged

    ids = args.ids
    conn = _init_db(args.db)
    try:
        result = set_flagged(conn, ids, flag=not args.unflag)

        if args.unflag:
            out = Path(args.output_dir)
            cluster_names: dict[int, str] = {}
            clusters_path = Path(args.clusters)
            if clusters_path.exists():
                for c in json.loads(clusters_path.read_text()):
                    cluster_names[c["id"]] = c["name"]
            for pid in result["done"]:
                row = conn.execute(
                    "SELECT path, cluster_id FROM photos WHERE id=?", (pid,)
                ).fetchone()
                if not row:
                    continue
                src_path, cluster_id = row
                raw = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
                cname = _sanitize(raw) if cluster_id else "unclustered"
                cluster_dir = out / cname
                fname = Path(src_path).name
                for f in cluster_dir.rglob(fname):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            print(f"✓ Unflagged {len(result['done'])} photo(s)")
        else:
            cluster_names: dict[int, str] = {}
            clusters_path = Path(args.clusters)
            if clusters_path.exists():
                for c in json.loads(clusters_path.read_text()):
                    cluster_names[c["id"]] = c["name"]

            out = Path(args.output_dir)
            n_copied = 0
            for pid in result["done"]:
                row = conn.execute(
                    "SELECT path, cluster_id FROM photos WHERE id=?", (pid,)
                ).fetchone()
                if not row:
                    continue
                src, cluster_id = row
                raw = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
                cname = _sanitize(raw) if cluster_id else "unclustered"
                dest_dir = out / cname
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _dest_path(dest_dir, Path(src).name)
                try:
                    _shutil.copy2(src, dest)
                    n_copied += 1
                except OSError as e:
                    print(f"  Warning: could not copy {src}: {e}")

            print(f"✓ Flagged {len(result['done'])} photo(s), {n_copied} copied to {args.output_dir}")

        for pid, reason in result["skipped"]:
            print(f"  Warning: skipped photo {pid} ({reason})")
        for pid in result["not_found"]:
            print(f"  Warning: photo {pid} not found in DB")
    finally:
        conn.close()


def cmd_search(args):
    import sqlite3 as _sqlite3
    from photos.search import build_fts, search_photos

    conn = _init_db(args.db)
    try:
        n_described = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE described_at IS NOT NULL AND discarded=0"
        ).fetchone()[0]
        if n_described == 0:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        build_fts(conn)

        try:
            results = search_photos(conn, args.query, args.limit)
        except _sqlite3.OperationalError as e:
            print(f"Search error: {e}")
            print(f"  Query was: {args.query}")
            return

        if not results:
            print(f'No photos found matching "{args.query}"')
            return

        print(f'Found {len(results)} photo(s) matching "{args.query}"\n')
        header = f" {'id':>4} | {'score':>6} | {'file':<25} | {'scene':<18} | {'place':<15} | caption"
        print(header)
        print("-" * len(header))
        for r in results:
            fname = Path(r["path"]).name
            print(
                f" {r['id']:>4} | {r['score']:>6.2f} | {fname:<25} | "
                f"{(r['scene'] or ''):<18} | {(r['place'] or ''):<15} | "
                f"{(r['caption'] or '')[:60]}"
            )

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'# Search Results: "{args.query}"',
            f"Generated: {datetime.now().strftime('%Y-%m-%d')}  |  {len(results)} results",
            "",
            "| id | score | file | scene | place | caption |",
            "|----|-------|------|-------|-------|---------|",
        ]
        for r in results:
            fname = Path(r["path"]).name
            caption_short = (r["caption"] or "")[:80]
            lines.append(
                f"| {r['id']} | {r['score']:.2f} | {fname} | "
                f"{r['scene'] or ''} | {r['place'] or ''} | {caption_short} |"
            )
        out.write_text("\n".join(lines))
        print(f"\nSaved to {out}")
    finally:
        conn.close()


def cmd_show(args):
    """Generate a contact sheet for a cluster and open it in imv."""
    import subprocess
    import tempfile
    from datetime import timezone
    from PIL import Image, ImageDraw, ImageFont

    conn = _init_db(args.db)
    clusters = json.loads(Path(args.clusters).read_text())

    all_ids = []
    label = []
    for cid in args.cluster_ids:
        cluster = next((c for c in clusters if c["id"] == cid), None)
        if cluster is None:
            print(f"Cluster {cid} not found.")
            return
        all_ids.extend(cluster["photo_ids"])
        label.append(cluster["name"])

    ids = all_ids
    VIDEO_EXTS = {".mp4", ".mov", ".MP4", ".MOV"}
    rows = conn.execute(
        "SELECT path, caption, taken_at FROM photos WHERE id IN ({}) AND discarded=0".format(
            ",".join("?" * len(ids))
        ),
        ids,
    ).fetchall()
    rows = [r for r in rows if Path(r[0]).suffix not in VIDEO_EXTS]
    rows = sorted(rows, key=lambda r: r[2] or 0)

    if not rows:
        print("No photos in cluster.")
        return

    # --- detect screen size via xrandr ---
    screen_w, screen_h = 1920, 1080
    try:
        xr = subprocess.run(["xrandr", "--current"], capture_output=True, text=True)
        for line in xr.stdout.splitlines():
            m = re.search(r"(\d{3,5})x(\d{3,5})\s+\d+\.\d+\*", line)
            if m:
                screen_w, screen_h = int(m.group(1)), int(m.group(2))
                break
    except Exception:
        pass

    n = len(rows)
    import math
    cols = math.ceil(math.sqrt(n))
    rows_count = math.ceil(n / cols)

    CAPTION_H = 80  # px per cell for text strip
    cell_w = screen_w // cols
    cell_h = (screen_h - CAPTION_H * rows_count) // rows_count
    cell_h = max(cell_h, 80)

    try:
        font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 11)
    except Exception:
        font = ImageFont.load_default()

    sheet_w = cell_w * cols
    sheet_h = (cell_h + CAPTION_H) * rows_count
    sheet = Image.new("RGB", (sheet_w, sheet_h), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)

    for idx, (path, caption, taken_at) in enumerate(rows):
        col = idx % cols
        row = idx // cols
        x = col * cell_w
        y = row * (cell_h + CAPTION_H)

        # thumbnail — scale-to-fit, centered, no cropping
        try:
            from PIL import ImageOps
            img = Image.open(path)
            img = ImageOps.contain(img, (cell_w, cell_h), Image.LANCZOS)
            ox = (cell_w - img.width) // 2
            oy = (cell_h - img.height) // 2
            sheet.paste(img, (x + ox, y + oy))
        except Exception:
            draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(60, 60, 60))
            draw.text((x + 4, y + cell_h // 2), "⚠ load error", fill=(200, 80, 80), font=font)

        # caption strip
        cy = y + cell_h
        draw.rectangle([x, cy, x + cell_w, cy + CAPTION_H], fill=(20, 20, 20))
        dt_str = ""
        if taken_at:
            dt_str = datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        fname = Path(path).name
        header = f"{fname}  {dt_str}" if dt_str else fname
        draw.text((x + 3, cy + 2), header, fill=(160, 200, 255), font=font)
        # wrap caption across up to 3 lines
        cap_text = (caption or "")[:300]
        words = cap_text.split()
        lines, line = [], ""
        for w in words:
            test = (line + " " + w).strip()
            if font.getlength(test) > cell_w - 6:
                lines.append(line)
                line = w
            else:
                line = test
        if line:
            lines.append(line)
        for li, l in enumerate(lines[:5]):
            draw.text((x + 3, cy + 16 + li * 13), l, fill=(220, 220, 220), font=font)

    cids_str = "_".join(str(c) for c in args.cluster_ids)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", prefix=f"cluster_{cids_str}_", delete=False)
    sheet.save(tmp.name, quality=90)
    tmp.close()
    print(f"Contact sheet → {tmp.name}  ({cols}×{rows_count} grid, {n} photos) — {', '.join(label)}")
    subprocess.run(["pkill", "imv"], capture_output=True)
    subprocess.Popen(["imv", tmp.name])


def cmd_show_discards(args):
    """Show contact sheet of discarded duplicate pairs (kept + discarded side by side)."""
    import math
    import subprocess
    import tempfile
    import re as _re
    from datetime import timezone as _tz
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    conn = _init_db(args.db)

    # Load discarded photos matching the reason filter
    reason_filter = args.reason  # e.g. "duplicate" or "junk" or None (all)
    if reason_filter:
        rows = conn.execute(
            "SELECT id, path, discard_reason FROM photos WHERE discarded=1 AND discard_reason LIKE ?",
            (f"%{reason_filter}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, path, discard_reason FROM photos WHERE discarded=1 AND discard_reason IS NOT NULL"
        ).fetchall()

    VIDEO_EXTS = {".mp4", ".mov", ".MP4", ".MOV"}
    rows = [r for r in rows if Path(r[1]).suffix not in VIDEO_EXTS]

    # Auto-confirm categories — no visual review needed
    def _is_auto(reason: str) -> bool:
        r = (reason or "").lower()
        return (r.startswith("exact duplicate") or
                r.startswith("burst shot") or
                r.startswith("user-") or
                r == "google-trash")

    auto_rows = [r for r in rows if _is_auto(r[2] or "")]
    rows = [r for r in rows if not _is_auto(r[2] or "")]

    if auto_rows:
        print(f"ℹ {len(auto_rows)} auto-confirmed (exact duplicates, burst shots, user-discards, trash)")

    if not rows:
        print("No photos remaining to review.")
        return

    # For remaining reasons, extract kept filename and look up kept path
    kept_name_re = _re.compile(r"visually identical to (.+)", _re.IGNORECASE)

    # Build organized-name index for all photos
    from datetime import datetime as _dt
    import json as _json
    clusters_path = Path(args.db).parent / "clusters.json"
    cluster_names: dict[int, str] = {}   # cluster_id → name
    photo_cluster_name: dict[int, str] = {}  # photo_id → name (via photo_ids list)
    if clusters_path.exists():
        for c in _json.loads(clusters_path.read_text()):
            cluster_names[c["id"]] = c.get("name", "")
            if c.get("confirmed"):
                for pid in c.get("photo_ids", []):
                    photo_cluster_name[pid] = c["name"]

    max_seq_row = conn.execute("SELECT MAX(organized_seq) FROM photos WHERE organized_seq IS NOT NULL").fetchone()
    max_seq = max_seq_row[0] or 0
    n_hex = max(5, len(format(max_seq, "x")))

    all_rows = conn.execute(
        "SELECT id, path, organized_seq, taken_at, cluster_id, discarded FROM photos"
    ).fetchall()

    def _org_name(photo_id, path, seq, taken_at, cluster_id, orig_ext):
        date_str = "00000000"
        if isinstance(taken_at, (int, float)):
            try:
                date_str = _dt.fromtimestamp(taken_at, tz=_tz.utc).strftime("%Y%m%d")
            except Exception:
                pass
        # prefer photo_ids lookup (survives cluster merges) over cluster_id column
        raw_name = photo_cluster_name.get(photo_id) or cluster_names.get(cluster_id, "unclustered")
        cname = _sanitize(raw_name)
        if seq is not None:
            return f"{date_str}-{format(seq, f'0{n_hex}x')}-{cname}{orig_ext}"
        else:
            return f"{date_str}-{cname}{orig_ext}"

    path_index = {}
    org_name_index = {}
    for row in all_rows:
        rid, rpath, rseq, rta, rcid, rdiscarded = row
        name = Path(rpath).name
        if not rdiscarded:
            path_index[name] = rpath
        org_name_index[rpath] = _org_name(rid, rpath, rseq, rta, rcid, Path(rpath).suffix)

    # Group discards by kept photo: {kept_path: [disc_path, ...]}
    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for pid, disc_path, reason in rows:
        kept_path = None
        if reason:
            m = kept_name_re.match(reason)
            if m:
                kept_name = m.group(1)
                kept_path = path_index.get(kept_name)
        # Skip orphaned pairs where kept photo was also discarded
        if kept_path is None and kept_name_re.match(reason or ""):
            continue
        key = kept_path or f"__unknown_{disc_path}"
        if key not in groups:
            groups[key] = {"kept": kept_path, "discards": []}
        groups[key]["discards"].append(disc_path)

    # For discards showing as "unclustered", inherit trip name from their kept photo
    for disc_path, kept_path in [(d, g["kept"]) for g in groups.values() for d in g["discards"] if g["kept"]]:
        disc_name = org_name_index.get(disc_path, "")
        if "unclustered" in disc_name and kept_path:
            kept_name = org_name_index.get(kept_path, "")
            # Extract trip suffix from kept name (everything after the seq)
            parts = kept_name.split("-", 2)
            if len(parts) == 3:
                disc_parts = disc_name.split("-", 2)
                if len(disc_parts) >= 2:
                    org_name_index[disc_path] = f"{disc_parts[0]}-{disc_parts[1]}-{parts[2]}" if len(disc_parts) > 2 else f"{disc_parts[0]}-{parts[2]}"

    group_list = list(groups.values())

    # Paginate by group
    total_groups = len(group_list)
    limit = args.limit
    page = args.page
    offset = (page - 1) * limit
    group_list = group_list[offset:offset + limit]
    pages = math.ceil(total_groups / limit)
    n_discards = sum(len(g["discards"]) for g in group_list)
    print(f"Page {page}/{pages} — showing {len(group_list)} groups ({n_discards} discards) of {total_groups} groups (--page N to navigate)")

    # Detect screen size
    screen_w, screen_h = 1920, 1080
    try:
        xr = subprocess.run(["xrandr", "--current"], capture_output=True, text=True)
        for line in xr.stdout.splitlines():
            import re as _re2
            mm = _re2.search(r"(\d{3,5})x(\d{3,5})\s+\d+\.\d+\*", line)
            if mm:
                screen_w, screen_h = int(mm.group(1)), int(mm.group(2))
                break
    except Exception:
        pass

    # Layout: each group = 1 row; row = [KEPT | DISC1 | DISC2 | ...]
    max_cols = max(2, screen_w // 400)  # fewer, wider cells
    rows_count = len(group_list)

    CAPTION_H = 48
    cell_w = screen_w // max_cols
    cell_h = max(220, min(400, (screen_h - CAPTION_H) // max(1, rows_count)))

    try:
        font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 10)
        font_bold = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", 10)
    except Exception:
        font = font_bold = ImageFont.load_default()

    sheet_w = cell_w * max_cols
    sheet_h = (cell_h + CAPTION_H) * rows_count
    sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)

    KEPT_BORDER = (80, 200, 80)    # green
    DISC_BORDER = (200, 80, 80)    # red
    BORDER = 3

    def place_cell(img_path, cx, cy, border_color, label, sublabel="", is_kept=False):
        x, y = cx * cell_w, cy * (cell_h + CAPTION_H)
        try:
            p = Path(img_path) if Path(img_path).is_absolute() else Path("/scratch/video_read") / img_path.lstrip("/")
            Image.MAX_IMAGE_PIXELS = None
            img = Image.open(p)
            img = ImageOps.contain(img, (cell_w - BORDER * 2, cell_h - BORDER * 2), Image.LANCZOS)
            ox = (cell_w - BORDER * 2 - img.width) // 2
            oy = (cell_h - BORDER * 2 - img.height) // 2
            sheet.paste(img, (x + BORDER + ox, y + BORDER + oy))
        except Exception:
            draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(50, 50, 50))
        draw.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], outline=border_color, width=BORDER)
        cy2 = y + cell_h
        draw.rectangle([x, cy2, x + cell_w, cy2 + CAPTION_H], fill=(15, 15, 15))
        draw.text((x + 3, cy2 + 2), label, fill=border_color, font=font_bold)
        display_name = org_name_index.get(img_path, Path(img_path).name)
        draw.text((x + 3, cy2 + 14), display_name, fill=(200, 200, 200), font=font)
        if sublabel:
            draw.text((x + 3, cy2 + 26), sublabel[:cell_w // 7], fill=(150, 150, 150), font=font)

    for row_idx, group in enumerate(group_list):
        kept_path = group["kept"]
        discards = group["discards"]
        max_discs = max_cols - 1  # leave 1 slot for kept

        # kept cell
        if kept_path:
            place_cell(kept_path, 0, row_idx, KEPT_BORDER, "KEPT", is_kept=True)
        else:
            x, y = 0, row_idx * (cell_h + CAPTION_H)
            draw.rectangle([x, y, x + cell_w, y + cell_h + CAPTION_H], fill=(30, 30, 30))
            draw.text((x + 4, y + cell_h // 2), "kept not found", fill=(120, 120, 120), font=font)

        # discard cells
        for d_idx, disc_path in enumerate(discards[:max_discs]):
            place_cell(disc_path, d_idx + 1, row_idx, DISC_BORDER, "DEL")

        # overflow indicator
        if len(discards) > max_discs:
            extra = len(discards) - max_discs
            x = (max_cols - 1 + max_discs) * cell_w  # won't fit, just annotate last cell
            y = row_idx * (cell_h + CAPTION_H)
            draw.text((max_cols * cell_w - 60, y + cell_h // 2), f"+{extra} more", fill=(200, 200, 100), font=font_bold)

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", prefix="discards_", delete=False)
    sheet.save(tmp.name, quality=88)
    tmp.close()
    n_groups = len(group_list)
    print(f"Contact sheet → {tmp.name}  ({n_groups} groups, {n_discards} discards, filter={reason_filter or 'all'})")
    subprocess.run(["pkill", "imv"], capture_output=True)
    subprocess.Popen(["imv", tmp.name])


def cmd_export_takeout(args):
    import shutil
    from datetime import datetime, timezone as _tz

    conn = _init_db(args.db)
    clusters = json.loads(Path(args.clusters).read_text())
    organized = Path(args.organized_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Build lookup: photo_id → (taken_at, lat, lon, caption)
    rows = conn.execute(
        "SELECT id, taken_at, lat, lon, caption FROM photos WHERE discarded=0"
    ).fetchall()
    meta = {r[0]: r[1:] for r in rows}

    # Build photo_id → new filename from organized dir
    # Scan organized dir for all files and map back via DB path
    all_organized = {f.name: f for f in organized.rglob("*") if f.is_file()}

    n_copied = n_sidecar = 0
    for c in clusters:
        if not c.get("confirmed"):
            continue
        album = _sanitize(c["name"])
        album_dir = out / album
        album_dir.mkdir(exist_ok=True)

        for pid in c.get("photo_ids", []):
            m = meta.get(pid)
            if not m:
                continue
            taken_at, lat, lon, caption = m

            # Find the organized file for this photo
            row = conn.execute("SELECT path, organized_seq FROM photos WHERE id=?", (pid,)).fetchone()
            if not row:
                continue
            orig_path, seq = row
            orig_ext = Path(orig_path).suffix

            # Build expected organized filename
            try:
                date_str = datetime.fromtimestamp(taken_at, tz=_tz.utc).strftime("%Y%m%d") if taken_at else "00000000"
            except Exception:
                date_str = "00000000"
            max_seq = conn.execute("SELECT MAX(organized_seq) FROM photos WHERE organized_seq IS NOT NULL").fetchone()[0] or 0
            n_hex = max(5, len(format(max_seq, "x")))
            seq_str = format(seq, f"0{n_hex}x") if seq is not None else "?????"
            fname = f"{date_str}-{seq_str}-{album}{orig_ext}"

            src = all_organized.get(fname)
            if not src:
                continue

            dest = album_dir / fname
            if not dest.exists():
                shutil.copy2(src, dest)
                n_copied += 1

            # Write sidecar JSON
            sidecar = album_dir / (fname + ".json")
            if not sidecar.exists():
                ts = str(int(taken_at)) if taken_at else "0"
                try:
                    fmt = datetime.fromtimestamp(int(ts), tz=_tz.utc).strftime("%b %-d, %Y, %-I:%M:%S %p UTC")
                except Exception:
                    fmt = ""
                geo = {"latitude": lat or 0.0, "longitude": lon or 0.0,
                       "altitude": 0.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0}
                data = {
                    "title": fname,
                    "description": caption or "",
                    "imageViews": "0",
                    "creationTime": {"timestamp": ts, "formatted": fmt},
                    "photoTakenTime": {"timestamp": ts, "formatted": fmt},
                    "geoData": geo,
                    "geoDataExif": geo,
                    "albumData": [{"title": c["name"]}],
                }
                sidecar.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                n_sidecar += 1

    conn.close()
    confirmed = sum(1 for c in clusters if c.get("confirmed"))
    print(f"✓ {n_copied} photos + {n_sidecar} sidecars exported to {out}/ ({confirmed} albums)")
    print(f"  Ready to zip and re-upload to Google Photos")


def cmd_export_discarded(args):
    import csv
    conn = _init_db(args.db)
    try:
        rows = conn.execute(
            "SELECT path, discard_reason FROM photos WHERE discarded=1"
        ).fetchall()
        if not rows:
            print("No discarded photos.")
            return

        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)

        copied = missing = 0
        for path, _ in rows:
            src = Path(path)
            if not src.exists():
                missing += 1
                continue
            dest = out / src.name
            if dest.exists():
                counter = 1
                while True:
                    candidate = out / f"{src.stem}_{counter}{src.suffix}"
                    if not candidate.exists():
                        dest = candidate
                        break
                    counter += 1
            shutil.copy2(src, dest)
            copied += 1

        manifest = out / "manifest.csv"
        with open(manifest, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["filename", "reason"])
            for path, reason in rows:
                w.writerow([Path(path).name, reason or "unknown"])

        print(f"✓ Copied {copied} discarded photo(s) to {out}/ ({missing} not found)")
        print(f"✓ Manifest written to {manifest}")
    finally:
        conn.close()


def cmd_enhance(args):
    from photos.enhance import enhance_photo, make_comparison
    from PIL import Image as _Image

    cluster_names: dict[int, str] = {}
    clusters_path = Path(args.clusters)
    if clusters_path.exists():
        for c in json.loads(clusters_path.read_text()):
            cluster_names[c["id"]] = c["name"]

    conn = _init_db(args.db)
    try:
        rows = conn.execute(
            "SELECT id, path, quality, cluster_id FROM photos "
            "WHERE discarded=0 AND described_at IS NOT NULL AND flagged=0"
        ).fetchall()

        if not rows:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        out_base = Path(args.output_dir)
        n_enhanced = n_skipped = 0
        bar = tqdm(rows, unit="photo")
        for _photo_id, photo_path, quality, cluster_id in bar:
            p = Path(photo_path)
            bar.set_description(p.name[:40])
            if not p.exists() or p.suffix.lower() in _VIDEO_EXTS:
                n_skipped += 1
                continue

            raw = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
            cname = _sanitize(raw) if cluster_id else "unclustered"
            dest_dir = out_base / cname
            dest_dir.mkdir(parents=True, exist_ok=True)

            enhanced_path = dest_dir / f"{p.stem}_enhanced.jpg"
            compare_path = dest_dir / f"{p.stem}_compare.jpg"

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
            except Exception as e:
                print(f"\n  Warning: could not enhance {p.name}: {e}")
                n_skipped += 1

        print(f"\n✓ Enhanced {n_enhanced} photo(s), {n_skipped} skipped")
        print(f"  Saved to {out_base}/")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(
        prog="photos.py",
        description="Google Photos Takeout organizer",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    s = sub.add_parser("scan", help="Scan Takeout export → photos.db")
    s.add_argument("--takeout-dir", required=True, metavar="DIR")
    s.add_argument("--db", default="photos.db", metavar="DB")

    c = sub.add_parser("cluster", help="Cluster photos into trips → clusters.json")
    c.add_argument("--db", default="photos.db", metavar="DB")
    c.add_argument("--clusters", default="clusters.json", metavar="FILE")
    c.add_argument("--gap-days", type=int, default=3, metavar="N")
    c.add_argument("--radius-km", type=float, default=50.0, metavar="KM")
    c.add_argument("--force", action="store_true", help="Overwrite existing confirmed clusters")

    r = sub.add_parser("review", help="Interactively review trip clusters")
    r.add_argument("--clusters", default="clusters.json", metavar="FILE")
    r.add_argument("--yes", action="store_true", help="Auto-confirm all pending trips")

    o = sub.add_parser("organize", help="Copy photos into organised folders")
    o.add_argument("--output-dir", default="output/final", metavar="DIR")
    o.add_argument("--db", default="photos.db", metavar="DB")
    o.add_argument("--clusters", default="clusters.json", metavar="FILE")

    d = sub.add_parser("dedup", help="Auto-discard byte-identical and visually identical duplicates")
    d.add_argument("--db", default="photos.db", metavar="DB")

    desc = sub.add_parser("describe", help="Describe photos with Qwen2.5-VL or Claude → store in DB")
    desc.add_argument("--db", default="photos.db", metavar="DB")
    desc.add_argument("--force", action="store_true", help="Re-describe already-described photos")
    desc.add_argument("--provider", choices=["qwen", "claude"], default="qwen",
                      help="Vision model provider (default: qwen)")
    desc.add_argument("--model", default="haiku",
                      choices=["haiku", "sonnet", "opus"],
                      help="Claude model to use (only with --provider claude, default: haiku)")
    desc.add_argument("--workers", type=int, default=5, metavar="N",
                      help="Concurrent Claude workers (only with --provider claude, default: 5)")
    desc.add_argument("--benchmark", action="store_true",
                      help="Compare providers on 20 sample photos, no DB writes")

    sd = sub.add_parser("show-discards", help="Contact sheet of discarded photos paired with their kept version")
    sd.add_argument("--db", default="output/photos.db", metavar="DB")
    sd.add_argument("--reason", default=None, metavar="REASON",
                    help="Filter by discard_reason substring (default: all)")
    sd.add_argument("--limit", type=int, default=12, metavar="N",
                    help="Groups per sheet (default: 12)")
    sd.add_argument("--page", type=int, default=1, metavar="N",
                    help="Page number (default: 1)")

    aj = sub.add_parser("auto-junk", help="Auto-discard receipts, black frames, accidentals, screenshots")
    aj.add_argument("--db", default="output/photos.db", metavar="DB")
    aj.add_argument("--to-delete-dir", default="output/to-delete", metavar="DIR")

    rec = sub.add_parser("recommend", help="Auto-flag bad quality photos and write review report")
    rec.add_argument("--db", default="photos.db", metavar="DB")
    rec.add_argument("--clusters", default="clusters.json", metavar="FILE")
    rec.add_argument("--output", default="output/recommendations.md", metavar="FILE")

    fl = sub.add_parser("flag", help="Flag photos for review and copy to to-review directory")
    fl.add_argument("ids", nargs="+", metavar="ID", type=int)
    fl.add_argument("--db", default="photos.db", metavar="DB")
    fl.add_argument("--clusters", default="clusters.json", metavar="FILE")
    fl.add_argument("--output-dir", default="output/to-review", metavar="DIR")
    fl.add_argument("--unflag", action="store_true", help="Unflag photos and remove copies")

    sr = sub.add_parser("search", help="Full-text search photos by description")
    sr.add_argument("query", metavar="QUERY")
    sr.add_argument("--db", default="photos.db", metavar="DB")
    sr.add_argument("--limit", type=int, default=20, metavar="N")
    sr.add_argument("--output", default="output/search-results.md", metavar="FILE")

    en = sub.add_parser("enhance", help="Apply color correction to all described photos")
    en.add_argument("--db", default="photos.db", metavar="DB")
    en.add_argument("--clusters", default="clusters.json", metavar="FILE")
    en.add_argument("--output-dir", default="output/enhanced", metavar="DIR")
    en.add_argument("--force", action="store_true", help="Re-enhance already-enhanced photos")

    et = sub.add_parser("export-takeout", help="Export organized photos as Google Takeout-style structure for re-upload")
    et.add_argument("--db", default="output/photos.db", metavar="DB")
    et.add_argument("--clusters", default="output/clusters.json", metavar="FILE")
    et.add_argument("--organized-dir", default="output/final", metavar="DIR")
    et.add_argument("--output-dir", default="output/final", metavar="DIR")

    ex = sub.add_parser("export-discarded", help="Copy discarded photos to a folder with manifest.csv")
    ex.add_argument("--db", default="photos.db", metavar="DB")
    ex.add_argument("--output-dir", default="output/to-delete", metavar="DIR")

    fd = sub.add_parser("fix-dates", help="Re-extract dates/GPS for scan-date artifact photos")
    fd.add_argument("--db", default="photos.db", metavar="DB")

    sh = sub.add_parser("show", help="Show contact sheet for one or more clusters in imv")
    sh.add_argument("cluster_ids", type=int, nargs="+", metavar="CLUSTER_ID")
    sh.add_argument("--db", default="output/photos.db", metavar="DB")
    sh.add_argument("--clusters", default="output/clusters.json", metavar="CLUSTERS")

    args = p.parse_args()
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag,
     "search": cmd_search, "enhance": cmd_enhance,
     "export-takeout": cmd_export_takeout,
     "export-discarded": cmd_export_discarded,
     "fix-dates": cmd_fix_dates,
     "auto-junk": cmd_auto_junk,
     "show-discards": cmd_show_discards,
     "show": cmd_show}[args.subcommand](args)


if __name__ == "__main__":
    main()
