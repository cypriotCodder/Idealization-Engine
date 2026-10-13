"""Database writes shared by every scraper. Nothing commits; the caller owns the transaction."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from db.connection import Connection
from ingest.hashing import text_hash


@dataclass(frozen=True)
class RawItem:
    platform: str
    external_id: str
    text: str
    created_at: datetime
    # set for answers and comments; the parent has to be stored first
    parent_external_id: str | None = None
    url: str | None = None
    author_hash: str | None = None
    title: str | None = None
    rating: int | None = None
    score: int | None = None
    num_comments: int | None = None
    view_count: int | None = None


def upsert_source(conn: Connection, platform: str, target: str, run_label: str) -> int:
    row = conn.execute(
        """
        INSERT INTO sources (platform, target, run_label)
        VALUES (%s, %s, %s)
        ON CONFLICT (platform, target, run_label) DO UPDATE SET active = true
        RETURNING id
        """,
        (platform, target, run_label),
    ).fetchone()
    assert row is not None
    return int(row[0])


def start_run(conn: Connection, source_id: int) -> int:
    row = conn.execute(
        "INSERT INTO scrape_runs (source_id) VALUES (%s) RETURNING id", (source_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def finish_run(conn: Connection, run_id: int, items_new: int, errors: list[dict[str, Any]]) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = now(), items_new = %s, errors = %s WHERE id = %s",
        (items_new, Jsonb(errors), run_id),
    )


def find_item_id(conn: Connection, platform: str, external_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM raw_items WHERE platform = %s AND external_id = %s",
        (platform, external_id),
    ).fetchone()
    return None if row is None else int(row[0])


def insert_item(conn: Connection, source_id: int, item: RawItem) -> bool:
    """Insert one item. Returns False if it was already stored, a repost, or orphaned."""
    parent_id: int | None = None
    if item.parent_external_id is not None:
        parent_id = find_item_id(conn, item.platform, item.parent_external_id)
        if parent_id is None:
            # the parent was filtered out or deduplicated, so the reply has no context to read
            return False
    # no conflict target: either the re-scrape guard or the repost guard may skip the row
    row = conn.execute(
        """
        INSERT INTO raw_items (
            source_id, platform, external_id, parent_id, url, author_hash, title, text,
            rating, score, num_comments, view_count, created_at, text_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            source_id,
            item.platform,
            item.external_id,
            parent_id,
            item.url,
            item.author_hash,
            item.title,
            item.text,
            item.rating,
            item.score,
            item.num_comments,
            item.view_count,
            item.created_at,
            text_hash(item.text),
        ),
    ).fetchone()
    return row is not None
