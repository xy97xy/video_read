from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, UTC
from pathlib import Path


_JUNK_CLASSIFY_PROMPT = (
    "Look at this photo carefully. Is it a JUNK photo?\n\n"
    "Junk means any of:\n"
    "- Receipt, invoice, bill, or paper document (not a meaningful photo)\n"
    "- Screenshot of a phone or computer screen\n"
    "- Photo that is completely black, completely white, or blank\n"
    "- Accidental shot: finger/hand covering lens, blurry mess, lens cap on, random floor/ceiling\n\n"
    "Answer with EXACTLY one of:\n"
    "YES - <one-line reason>\n"
    "NO\n"
)


_CAPTION_SUSPECT_WORDS = [
    "receipt", "invoice", "screenshot", "handwritten", "text on paper",
    "screen capture", "screen recording", "completely black", "all black",
    "blank image", "out of focus", "blurry photo", "lens cap",
    "price tag", "bill total", "payment", "menu",
    "document", "form field", "phone screen", "computer screen",
]


def _caption_suspects(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return (id, path) for photos whose captions contain junk indicators."""
    import re
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in _CAPTION_SUSPECT_WORDS) + r")\b",
        re.IGNORECASE,
    )
    rows = conn.execute(
        "SELECT id, path, caption FROM photos WHERE discarded=0 AND caption IS NOT NULL"
    ).fetchall()
    return [(pid, path) for pid, path, cap in rows if cap and pattern.search(cap)]


def auto_discard_junk(conn: sqlite3.Connection, to_delete_dir: Path) -> int:
    """Detect and discard junk photos using Qwen vision classification.

    Pre-screens by caption to find suspects, then runs Qwen2.5-VL on each
    suspect image for definitive yes/no classification.
    Returns count of newly discarded photos.
    """
    import shutil
    import sys
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # suppress decompression bomb warning
    from photos.describe import load_qwen, _call_qwen

    to_delete_dir.mkdir(parents=True, exist_ok=True)

    suspects = _caption_suspects(conn)
    if not suspects:
        print("  No caption suspects found — collection looks clean.")
        return 0

    print(f"Found {len(suspects)} caption suspects. Loading Qwen2.5-VL for vision check...")
    model, processor = load_qwen()

    n_discarded = 0
    for i, (pid, path) in enumerate(suspects):
        src = Path(path)
        if not src.exists():
            src = Path("/scratch/video_read") / path.lstrip("/")
        if not src.exists():
            continue

        if src.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}:
            continue

        try:
            import tempfile
            tmp_name = None
            img = Image.open(src)
            if src.suffix.lower() == ".heic":
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.close()
                img.save(tmp.name, "JPEG")
                tmp_name = tmp.name
                img = Image.open(tmp_name)
            img.thumbnail((630, 630), Image.LANCZOS)
            img = img.convert("RGB")

            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "image": img, "max_pixels": 360 * 420},
                    {"type": "text", "text": _JUNK_CLASSIFY_PROMPT},
                ],
            }]
            response = _call_qwen(model, processor, messages)
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)
        except Exception as e:
            print(f"  [{i+1}/{len(suspects)}] skip {src.name}: {e}", file=sys.stderr)
            continue

        is_junk = response.strip().upper().startswith("YES")
        reason_part = response.strip()[3:].strip(" -:") if is_junk else ""
        print(f"  [{i+1}/{len(suspects)}] {'JUNK' if is_junk else 'ok  '} {src.name}"
              f"{(' — ' + reason_part[:60]) if is_junk else ''}")

        if is_junk:
            dest = to_delete_dir / src.name
            if dest.exists():
                dest = to_delete_dir / (src.stem + f"_{pid}" + src.suffix)
            shutil.copy2(src, dest)
            conn.execute(
                "UPDATE photos SET discarded=1, discard_reason='auto-junk' WHERE id=?",
                (pid,),
            )
            n_discarded += 1

    conn.commit()
    return n_discarded


def auto_flag_quality(conn: sqlite3.Connection) -> int:
    """Flag non-good-quality non-discarded photos. Returns count newly flagged."""
    cur = conn.execute(
        "UPDATE photos SET flagged=1 "
        "WHERE quality != 'good' AND discarded=0 AND flagged=0"
    )
    conn.commit()
    return cur.rowcount


def build_report(
    conn: sqlite3.Connection,
    clusters_path: Path | None,
    output_path: Path,
) -> Path:
    """Write cluster-by-cluster markdown report. Returns output_path."""
    rows = conn.execute(
        "SELECT id, path, quality, scene, caption, people, cluster_id, flagged "
        "FROM photos WHERE discarded=0 ORDER BY cluster_id NULLS LAST, id"
    ).fetchall()

    cluster_names: dict[int, str] = {}
    if clusters_path and clusters_path.exists():
        for c in json.loads(clusters_path.read_text()):
            cluster_names[c["id"]] = c["name"]

    total = len(rows)
    n_flagged = sum(1 for r in rows if r[7])

    groups: dict[int | None, list] = defaultdict(list)
    for r in rows:
        groups[r[6]].append(r)

    lines = [
        "# Photo Recommendations",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d')}  |  {total} photos  |  {n_flagged} flagged",
        "",
    ]

    for cid in sorted(k for k in groups if k is not None):
        photos = groups[cid]
        name = cluster_names.get(cid, f"cluster-{cid}")
        _append_table(lines, f"{name} ({len(photos)} photos)", photos)

    if None in groups:
        _append_table(lines, f"Unclustered ({len(groups[None])} photos)", groups[None])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    return output_path


def _append_table(lines: list[str], heading: str, photos: list) -> None:
    lines += [
        f"## {heading}",
        "",
        "| id | file | quality | scene | people | caption |",
        "|----|------|---------|-------|--------|---------|",
    ]
    for pid, path, quality, scene, caption, people, _, flagged in photos:
        marker = " 🚩" if flagged else ""
        caption_short = (caption or "")[:80]
        lines.append(
            f"| {pid}{marker} | {Path(path).name} | {quality or ''} "
            f"| {scene or ''} | {people or ''} | {caption_short} |"
        )
    lines.append("")


def set_flagged(
    conn: sqlite3.Connection,
    ids: list[int],
    flag: bool,
) -> dict:
    """Set flagged=1 or flagged=0 for given IDs.

    Returns {"done": [...], "skipped": [(id, reason),...], "not_found": [...]}.
    When flagging (flag=True), photos with discarded=1 are skipped.
    """
    done: list[int] = []
    skipped: list[tuple[int, str]] = []
    not_found: list[int] = []

    for pid in ids:
        row = conn.execute(
            "SELECT discarded FROM photos WHERE id=?", (pid,)
        ).fetchone()
        if row is None:
            not_found.append(pid)
            continue
        if flag and row[0]:
            skipped.append((pid, "already discarded"))
            continue
        conn.execute("UPDATE photos SET flagged=? WHERE id=?", (int(flag), pid))
        done.append(pid)

    conn.commit()
    return {"done": done, "skipped": skipped, "not_found": not_found}
