from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from db.connection import Connection
from ingest.atlassian import (
    PLATFORM,
    SOURCES_PATH,
    App,
    AtlassianClient,
    Settings,
    load_settings,
    parse_date,
    review_to_item,
    run_category,
    wanted,
)
from tests.ingest.dbutil import delete_source

NOW = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
CATEGORY = "Test category"
APP = App("com.test.app", "Test Timesheets")
TEXT = "We have to re-enter every worklog by hand because the export drops custom fields."


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "active": True,
        "run_label": "run1",
        "api": "https://mp.test/rest/2",
        "categories": (CATEGORY,),
        "apps_per_category": 30,
        "max_rating": 3,
        "max_reviews_per_app": 200,
        "min_text_length": 80,
        "lookback_days": 365,
    }
    return Settings(**{**values, **overrides})


def _review(
    review_id: str, stars: int, date: str = "2026-09-01T10:00:00.123456789Z"
) -> dict[str, Any]:
    return {
        "id": review_id,
        "review": f"{TEXT} (case {review_id})",
        "stars": stars,
        "date": date,
        "helpfulVotes": 4,
        "_links": {"alternate": {"href": f"/apps/1/test?reviewId={review_id}"}},
        "_embedded": {
            "author": {
                "name": "Real Name",
                "_links": {"alternate": {"href": f"/users/{review_id}"}},
            }
        },
    }


def _client(tmp_path: Path, handler: Any) -> AtlassianClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return AtlassianClient(http, "https://mp.test/rest/2", tmp_path, sleep=lambda _: None)


def test_repo_config_loads() -> None:
    settings = load_settings(SOURCES_PATH)
    assert settings.categories
    assert 1 <= settings.max_rating <= 5


def test_parse_date_handles_nanoseconds() -> None:
    assert parse_date("2026-09-23T13:58:51.938979938Z") == datetime(
        2026, 9, 23, 13, 58, 51, 938979, tzinfo=UTC
    )
    assert parse_date("2026-09-23T13:58:51Z") == datetime(2026, 9, 23, 13, 58, 51, tzinfo=UTC)


def test_wanted_keeps_recent_low_star_reviews_only() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert wanted(_review("a", 3), 3, since) is True
    assert wanted(_review("a", 4), 3, since) is False
    assert wanted(_review("a", 1, "2025-06-01T00:00:00Z"), 3, since) is False


def test_review_maps_to_a_raw_item_without_the_name() -> None:
    item = review_to_item(_review("r1", 2), APP, "salt")
    assert (item.platform, item.external_id) == (PLATFORM, "com.test.app:r1")
    assert item.title == "Test Timesheets"
    assert (item.rating, item.score) == (2, 4)
    assert item.url == "https://marketplace.atlassian.com/apps/1/test?reviewId=r1"
    assert item.author_hash is not None
    assert "Real Name" not in repr(item)


def test_recent_reviews_stops_once_reviews_are_too_old(tmp_path: Path) -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params["offset"])
        # a full page whose last review is older than the cut-off
        page = [_review(f"r{i}", 1) for i in range(49)] + [
            _review("old", 1, "2020-01-01T00:00:00Z")
        ]
        return httpx.Response(200, json={"_embedded": {"reviews": page}})

    reviews = _client(tmp_path, handler).recent_reviews(
        "com.test.app", 200, datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert offsets == ["0"]
    assert len(reviews) == 50


def test_popular_apps_is_capped(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        addons = [{"key": f"k{offset + i}", "name": f"App {offset + i}"} for i in range(50)]
        return httpx.Response(200, json={"_embedded": {"addons": addons}})

    apps = _client(tmp_path, handler).popular_apps(CATEGORY, 60)
    assert len(apps) == 60
    assert apps[-1] == App("k59", "App 59")


def _api(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/addons"):
        addons = [
            {"key": "com.test.app", "name": "Test Timesheets"},
            {"key": "broken", "name": "B"},
        ]
        return httpx.Response(200, json={"_embedded": {"addons": addons}})
    if "/addons/broken/" in request.url.path:
        return httpx.Response(500, json={})
    reviews = [
        _review("test-r1", 1),
        _review("test-r2", 3),
        _review("test-r3", 5),
        {**_review("test-r4", 1), "review": "Bad."},
    ]
    return httpx.Response(200, json={"_embedded": {"reviews": reviews}})


def test_run_category_stores_low_star_reviews_once(conn: Connection, tmp_path: Path) -> None:
    client = _client(tmp_path, _api)
    try:
        first = run_category(conn, client, _settings(), CATEGORY, "test-run", "salt", NOW)
        assert (first.items_new, first.skipped_short) == (2, 1)
        rows = conn.execute(
            """
            SELECT r.external_id, r.rating, r.title FROM raw_items r
            JOIN sources s ON s.id = r.source_id
            WHERE s.platform = %s AND s.target = %s ORDER BY r.external_id
            """,
            (PLATFORM, CATEGORY),
        ).fetchall()
        assert rows == [
            ("com.test.app:test-r1", 1, "Test Timesheets"),
            ("com.test.app:test-r2", 3, "Test Timesheets"),
        ]

        second = run_category(conn, client, _settings(), CATEGORY, "test-run", "salt", NOW)
        assert second.items_new == 0
        row = conn.execute(
            """
            SELECT sr.errors -> 0 ->> 'stage', sr.errors -> 0 ->> 'detail' FROM scrape_runs sr
            JOIN sources s ON s.id = sr.source_id
            WHERE s.platform = %s AND s.target = %s ORDER BY sr.id LIMIT 1
            """,
            (PLATFORM, CATEGORY),
        ).fetchone()
        assert row == ("reviews", "app=broken")
    finally:
        delete_source(conn, PLATFORM, CATEGORY)


def test_run_category_respects_the_limit(conn: Connection, tmp_path: Path) -> None:
    try:
        stats = run_category(
            conn, _client(tmp_path, _api), _settings(), CATEGORY, "test-run", "salt", NOW, limit=1
        )
        assert stats.items_new == 1
    finally:
        delete_source(conn, PLATFORM, CATEGORY)
