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
        ("discard_reason", "ALTER TABLE photos ADD COLUMN discard_reason TEXT"),
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


_VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv'}


def cmd_dedup(args):
    conn = _init_db(args.db)
    try:
        if args.apply:
            picks = json.loads(Path(args.apply).read_text())
            n_discarded = 0
            for group_pick in picks:
                keep_name = group_pick["keep_name"]
                group_type = group_pick.get("type", "burst")
                reason = (f"exact duplicate of {keep_name}" if group_type == "exact_duplicate"
                          else f"burst shot (kept {keep_name})")
                for pid in group_pick["discard_ids"]:
                    conn.execute(
                        "UPDATE photos SET discarded=1, discard_reason=? WHERE id=?",
                        (reason, pid),
                    )
                    n_discarded += 1
            conn.commit()
            print(f"✓ Applied picks: {n_discarded} photo(s) discarded across {len(picks)} group(s)")
            return

        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows
                  if Path(r[1]).suffix.lower() not in _VIDEO_EXTS]

        def _photo_info(p: dict, group_type: str) -> dict:
            row = conn.execute(
                "SELECT caption, quality, scene FROM photos WHERE id=?", (p["id"],)
            ).fetchone()
            size_mb = Path(p["path"]).stat().st_size / 1_048_576 if Path(p["path"]).exists() else 0
            dt_str = datetime.fromtimestamp(p["taken_at"]).strftime("%Y-%m-%d %H:%M:%S") if p["taken_at"] else ""
            caption, quality, scene = row if row else (None, None, None)
            return {
                "id": p["id"],
                "filename": Path(p["path"]).name,
                "path": p["path"],
                "size_mb": round(size_mb, 1),
                "taken_at": dt_str,
                "caption": caption,
                "quality": quality,
                "scene": scene,
            }

        # Pass 1: exact duplicates — auto-discard, keep the described copy (or lower ID)
        dup_groups = find_exact_duplicates(photos)
        n_exact = 0
        for group in dup_groups:
            described = [p for p in group if conn.execute(
                "SELECT described_at FROM photos WHERE id=?", (p["id"],)
            ).fetchone()[0]]
            keep = described[0] if described else min(group, key=lambda p: p["id"])
            keep_name = Path(keep["path"]).name
            for p in group:
                if p["id"] != keep["id"]:
                    conn.execute(
                        "UPDATE photos SET discarded=1, discard_reason=? WHERE id=?",
                        (f"exact duplicate of {keep_name}", p["id"]),
                    )
                    n_exact += 1
        conn.commit()
        print(f"✓ Auto-discarded {n_exact} exact duplicate(s) (kept described copy)")

        # Pass 2: burst groups — report for Claude to review
        rows = conn.execute(
            "SELECT id, path, taken_at FROM photos WHERE discarded = 0"
        ).fetchall()
        photos = [{"id": r[0], "path": r[1], "taken_at": r[2]} for r in rows
                  if Path(r[1]).suffix.lower() not in _VIDEO_EXTS]
        burst_groups = find_burst_groups(photos, window_seconds=args.burst_window)

        if not burst_groups:
            print("✓ No burst groups found")
            return

        groups_out = []
        for group in burst_groups:
            photos_out = [_photo_info(p, "burst") for p in group]
            warning = len(group) >= 10
            groups_out.append({"type": "burst", "photos": photos_out,
                                "warning": "large group — likely bad timestamps, consider skipping" if warning else None})

        print(f"Found {len(burst_groups)} burst group(s). Claude should review and run --apply.")

        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(groups_out, indent=2, ensure_ascii=False))
        print(f"✓ Report written to {report_path}")
        print(f"\n  picks.json format: [{{'keep_id': 5, 'keep_name': 'IMG_1234.jpg', 'discard_ids': [6, 7]}}]")
        print(f"  Then run: python photos.py dedup --apply <picks.json>")
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

    _VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.avi'}

    conn = _init_db(args.db)
    try:
        rows = conn.execute(
            "SELECT id, path, quality FROM photos "
            "WHERE discarded=0 AND described_at IS NOT NULL AND flagged=0"
        ).fetchall()

        if not rows:
            print("⚠ No photos have been described yet. Run: python photos.py describe --db <db>")
            return

        n_enhanced = n_skipped = 0
        bar = tqdm(rows, unit="photo")
        for _photo_id, photo_path, quality in bar:
            p = Path(photo_path)
            bar.set_description(p.name[:40])
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
            except Exception as e:
                print(f"\n  Warning: could not enhance {p.name}: {e}")
                n_skipped += 1

        print(f"\n✓ Enhanced {n_enhanced} photo(s), {n_skipped} skipped")
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

    d = sub.add_parser("dedup", help="Find exact duplicates + report burst groups for Claude to review")
    d.add_argument("--db", default="photos.db", metavar="DB")
    d.add_argument("--burst-window", type=int, default=3, metavar="SEC")
    d.add_argument("--report", default="output/burst-groups.json", metavar="FILE",
                   help="Where to write burst group report (default: output/burst-groups.json)")
    d.add_argument("--apply", default=None, metavar="FILE",
                   help="Apply Claude's picks from a JSON file")

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

    sr = sub.add_parser("search", help="Full-text search photos by description")
    sr.add_argument("query", metavar="QUERY")
    sr.add_argument("--db", default="photos.db", metavar="DB")
    sr.add_argument("--limit", type=int, default=20, metavar="N")
    sr.add_argument("--output", default="output/search-results.md", metavar="FILE")

    en = sub.add_parser("enhance", help="Apply color correction to all described photos")
    en.add_argument("--db", default="photos.db", metavar="DB")
    en.add_argument("--force", action="store_true", help="Re-enhance already-enhanced photos")

    ex = sub.add_parser("export-discarded", help="Copy discarded photos to a folder with manifest.csv")
    ex.add_argument("--db", default="photos.db", metavar="DB")
    ex.add_argument("--output-dir", default="output/to-delete", metavar="DIR")

    args = p.parse_args()
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize,
     "dedup": cmd_dedup, "describe": cmd_describe,
     "recommend": cmd_recommend, "flag": cmd_flag,
     "search": cmd_search, "enhance": cmd_enhance,
     "export-discarded": cmd_export_discarded}[args.subcommand](args)


if __name__ == "__main__":
    main()
