from __future__ import annotations
import sqlite3


def build_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 full-text search index from described, non-discarded photos."""
    conn.execute("DROP TABLE IF EXISTS photos_fts")
    # Standalone (not content='photos'): full rebuild on every call means no triggers
    # needed and WHERE filtering during INSERT works without restriction.
    conn.execute("""
        CREATE VIRTUAL TABLE photos_fts USING fts5(
            caption, scene, people, place
        )
    """)
    conn.execute("""
        INSERT INTO photos_fts(rowid, caption, scene, people, place)
        SELECT id, caption, scene, people, place
        FROM photos
        WHERE discarded=0 AND described_at IS NOT NULL
    """)
    conn.commit()


def search_photos(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search photos using FTS5 BM25 ranking. Returns list sorted by relevance (best first).

    Each dict has: id, path, scene, place, caption, cluster_id, score.
    bm25() returns negative values — lower (more negative) = better match.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.path, p.scene, p.place, p.caption, p.cluster_id,
               bm25(photos_fts) AS score
        FROM photos_fts
        JOIN photos p ON photos_fts.rowid = p.id
        WHERE photos_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "path": r[1],
            "scene": r[2],
            "place": r[3],
            "caption": r[4],
            "cluster_id": r[5],
            "score": r[6],
        }
        for r in rows
    ]
