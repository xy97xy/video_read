#!/usr/bin/env python3
import argparse
import json
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
from photos.dedup import find_exact_duplicates, find_burst_groups


def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    migrations = [
        ("discarded",    "ALTER TABLE photos ADD COLUMN discarded    INTEGER DEFAULT 0"),
        ("caption",      "ALTER TABLE photos ADD COLUMN caption      TEXT"),
        ("quality",      "ALTER TABLE photos ADD COLUMN quality      TEXT"),
        ("scene",        "ALTER TABLE photos ADD COLUMN scene        TEXT"),
        ("people",       "ALTER TABLE photos ADD COLUMN people       TEXT"),
        ("described_at", "ALTER TABLE photos ADD COLUMN described_at INTEGER"),
        ("flagged",      "ALTER TABLE photos ADD COLUMN flagged      INTEGER DEFAULT 0"),
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


def cmd_cluster(args):
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT id, path, taken_at, lat, lon, place FROM photos"
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
    clusters = json.loads(Path(args.clusters).read_text())
    conn = _init_db(args.db)
    id_to_path = {r[0]: r[1] for r in conn.execute(
        "SELECT id, path FROM photos WHERE discarded = 0"
    )}
    conn.close()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    n_copied = n_skipped = 0
    for c in clusters:
        if not c.get("confirmed"):
            continue
        folder_name = _sanitize(c["name"])
        folder = out / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        for pid in c["photo_ids"]:
            src = id_to_path.get(pid)
            if not src or not Path(src).exists():
                n_skipped += 1
                continue
            dest = _dest_path(folder, Path(src).name)
            shutil.copy2(src, dest)
            n_copied += 1

    confirmed = sum(1 for c in clusters if c.get("confirmed"))
    print(f"✓ {n_copied} photos copied into {confirmed} folder(s) under {args.output_dir}")
    if n_skipped:
        print(f"  {n_skipped} skipped (source file not found)")


def cmd_dedup(args):
    conn = _init_db(args.db)
    try:
        # Pass 1: exact duplicates
        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows]

        dup_groups = find_exact_duplicates(photos)
        n_auto = 0
        for group in dup_groups:
            keep_id = min(p["id"] for p in group)
            for p in group:
                if p["id"] != keep_id:
                    conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                    n_auto += 1
        conn.commit()
        print(f"✓ Auto-discarded {n_auto} exact duplicate(s)")

        # Pass 2: burst groups
        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows]
        burst_groups = find_burst_groups(photos, window_seconds=args.burst_window)

        def _file_size(p: dict) -> int:
            try:
                return Path(p["path"]).stat().st_size
            except OSError:
                return 0

        n_kept = n_discarded_burst = 0
        for group in burst_groups:
            recommended = max(group, key=_file_size)

            print(f"\nBurst group ({len(group)} photos):")
            for i, p in enumerate(group, 1):
                size_mb = _file_size(p) / 1_048_576
                dt_str = (
                    datetime.fromtimestamp(p["taken_at"]).strftime("%Y-%m-%d %H:%M:%S")
                    if p["taken_at"] else ""
                )
                arrow = "  ← recommended" if p["id"] == recommended["id"] else ""
                print(f"  {i}. {Path(p['path']).name}  {size_mb:.1f} MB  {dt_str}{arrow}")
            print("Actions: [k]eep recommended  [p]ick different  [s]kip  [?]help")

            while True:
                try:
                    action = input("> ").strip().lower()
                except EOFError:
                    print("\nAborted.")
                    return

                if action == "k":
                    for p in group:
                        if p["id"] != recommended["id"]:
                            conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                            n_discarded_burst += 1
                    conn.commit()
                    n_kept += 1
                    break
                elif action == "p":
                    while True:
                        try:
                            choice_str = input(f"  Keep which? (1-{len(group)}): ").strip()
                        except EOFError:
                            print("\nAborted.")
                            return
                        try:
                            idx = int(choice_str) - 1
                            if 0 <= idx < len(group):
                                chosen = group[idx]
                                for p in group:
                                    if p["id"] != chosen["id"]:
                                        conn.execute("UPDATE photos SET discarded=1 WHERE id=?", (p["id"],))
                                        n_discarded_burst += 1
                                conn.commit()
                                n_kept += 1
                                break
                            else:
                                print(f"  Invalid. Enter 1–{len(group)}.")
                        except ValueError:
                            print(f"  Invalid. Enter 1–{len(group)}.")
                    break
                elif action == "s":
                    break
                elif action == "?":
                    print("  k = keep recommended (largest file), discard others")
                    print("  p = pick a different photo to keep")
                    print("  s = skip this group (keep all)")
                    print("  ? = show this help")
                else:
                    print("  Unknown action. Type ? for help.")

        print(f"\n✓ Kept {n_kept}, discarded {n_discarded_burst} across {len(burst_groups)} burst group(s)")
    finally:
        conn.close()


def cmd_describe(args):
    from photos.describe import load_qwen, describe_photo

    conn = _init_db(args.db)
    try:
        if getattr(args, "force", False):
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path FROM photos WHERE discarded = 0 AND described_at IS NULL"
            ).fetchall()

        if not rows:
            print("✓ All photos already described. Use --force to re-describe.")
            return

        print(f"Loading Qwen2.5-VL ({len(rows)} photos to describe)...")
        t0 = time.time()
        model, processor = load_qwen()
        print(f"Model loaded in {time.time() - t0:.0f}s")

        _VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi'}
        n_described = 0
        bar = tqdm(rows, unit="photo")
        for photo_id, photo_path in bar:
            p = Path(photo_path)
            if not p.exists() or p.suffix.lower() in _VIDEO_EXTS:
                continue
            bar.set_description(p.name[:40])
            result = describe_photo(model, processor, p)
            conn.execute(
                "UPDATE photos SET caption=?, quality=?, scene=?, people=?, described_at=? WHERE id=?",
                (result["caption"], result["quality"], result["scene"], result["people"],
                 int(time.time()), photo_id),
            )
            conn.commit()
            n_described += 1

        print(f"\n✓ Described {n_described} photo(s)")
    finally:
        conn.close()


def cmd_recommend(args):
    from photos.recommend import auto_flag_quality, build_report

    conn = _init_db(args.db)
    try:
        n = auto_flag_quality(conn)
        print(f"✓ Auto-flagged {n} photo(s) with non-good quality")

        clusters_path = Path(args.clusters) if Path(args.clusters).exists() else None
        output_path = build_report(conn, clusters_path, Path(args.output))
        print(f"✓ Report written to {output_path}")
        print(f"  Review it, then run: python photos.py flag <id> [id ...]")
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
                cname = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
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
                cname = cluster_names.get(cluster_id, "unclustered") if cluster_id else "unclustered"
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

    r = sub.add_parser("review", help="Interactively review trip clusters")
    r.add_argument("--clusters", default="clusters.json", metavar="FILE")
    r.add_argument("--yes", action="store_true", help="Auto-confirm all pending trips")

    o = sub.add_parser("organize", help="Copy photos into organised folders")
    o.add_argument("--output-dir", required=True, metavar="DIR")
    o.add_argument("--db", default="photos.db", metavar="DB")
    o.add_argument("--clusters", default="clusters.json", metavar="FILE")

    d = sub.add_parser("dedup", help="Remove exact duplicates and thin burst shots")
    d.add_argument("--db", default="photos.db", metavar="DB")
    d.add_argument("--burst-window", type=int, default=3, metavar="SEC")

    desc = sub.add_parser("describe", help="Describe photos with Qwen2.5-VL → store in DB")
    desc.add_argument("--db", default="photos.db", metavar="DB")
    desc.add_argument("--force", action="store_true", help="Re-describe already-described photos")

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

    args = p.parse_args()
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag}[args.subcommand](args)


if __name__ == "__main__":
    main()
