"""Stack Exchange ingest. Run with `python -m ingest.stackexchange --run-label run1 --limit 50`.

For each configured site, searches every keyword (per tag, where tags are set), stores the
questions, then stores the accepted and top-voted answers as rows pointing at their question.
"""

import argparse
import html
import logging
import os
import time
from collections import defaultdict
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
from ingest.store import RawItem, finish_run, start_run, upsert_source

logger = logging.getLogger(__name__)

PLATFORM = "stackexchange"
API_URL = "https://api.stackexchange.com/2.3"
SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
PAGE_SIZE = 100
# the API takes at most 100 ids per call
ID_BATCH = 100
# stop a little early so a run never burns the last of the day's quota
MIN_QUOTA = 5


@dataclass(frozen=True)
class Site:
    name: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    active: bool
    run_label: str
    api_key_env: str
    sites: tuple[Site, ...]
    keywords: tuple[str, ...]
    include_answers: bool
    top_answers_per_question: int
    min_score: int
    max_pages_per_query: int
    min_text_length: int
    lookback_days: int


def load_settings(path: Path) -> Settings:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = config["stackexchange"]
    defaults = config["defaults"]
    return Settings(
        active=bool(section["active"]),
        run_label=str(config["run_label"]),
        api_key_env=str(section["api_key_env"]),
        sites=tuple(Site(str(s["site"]), tuple(s.get("tags", ()))) for s in section["sites"]),
        keywords=tuple(section["keywords"]),
        include_answers=bool(section["include_answers"]),
        top_answers_per_question=int(section["top_answers_per_question"]),
        min_score=int(section["min_score"]),
        max_pages_per_query=int(section["max_pages_per_query"]),
        min_text_length=int(defaults["min_text_length"]),
        lookback_days=int(defaults["lookback_days"]),
    )


def queries(site: Site, keywords: tuple[str, ...]) -> list[tuple[str, str | None]]:
    """Every (keyword, tag) pair to search. One tag per query, so tags act as OR, not AND."""
    tags: tuple[str | None, ...] = site.tags or (None,)
    return [(keyword, tag) for tag in tags for keyword in keywords]


def search_params(
    site: str, keyword: str, tag: str | None, page: int, fromdate: datetime, min_score: int
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "site": site,
        "q": keyword,
        "page": page,
        "pagesize": PAGE_SIZE,
        "order": "desc",
        # `min` applies to the sort field, so sorting by votes is what makes min_score work
        "sort": "votes",
        "min": min_score,
        "fromdate": int(fromdate.timestamp()),
        "filter": "withbody",
    }
    if tag is not None:
        params["tagged"] = tag
    return params


def _external_id(site: str, post_id: int) -> str:
    # ids repeat across sites, and questions and answers share one id space within a site
    return f"{site}:{post_id}"


def _author(owner: dict[str, Any] | None, salt: str) -> str | None:
    # deleted accounts come back without a user_id
    if not owner or "user_id" not in owner:
        return None
    return author_hash(PLATFORM, str(owner["user_id"]), salt)


def question_to_item(question: dict[str, Any], site: str, salt: str) -> RawItem:
    return RawItem(
        platform=PLATFORM,
        external_id=_external_id(site, question["question_id"]),
        text=html_to_text(question.get("body", "")),
        created_at=datetime.fromtimestamp(question["creation_date"], UTC),
        url=question.get("link"),
        author_hash=_author(question.get("owner"), salt),
        title=html.unescape(question["title"]),
        score=question.get("score"),
        num_comments=question.get("answer_count"),
        view_count=question.get("view_count"),
    )


def answer_to_item(answer: dict[str, Any], site: str, salt: str) -> RawItem:
    return RawItem(
        platform=PLATFORM,
        external_id=_external_id(site, answer["answer_id"]),
        parent_external_id=_external_id(site, answer["question_id"]),
        text=html_to_text(answer.get("body", "")),
        created_at=datetime.fromtimestamp(answer["creation_date"], UTC),
        author_hash=_author(answer.get("owner"), salt),
        score=answer.get("score"),
    )


def pick_answers(answers: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Per question, keep the accepted answer plus the highest-voted ones, `top_n` in total."""
    by_question: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for answer in answers:
        by_question[answer["question_id"]].append(answer)
    picked: list[dict[str, Any]] = []
    for group in by_question.values():
        group.sort(key=lambda a: (bool(a.get("is_accepted")), a.get("score", 0)), reverse=True)
        picked.extend(group[:top_n])
    return picked


class QuotaExhausted(Exception):
    pass


class StackExchangeClient:
    def __init__(
        self,
        http: httpx.Client,
        cache_dir: Path,
        api_key: str | None,
        refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._api_key = api_key
        self._refresh = refresh
        self._sleep = sleep

    def get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        if self._api_key:
            params = {**params, "key": self._api_key}
        payload, cached = get_json(
            self._http, f"{API_URL}{path}", params, self._cache_dir, self._refresh
        )
        if cached:
            return payload
        # the API tells us when to slow down; ignoring `backoff` gets the IP banned
        if payload.get("backoff"):
            self._sleep(float(payload["backoff"]))
        if payload.get("quota_remaining", MIN_QUOTA) < MIN_QUOTA:
            raise QuotaExhausted(f"quota_remaining={payload['quota_remaining']}")
        return payload

    def pages(
        self, path: str, params: dict[str, str | int], max_pages: int
    ) -> Iterator[list[dict[str, Any]]]:
        for page in range(1, max_pages + 1):
            payload = self.get(path, {**params, "page": page})
            yield list(payload.get("items", []))
            if not payload.get("has_more"):
                return


def run_site(
    conn: Connection,
    client: StackExchangeClient,
    settings: Settings,
    site: Site,
    run_label: str,
    salt: str,
    now: datetime,
    limit: int | None = None,
) -> RunStats:
    source_id = upsert_source(conn, PLATFORM, site.name, run_label)
    run_id = start_run(conn, source_id)
    conn.commit()

    stats = RunStats()
    errors: list[dict[str, Any]] = []
    # snap to midnight so the request, and so its cache key, is the same all day
    fromdate = (now - timedelta(days=settings.lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    question_ids: dict[int, None] = {}

    def record(stage: str, detail: str, error: Exception) -> None:
        logger.warning("stage=%s site=%s %s error=%r", stage, site.name, detail, error)
        errors.append({"stage": stage, "detail": detail, "error": repr(error)})

    try:
        for keyword, tag in queries(site, settings.keywords):
            if limit is not None and len(question_ids) >= limit:
                break
            params = search_params(site.name, keyword, tag, 1, fromdate, settings.min_score)
            try:
                for page in client.pages("/search/advanced", params, settings.max_pages_per_query):
                    if limit is not None:
                        page = page[: max(0, limit - len(question_ids))]
                    items = [question_to_item(q, site.name, salt) for q in page]
                    for item in store_items(
                        conn, source_id, items, settings.min_text_length, stats
                    ):
                        question_ids[int(item.external_id.split(":")[1])] = None
                    if limit is not None and len(question_ids) >= limit:
                        break
            except httpx.HTTPError as error:
                # one bad query shouldn't sink the site; note it and move to the next
                record("search", f"keyword={keyword!r} tag={tag}", error)

        if settings.include_answers:
            ids = list(question_ids)
            for start in range(0, len(ids), ID_BATCH):
                batch = ";".join(str(i) for i in ids[start : start + ID_BATCH])
                params = {
                    "site": site.name,
                    "pagesize": PAGE_SIZE,
                    "order": "desc",
                    "sort": "votes",
                    "filter": "withbody",
                }
                try:
                    answers = [
                        answer
                        for page in client.pages(
                            f"/questions/{batch}/answers", params, settings.max_pages_per_query
                        )
                        for answer in page
                    ]
                except httpx.HTTPError as error:
                    record("answers", f"batch_start={start}", error)
                    continue
                picked = pick_answers(answers, settings.top_answers_per_question)
                items = [answer_to_item(a, site.name, salt) for a in picked]
                store_items(conn, source_id, items, settings.min_text_length, stats)
    except QuotaExhausted as error:
        record("quota", "stopping this site early", error)

    finish_run(conn, run_id, stats.items_new, errors)
    conn.commit()
    logger.info(
        "site=%s run_label=%s questions_seen=%d items_new=%d skipped_short=%d errors=%d",
        site.name,
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
    parser.add_argument("--limit", type=int, help="max questions per site, for sample runs")
    parser.add_argument("--site", help="only run this site")
    parser.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings(SOURCES_PATH)
    if not settings.active:
        logger.info("stackexchange is not active in config/sources.yaml; nothing to do")
        return
    sites = [s for s in settings.sites if args.site in (None, s.name)]
    salt = load_salt()
    api_key = os.environ.get(settings.api_key_env) or None
    if api_key is None:
        logger.warning("no %s set; running on the 300 requests/day quota", settings.api_key_env)

    with httpx.Client(timeout=30) as http, connect() as conn:
        client = StackExchangeClient(http, DEFAULT_CACHE_DIR, api_key, refresh=args.refresh)
        for site in sites:
            run_site(
                conn,
                client,
                settings,
                site,
                args.run_label or settings.run_label,
                salt,
                datetime.now(UTC),
                args.limit,
            )


if __name__ == "__main__":
    main()
