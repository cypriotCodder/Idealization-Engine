"""Hacker News ingest via the Algolia search API.

Run with `python -m ingest.hackernews --run-label run1 --limit 50`.

Three passes, in this order so comments can be linked to their story:
1. search stories for each query and store the ones with a text body (Ask HN posts)
2. search comments for each query
3. pull the comment thread of every story stored in pass 1
"""

import argparse
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from db.connection import Connection, connect
from ingest.cache import DEFAULT_CACHE_DIR, get_json
from ingest.common import RunStats, load_salt, setup_logging, store_items
from ingest.hashing import author_hash
from ingest.html_text import html_to_text
from ingest.store import RawItem, find_item_id, finish_run, start_run, upsert_source

logger = logging.getLogger(__name__)

PLATFORM = "hackernews"
SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
# one source row for the whole platform: we search HN as a whole, not per community
TARGET = "search"
PAGE_SIZE = 100
# Algolia allows 10,000 requests/hour per IP; a short pause keeps us far below that
REQUEST_PAUSE_SECONDS = 0.2


@dataclass(frozen=True)
class Settings:
    active: bool
    run_label: str
    api: str
    queries: tuple[str, ...]
    include_comments: bool
    min_points: int
    max_pages_per_query: int
    max_comments_per_story: int
    min_text_length: int
    lookback_days: int


def load_settings(path: Path) -> Settings:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = config["hackernews"]
    defaults = config["defaults"]
    return Settings(
        active=bool(section["active"]),
        run_label=str(config["run_label"]),
        api=str(section["api"]).rstrip("/"),
        queries=tuple(section["queries"]),
        include_comments=bool(section["include_comments"]),
        min_points=int(section["min_points"]),
        max_pages_per_query=int(section["max_pages_per_query"]),
        max_comments_per_story=int(section["max_comments_per_story"]),
        min_text_length=int(defaults["min_text_length"]),
        lookback_days=int(defaults["lookback_days"]),
    )


def story_params(query: str, min_points: int, since: datetime) -> dict[str, str | int]:
    return {
        "query": query,
        "tags": "story",
        "numericFilters": f"points>={min_points},created_at_i>{int(since.timestamp())}",
        "hitsPerPage": PAGE_SIZE,
    }


def comment_params(query: str, since: datetime) -> dict[str, str | int]:
    return {
        "query": query,
        "tags": "comment",
        "numericFilters": f"created_at_i>{int(since.timestamp())}",
        "hitsPerPage": PAGE_SIZE,
    }


def thread_params(story_id: str, max_comments: int) -> dict[str, str | int]:
    # a comma between tags means AND: comments that belong to this story
    return {"tags": f"comment,story_{story_id}", "hitsPerPage": max_comments}


def _item_url(item_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


def _author(hit: dict[str, Any], salt: str) -> str | None:
    author = hit.get("author")
    return author_hash(PLATFORM, str(author), salt) if author else None


def story_to_item(hit: dict[str, Any], salt: str) -> RawItem:
    story_id = str(hit["objectID"])
    return RawItem(
        platform=PLATFORM,
        external_id=story_id,
        # link posts have no story_text; they come out empty and the length check drops them
        text=html_to_text(hit.get("story_text") or ""),
        created_at=datetime.fromtimestamp(hit["created_at_i"], UTC),
        url=_item_url(story_id),
        author_hash=_author(hit, salt),
        title=hit.get("title"),
        score=hit.get("points"),
        num_comments=hit.get("num_comments"),
    )


def comment_to_item(hit: dict[str, Any], salt: str, story_is_stored: bool) -> RawItem:
    comment_id = str(hit["objectID"])
    return RawItem(
        platform=PLATFORM,
        external_id=comment_id,
        # a comment found by search often belongs to a story we never stored; it still
        # stands on its own as a complaint, so keep it with no parent instead of dropping it
        parent_external_id=str(hit["story_id"]) if story_is_stored else None,
        text=html_to_text(hit.get("comment_text") or ""),
        created_at=datetime.fromtimestamp(hit["created_at_i"], UTC),
        url=_item_url(comment_id),
        author_hash=_author(hit, salt),
        # the thread title is the only context the filter gets for a lone comment
        title=hit.get("story_title"),
    )


class HackerNewsClient:
    def __init__(
        self,
        http: httpx.Client,
        api: str,
        cache_dir: Path,
        refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._url = f"{api}/search"
        self._cache_dir = cache_dir
        self._refresh = refresh
        self._sleep = sleep

    def pages(self, params: dict[str, str | int], max_pages: int) -> Iterator[list[dict[str, Any]]]:
        # Algolia pages start at 0
        for page in range(max_pages):
            payload, cached = get_json(
                self._http, self._url, {**params, "page": page}, self._cache_dir, self._refresh
            )
            if not cached:
                self._sleep(REQUEST_PAUSE_SECONDS)
            yield list(payload.get("hits", []))
            if page + 1 >= int(payload.get("nbPages", 0)):
                return


def run(
    conn: Connection,
    client: HackerNewsClient,
    settings: Settings,
    run_label: str,
    salt: str,
    now: datetime,
    limit: int | None = None,
    target: str = TARGET,
) -> RunStats:
    source_id = upsert_source(conn, PLATFORM, target, run_label)
    run_id = start_run(conn, source_id)
    conn.commit()

    stats = RunStats()
    errors: list[dict[str, Any]] = []
    # snap to midnight so the request, and so its cache key, is the same all day
    since = (now - timedelta(days=settings.lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    kept_total = 0
    story_ids: dict[str, None] = {}

    def full() -> bool:
        return limit is not None and kept_total >= limit

    def trim(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return hits if limit is None else hits[: max(0, limit - kept_total)]

    def comments(hits: list[dict[str, Any]]) -> list[RawItem]:
        return [
            comment_to_item(
                hit, salt, find_item_id(conn, PLATFORM, str(hit["story_id"])) is not None
            )
            for hit in hits
            if "story_id" in hit
        ]

    def record(stage: str, detail: str, error: Exception) -> None:
        logger.warning("stage=%s %s error=%r", stage, detail, error)
        errors.append({"stage": stage, "detail": detail, "error": repr(error)})

    for query in settings.queries:
        if full():
            break
        try:
            params = story_params(query, settings.min_points, since)
            for hits in client.pages(params, settings.max_pages_per_query):
                items = [story_to_item(hit, salt) for hit in trim(hits)]
                kept = store_items(conn, source_id, items, settings.min_text_length, stats)
                kept_total += len(kept)
                story_ids.update((item.external_id, None) for item in kept)
                if full():
                    break
        except httpx.HTTPError as error:
            # one bad query shouldn't sink the run; note it and move to the next
            record("stories", f"query={query!r}", error)

    if settings.include_comments:
        for query in settings.queries:
            if full():
                break
            try:
                params = comment_params(query, since)
                for hits in client.pages(params, settings.max_pages_per_query):
                    kept = store_items(
                        conn, source_id, comments(trim(hits)), settings.min_text_length, stats
                    )
                    kept_total += len(kept)
                    if full():
                        break
            except httpx.HTTPError as error:
                record("comments", f"query={query!r}", error)

        for story_id in story_ids:
            if full():
                break
            try:
                params = thread_params(story_id, settings.max_comments_per_story)
                for hits in client.pages(params, 1):
                    kept = store_items(
                        conn, source_id, comments(trim(hits)), settings.min_text_length, stats
                    )
                    kept_total += len(kept)
            except httpx.HTTPError as error:
                record("thread", f"story_id={story_id}", error)

    finish_run(conn, run_id, stats.items_new, errors)
    conn.commit()
    logger.info(
        "platform=%s run_label=%s seen=%d items_new=%d skipped_short=%d errors=%d",
        PLATFORM,
        run_label,
        stats.seen,
        stats.items_new,
        stats.skipped_short,
        len(errors),
    )
    return stats


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", help="defaults to run_label in config/sources.yaml")
    parser.add_argument("--limit", type=int, help="max items to keep, for sample runs")
    parser.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings(SOURCES_PATH)
    if not settings.active:
        logger.info("hackernews is not active in config/sources.yaml; nothing to do")
        return
    salt = load_salt()

    with httpx.Client(timeout=30) as http, connect() as conn:
        client = HackerNewsClient(http, settings.api, DEFAULT_CACHE_DIR, refresh=args.refresh)
        run(
            conn,
            client,
            settings,
            args.run_label or settings.run_label,
            salt,
            datetime.now(UTC),
            args.limit,
        )


if __name__ == "__main__":
    main()
