from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from db.connection import Connection
from ingest.hackernews import (
    PLATFORM,
    SOURCES_PATH,
    HackerNewsClient,
    Settings,
    comment_params,
    comment_to_item,
    load_settings,
    run,
    story_params,
    story_to_item,
    thread_params,
)
from tests.ingest.dbutil import delete_source

NOW = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
TARGET = "test-search"
TEXT = "Every month I export invoices by hand and retype them into our accounting spreadsheet."
# far above any real HN id, so test rows can never collide with scraped ones
STORY, LINK_STORY, OTHER_STORY = 9_000_000_001, 9_000_000_002, 9_000_000_003


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "active": True,
        "run_label": "run1",
        "api": "https://hn.test/api/v1",
        "queries": ("spreadsheet",),
        "include_comments": True,
        "min_points": 5,
        "max_pages_per_query": 3,
        "max_comments_per_story": 50,
        "min_text_length": 80,
        "lookback_days": 365,
    }
    return Settings(**{**values, **overrides})


def _story(story_id: int, text: str | None) -> dict[str, Any]:
    return {
        "objectID": str(story_id),
        "story_id": story_id,
        "title": f"Ask HN: story {story_id}",
        "story_text": text,
        "author": "real_username",
        "points": 42,
        "num_comments": 7,
        "created_at_i": 1_760_000_000,
    }


def _comment(comment_id: int, story_id: int, note: str) -> dict[str, Any]:
    return {
        "objectID": str(comment_id),
        "story_id": story_id,
        "story_title": f"Ask HN: story {story_id}",
        "comment_text": f"<p>{note}: {TEXT}</p>",
        "author": f"user_{comment_id}",
        "created_at_i": 1_760_000_500,
    }


def _client(tmp_path: Path, handler: Any, sleeps: list[float] | None = None) -> HackerNewsClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    recorded = sleeps if sleeps is not None else []
    return HackerNewsClient(http, "https://hn.test/api/v1", tmp_path, sleep=recorded.append)


def test_repo_config_loads() -> None:
    settings = load_settings(SOURCES_PATH)
    assert settings.queries
    assert settings.max_comments_per_story > 0


def test_story_params_filter_on_points_and_date() -> None:
    params = story_params("spreadsheet", 5, NOW)
    assert params["tags"] == "story"
    assert params["numericFilters"] == f"points>=5,created_at_i>{int(NOW.timestamp())}"


def test_comment_params_have_no_points_filter() -> None:
    assert "points" not in str(comment_params("spreadsheet", NOW)["numericFilters"])


def test_thread_params_select_one_storys_comments() -> None:
    assert thread_params("123", 50) == {"tags": "comment,story_123", "hitsPerPage": 50}


def test_story_maps_to_a_raw_item_without_the_username() -> None:
    item = story_to_item(_story(STORY, f"<p>{TEXT}</p>"), "salt")
    assert (item.platform, item.external_id, item.parent_external_id) == (
        PLATFORM,
        str(STORY),
        None,
    )
    assert item.text == TEXT
    assert item.url == f"https://news.ycombinator.com/item?id={STORY}"
    assert (item.score, item.num_comments) == (42, 7)
    assert item.author_hash is not None
    assert "real_username" not in repr(item)


def test_link_story_has_empty_text() -> None:
    assert story_to_item(_story(LINK_STORY, None), "salt").text == ""


def test_comment_links_to_its_story_only_when_stored() -> None:
    hit = _comment(1, STORY, "a")
    assert comment_to_item(hit, "salt", story_is_stored=True).parent_external_id == str(STORY)
    lone = comment_to_item(hit, "salt", story_is_stored=False)
    assert lone.parent_external_id is None
    assert lone.title == f"Ask HN: story {STORY}"


def test_client_pages_from_zero_until_nb_pages(tmp_path: Path) -> None:
    seen_pages: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_pages.append(request.url.params["page"])
        return httpx.Response(200, json={"hits": [{"n": 1}], "nbPages": 2})

    pages = list(_client(tmp_path, handler, sleeps).pages({"query": "x"}, 5))
    assert seen_pages == ["0", "1"]
    assert len(pages) == 2
    assert len(sleeps) == 2


def test_client_stops_at_max_pages(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": [], "nbPages": 99})

    assert len(list(_client(tmp_path, handler).pages({"query": "x"}, 3))) == 3


def _api(request: httpx.Request) -> httpx.Response:
    tags = request.url.params["tags"]
    if tags == "story":
        hits = [_story(STORY, f"<p>{TEXT}</p>"), _story(LINK_STORY, None)]
    elif tags == "comment":
        # one comment on the stored story, one on a story we never stored
        hits = [
            _comment(9_000_000_011, STORY, "found by search"),
            _comment(9_000_000_012, OTHER_STORY, "lone"),
        ]
    else:
        assert tags == f"comment,story_{STORY}"
        hits = [
            _comment(9_000_000_011, STORY, "found by search"),
            _comment(9_000_000_013, STORY, "thread only"),
            {**_comment(9_000_000_014, STORY, "x"), "comment_text": "<p>too short</p>"},
        ]
    return httpx.Response(200, json={"hits": hits, "nbPages": 1})


def test_run_stores_stories_and_comments_once(conn: Connection, tmp_path: Path) -> None:
    client = _client(tmp_path, _api)
    try:
        first = run(conn, client, _settings(), "test-run", "salt", NOW, target=TARGET)
        assert (first.items_new, first.skipped_short) == (4, 2)
        rows = conn.execute(
            """
            SELECT r.external_id, parent.external_id
            FROM raw_items r
            JOIN sources s ON s.id = r.source_id
            LEFT JOIN raw_items parent ON parent.id = r.parent_id
            WHERE s.platform = %s AND s.target = %s ORDER BY r.external_id
            """,
            (PLATFORM, TARGET),
        ).fetchall()
        assert rows == [
            ("9000000001", None),
            ("9000000011", "9000000001"),
            ("9000000012", None),
            ("9000000013", "9000000001"),
        ]

        second = run(conn, client, _settings(), "test-run", "salt", NOW, target=TARGET)
        assert second.items_new == 0
    finally:
        delete_source(conn, PLATFORM, TARGET)


def test_run_records_a_failed_query_and_carries_on(conn: Connection, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("query") == "broken":
            return httpx.Response(500, json={})
        return _api(request)

    settings = _settings(queries=("broken", "spreadsheet"), include_comments=False)
    try:
        stats = run(
            conn, _client(tmp_path, handler), settings, "test-run", "salt", NOW, target=TARGET
        )
        assert stats.items_new == 1
        row = conn.execute(
            """
            SELECT jsonb_array_length(sr.errors), sr.errors -> 0 ->> 'stage' FROM scrape_runs sr
            JOIN sources s ON s.id = sr.source_id WHERE s.platform = %s AND s.target = %s
            """,
            (PLATFORM, TARGET),
        ).fetchone()
        assert row == (1, "stories")
    finally:
        delete_source(conn, PLATFORM, TARGET)


def test_run_respects_the_limit(conn: Connection, tmp_path: Path) -> None:
    try:
        stats = run(
            conn, _client(tmp_path, _api), _settings(), "test-run", "salt", NOW, 2, target=TARGET
        )
        assert stats.items_new == 2
    finally:
        delete_source(conn, PLATFORM, TARGET)
