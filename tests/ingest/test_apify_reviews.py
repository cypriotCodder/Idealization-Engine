import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from db.connection import Connection
from ingest.apify_reviews import (
    SOURCES_PATH,
    ApifyClient,
    ApifyRunFailed,
    BudgetExceeded,
    Settings,
    actor_input,
    check_budget,
    load_settings,
    review_to_item,
    run_key,
    run_product,
)
from tests.ingest.dbutil import delete_source

PRODUCT = "Test Product"
PLATFORMS = ("test-g2", "test-capterra")
TEXT = "We export every invoice by hand because the sync with our ledger breaks each month."


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "active": True,
        "run_label": "run1",
        "api_token_env": "APIFY_TOKEN",
        "actor": "someone/review-scraper",
        "platforms": PLATFORMS,
        "star_ratings": (1, 2, 3),
        "sort": "most_recent",
        "max_results_per_product": 100,
        "max_charge_usd_per_product": 0.45,
        "max_total_charge_usd": 4.50,
        "products": (PRODUCT,),
        "min_text_length": 80,
    }
    return Settings(**{**values, **overrides})


def _review(platform: str, review_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "platform": platform,
        "reviewId": review_id,
        "title": "Sync keeps breaking",
        "text": f"{TEXT} (case {review_id})",
        "cons": "No bulk export.",
        "rating": 2.5,
        "date": "2026-03-04",
        "reviewUrl": f"https://reviews.test/{review_id}",
        "reviewer": {"name": "Real Name", "company": "Acme", "companySize": "11-50"},
        "productName": PRODUCT,
        **overrides,
    }


class FakeApify:
    """Stands in for the Apify API and counts how many paid runs were started."""

    def __init__(self, items: list[dict[str, Any]], status: str = "SUCCEEDED") -> None:
        self.items = items
        self.status = status
        self.starts: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            self.starts.append(request)
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        if path.endswith("/actor-runs/run-1"):
            run = {"status": self.status, "defaultDatasetId": "ds-1", "usageTotalUsd": 0.44}
            return httpx.Response(200, json={"data": run})
        assert path.endswith("/datasets/ds-1/items")
        return httpx.Response(200, json=self.items)


def _client(tmp_path: Path, api: FakeApify) -> ApifyClient:
    http = httpx.Client(transport=httpx.MockTransport(api))
    return ApifyClient(http, "secret-token", tmp_path, sleep=lambda _: None)


def test_repo_config_loads_and_fits_its_own_budget() -> None:
    settings = load_settings(SOURCES_PATH)
    planned = len(settings.products) * settings.max_charge_usd_per_product
    assert planned <= settings.max_total_charge_usd + 1e-9


def test_actor_input_sends_stars_as_strings() -> None:
    assert actor_input(_settings(), "Pipedrive") == {
        "query": "Pipedrive",
        "platforms": list(PLATFORMS),
        "maxResults": 100,
        "sort": "most_recent",
        "starRatings": ["1", "2", "3"],
    }


def test_run_key_depends_on_the_input() -> None:
    assert run_key("a/b", {"query": "x"}) == run_key("a/b", {"query": "x"})
    assert run_key("a/b", {"query": "x"}) != run_key("a/b", {"query": "y"})


def test_review_maps_to_a_raw_item_without_the_name() -> None:
    item = review_to_item(_review("g2", "r1"), "salt")
    assert item is not None
    assert (item.platform, item.external_id, item.rating) == ("g2", "r1", 2)
    assert item.text == f"{TEXT} (case r1) Dislikes: No bulk export."
    assert item.title == f"{PRODUCT}: Sync keeps breaking"
    assert item.created_at == datetime(2026, 3, 4, tzinfo=UTC)
    assert item.author_hash is not None
    assert "Real Name" not in repr(item)


def test_review_without_an_id_is_skipped() -> None:
    assert review_to_item(_review("g2", "r1", reviewId=None), "salt") is None


def test_start_sends_the_cost_cap_and_keeps_the_token_out_of_the_url(tmp_path: Path) -> None:
    api = FakeApify([_review("g2", "r1")])
    result = _client(tmp_path, api).reviews("someone/review-scraper", {"query": "x"}, 0.45)
    start = api.starts[0]
    assert start.url.path.endswith("/acts/someone~review-scraper/runs")
    assert start.url.params["maxTotalChargeUsd"] == "0.45"
    assert "secret-token" not in str(start.url)
    assert start.headers["Authorization"] == "Bearer secret-token"
    assert (result.status, result.charged_usd, len(result.items)) == ("SUCCEEDED", 0.44, 1)


def test_second_call_never_starts_a_paid_run(tmp_path: Path) -> None:
    api = FakeApify([_review("g2", "r1")])
    client = _client(tmp_path, api)
    client.reviews("a/b", {"query": "x"}, 0.45)
    again = client.reviews("a/b", {"query": "x"}, 0.45)
    assert len(api.starts) == 1
    assert (again.status, again.charged_usd) == ("CACHED", None)


def test_interrupted_run_is_resumed_not_restarted(tmp_path: Path) -> None:
    api = FakeApify([_review("g2", "r1")])
    key = run_key("a/b", {"query": "x"})
    # what's left on disk if we crash right after starting the run
    (tmp_path / f"{key}.run.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    result = _client(tmp_path, api).reviews("a/b", {"query": "x"}, 0.45)
    assert api.starts == []
    assert len(result.items) == 1


def test_capped_run_still_yields_its_reviews(tmp_path: Path) -> None:
    api = FakeApify([_review("g2", "r1")], status="ABORTED")
    assert len(_client(tmp_path, api).reviews("a/b", {"query": "x"}, 0.45).items) == 1


def test_empty_run_fails_and_is_not_silently_retried(tmp_path: Path) -> None:
    api = FakeApify([], status="FAILED")
    client = _client(tmp_path, api)
    with pytest.raises(ApifyRunFailed):
        client.reviews("a/b", {"query": "x"}, 0.45)
    with pytest.raises(ApifyRunFailed):
        client.reviews("a/b", {"query": "x"}, 0.45)
    assert len(api.starts) == 1


def test_budget_counts_only_runs_not_yet_paid_for(tmp_path: Path) -> None:
    api = FakeApify([_review("g2", "r1")])
    client = _client(tmp_path, api)
    settings = _settings(products=("A", "B", "C"), max_total_charge_usd=0.90)
    with pytest.raises(BudgetExceeded):
        check_budget(client, settings, ["A", "B", "C"])
    client.reviews(settings.actor, actor_input(settings, "A"), 0.45)
    assert check_budget(client, settings, ["A", "B", "C"]) == pytest.approx(0.90)


def _cleanup(conn: Connection) -> None:
    for platform in PLATFORMS:
        delete_source(conn, platform, PRODUCT)


def test_run_product_stores_each_site_once(conn: Connection, tmp_path: Path) -> None:
    api = FakeApify(
        [
            _review("test-g2", "r1"),
            _review("test-g2", "r2", text="Too short.", cons=""),
            _review("test-capterra", "r3"),
            _review("some-other-site", "r4"),
        ]
    )
    client = _client(tmp_path, api)
    try:
        first = run_product(conn, client, _settings(), PRODUCT, "test-run", "salt")
        assert {p: (s.items_new, s.skipped_short) for p, s in first.items()} == {
            "test-g2": (1, 1),
            "test-capterra": (1, 0),
        }
        second = run_product(conn, client, _settings(), PRODUCT, "test-run", "salt")
        assert [s.items_new for s in second.values()] == [0, 0]
        assert len(api.starts) == 1
        rows = conn.execute(
            """
            SELECT s.platform, count(*) FROM scrape_runs sr JOIN sources s ON s.id = sr.source_id
            WHERE s.target = %s AND s.platform = ANY(%s) GROUP BY 1 ORDER BY 1
            """,
            (PRODUCT, list(PLATFORMS)),
        ).fetchall()
        assert rows == [("test-capterra", 2), ("test-g2", 2)]
    finally:
        _cleanup(conn)


def test_run_product_logs_a_failed_run(conn: Connection, tmp_path: Path) -> None:
    client = _client(tmp_path, FakeApify([], status="FAILED"))
    try:
        stats = run_product(conn, client, _settings(), PRODUCT, "test-run", "salt")
        assert [s.items_new for s in stats.values()] == [0, 0]
        row = conn.execute(
            """
            SELECT sr.errors -> 0 ->> 'stage' FROM scrape_runs sr
            JOIN sources s ON s.id = sr.source_id WHERE s.target = %s AND s.platform = %s
            """,
            (PRODUCT, PLATFORMS[0]),
        ).fetchone()
        assert row == ("apify",)
    finally:
        _cleanup(conn)
