from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def auto_flag_quality(conn: sqlite3.Connection) -> int:
    """Flag non-good-quality non-discarded photos. Returns count newly flagged."""
    cur = conn.execute(
        "UPDATE photos SET flagged=1 "
        "WHERE quality != 'good' AND discarded=0 AND flagged=0"
    )
    conn.commit()
    return cur.rowcount
