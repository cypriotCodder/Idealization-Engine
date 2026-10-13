"""Bits every scraper's run loop needs: logging setup, the author salt, and the store step."""

import logging
import os
from dataclasses import dataclass

from db.connection import Connection
from ingest.store import RawItem, insert_item


@dataclass
class RunStats:
    seen: int = 0
    items_new: int = 0
    skipped_short: int = 0


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request at INFO, which buries our own one-line-per-run summaries
    logging.getLogger("httpx").setLevel(logging.WARNING)


def load_salt() -> str:
    salt = os.environ.get("AUTHOR_HASH_SALT", "")
    # a placeholder salt would be baked into every author_hash, and changing it later
    # breaks unique-author counts and repost dedup for everything already stored
    if not salt or salt.startswith("change_me"):
        raise RuntimeError("set AUTHOR_HASH_SALT in .env to a long random string before ingesting")
    return salt


def store_items(
    conn: Connection, source_id: int, items: list[RawItem], min_length: int, stats: RunStats
) -> list[RawItem]:
    """Insert the items that are long enough; return those (new or already stored)."""
    kept = [item for item in items if len(item.text) >= min_length]
    stats.seen += len(items)
    stats.skipped_short += len(items) - len(kept)
    stats.items_new += sum(insert_item(conn, source_id, item) for item in kept)
    # commit per page so an interrupted run keeps what it already fetched
    conn.commit()
    return kept
