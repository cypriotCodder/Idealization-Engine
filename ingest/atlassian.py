"""Atlassian Marketplace ingest via its public REST API.

Run with `python -m ingest.atlassian --run-label run1 --limit 50`.

For each configured category, lists the most popular apps and stores their low-star reviews.
The plan pencilled this in for Apify, but Atlassian serves the same data from an official,
free API, so there is nothing to scrape or pay for.
"""

import argparse
import logging
import re
import time
from collections.abc import Callable
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
from ingest.store import RawItem, finish_run, start_run, upsert_source

logger = logging.getLogger(__name__)

PLATFORM = "atlassian_marketplace"
SITE_URL = "https://marketplace.atlassian.com"
SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
# the API rejects a bigger page than this
PAGE_SIZE = 50
REQUEST_PAUSE_SECONDS = 0.2
USER_AGENT = "ideation-pipeline/0.1 (private market research)"


@dataclass(frozen=True)
class Settings:
    active: bool
    run_label: str
    api: str
    categories: tuple[str, ...]
    apps_per_category: int
    max_rating: int
    max_reviews_per_app: int
    min_text_length: int
    lookback_days: int


@dataclass(frozen=True)
class App:
    key: str
    name: str


def load_settings(path: Path) -> Settings:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = config["atlassian_marketplace"]
    defaults = config["defaults"]
    return Settings(
        active=bool(section["active"]),
        run_label=str(config["run_label"]),
        api=str(section["api"]).rstrip("/"),
        categories=tuple(section["categories"]),
        apps_per_category=int(section["apps_per_category"]),
        max_rating=int(section["max_rating"]),
        max_reviews_per_app=int(section["max_reviews_per_app"]),
        min_text_length=int(defaults["min_text_length"]),
        lookback_days=int(section.get("lookback_days", defaults["lookback_days"])),
    )


def parse_date(value: str) -> datetime:
    # the API sends nanoseconds ("...51.938979938Z"); Python only reads up to microseconds
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def _author(review: dict[str, Any], salt: str) -> str | None:
    # the profile link is the only stable id for a reviewer; the display name can change
    href = review.get("_embedded", {}).get("author", {}).get("_links", {}).get("alternate", {})
    profile = href.get("href")
    return author_hash(PLATFORM, str(profile), salt) if profile else None


def review_to_item(review: dict[str, Any], app: App, salt: str) -> RawItem:
    link = review.get("_links", {}).get("alternate", {}).get("href")
    return RawItem(
        platform=PLATFORM,
        # a review's id is really its author's user id, so it repeats on every app that
        # person reviewed; the app key makes it unique
        external_id=f"{app.key}:{review['id']}",
        text=" ".join(str(review.get("review") or "").split()),
        created_at=parse_date(review["date"]),
        url=f"{SITE_URL}{link}" if link else None,
        author_hash=_author(review, salt),
        # a review rarely names the app it is about, so the title carries that context
        title=app.name,
        rating=review.get("stars"),
        score=review.get("helpfulVotes"),
    )


def wanted(review: dict[str, Any], max_rating: int, since: datetime) -> bool:
    return int(review.get("stars", 0)) <= max_rating and parse_date(review["date"]) >= since


class AtlassianClient:
    def __init__(
        self,
        http: httpx.Client,
        api: str,
        cache_dir: Path,
        refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._api = api
        self._cache_dir = cache_dir
        self._refresh = refresh
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        payload, cached = get_json(
            self._http, f"{self._api}{path}", params, self._cache_dir, self._refresh
        )
        if not cached:
            self._sleep(REQUEST_PAUSE_SECONDS)
        return payload

    def popular_apps(self, category: str, count: int) -> list[App]:
        apps: list[App] = []
        for offset in range(0, count, PAGE_SIZE):
            payload = self._get(
                "/addons",
                {"category": category, "filter": "popular", "limit": PAGE_SIZE, "offset": offset},
            )
            page = payload.get("_embedded", {}).get("addons", [])
            apps.extend(App(str(a["key"]), str(a["name"])) for a in page)
            if len(page) < PAGE_SIZE:
                break
        return apps[:count]

    def recent_reviews(
        self, app_key: str, max_reviews: int, since: datetime
    ) -> list[dict[str, Any]]:
        """Newest-first reviews, stopping at `max_reviews` or once they are older than `since`."""
        reviews: list[dict[str, Any]] = []
        for offset in range(0, max_reviews, PAGE_SIZE):
            payload = self._get(
                f"/addons/{app_key}/reviews", {"limit": PAGE_SIZE, "offset": offset}
            )
            page = payload.get("_embedded", {}).get("reviews", [])
            reviews.extend(page)
            # reviews come newest first, so one that is too old means the rest are too
            if len(page) < PAGE_SIZE or parse_date(page[-1]["date"]) < since:
                break
        return reviews[:max_reviews]


def run_category(
    conn: Connection,
    client: AtlassianClient,
    settings: Settings,
    category: str,
    run_label: str,
    salt: str,
    now: datetime,
    limit: int | None = None,
) -> RunStats:
    source_id = upsert_source(conn, PLATFORM, category, run_label)
    run_id = start_run(conn, source_id)
    conn.commit()

    stats = RunStats()
    errors: list[dict[str, Any]] = []
    # snap to midnight so the cut-off doesn't shift between runs on the same day
    since = (now - timedelta(days=settings.lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    kept_total = 0

    def record(stage: str, detail: str, error: Exception) -> None:
        logger.warning("stage=%s category=%r %s error=%r", stage, category, detail, error)
        errors.append({"stage": stage, "detail": detail, "error": repr(error)})

    try:
        apps = client.popular_apps(category, settings.apps_per_category)
    except httpx.HTTPError as error:
        record("apps", "could not list apps", error)
        apps = []

    for app in apps:
        if limit is not None and kept_total >= limit:
            break
        try:
            reviews = client.recent_reviews(app.key, settings.max_reviews_per_app, since)
        except httpx.HTTPError as error:
            # one app failing shouldn't sink the category; note it and move on
            record("reviews", f"app={app.key}", error)
            continue
        items = [
            review_to_item(review, app, salt)
            for review in reviews
            if wanted(review, settings.max_rating, since)
        ]
        if limit is not None:
            items = items[: max(0, limit - kept_total)]
        kept_total += len(store_items(conn, source_id, items, settings.min_text_length, stats))

    finish_run(conn, run_id, stats.items_new, errors)
    conn.commit()
    logger.info(
        "category=%r run_label=%s apps=%d seen=%d items_new=%d skipped_short=%d errors=%d",
        category,
        run_label,
        len(apps),
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
    parser.add_argument("--limit", type=int, help="max reviews kept per category, for samples")
    parser.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings(SOURCES_PATH)
    if not settings.active:
        logger.info("atlassian_marketplace is not active in config/sources.yaml; nothing to do")
        return
    salt = load_salt()

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=30, headers=headers) as http, connect() as conn:
        client = AtlassianClient(http, settings.api, DEFAULT_CACHE_DIR, refresh=args.refresh)
        for category in settings.categories:
            run_category(
                conn,
                client,
                settings,
                category,
                args.run_label or settings.run_label,
                salt,
                datetime.now(UTC),
                args.limit,
            )


if __name__ == "__main__":
    main()
