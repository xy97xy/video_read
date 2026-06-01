from __future__ import annotations
import sqlite3


def build_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 full-text search index from described, non-discarded photos."""
    conn.execute("DROP TABLE IF EXISTS photos_fts")
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
