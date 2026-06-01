from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, UTC
from pathlib import Path


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
