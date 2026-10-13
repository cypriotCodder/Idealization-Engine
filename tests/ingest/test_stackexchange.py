from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from db.connection import Connection
from ingest.stackexchange import (
    PLATFORM,
    SOURCES_PATH,
    QuotaExhausted,
    Settings,
    Site,
    StackExchangeClient,
    answer_to_item,
    load_settings,
    pick_answers,
    queries,
    question_to_item,
    run_site,
    search_params,
)
from tests.ingest.dbutil import delete_source

NOW = datetime(2026, 10, 13, 15, 30, tzinfo=UTC)
BODY = "<p>Every week I manually copy rows from one workbook into another and it takes hours.</p>"
SITE = Site("test-site", ())


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "active": True,
        "run_label": "run1",
        "api_key_env": "STACKEXCHANGE_KEY",
        "sites": (SITE,),
        "keywords": ("manually",),
        "include_answers": True,
        "top_answers_per_question": 2,
        "min_score": 1,
        "max_pages_per_query": 3,
        "min_text_length": 80,
        "lookback_days": 365,
    }
    return Settings(**{**values, **overrides})


def _question(question_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "title": "Copy &amp; paste between workbooks",
        "body": BODY + f"<p>Case number {question_id}.</p>",
        "creation_date": 1_760_000_000,
        "link": f"https://example.test/q/{question_id}",
        "owner": {"user_id": question_id * 10, "display_name": "RealName"},
        "score": 4,
        "answer_count": 2,
        "view_count": 900,
        **overrides,
    }


def _answer(answer_id: int, question_id: int, score: int, accepted: bool = False) -> dict[str, Any]:
    return {
        "answer_id": answer_id,
        "question_id": question_id,
        "body": f"<p>Answer {answer_id}: you can script this with Power Query instead of copying"
        " the rows across by hand each week.</p>",
        "creation_date": 1_760_000_100,
        "owner": {"user_id": answer_id * 10},
        "score": score,
        "is_accepted": accepted,
    }


def _client(tmp_path: Path, handler: Any, sleeps: list[float] | None = None) -> StackExchangeClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    recorded = sleeps if sleeps is not None else []
    return StackExchangeClient(http, tmp_path, api_key=None, sleep=recorded.append)


def test_repo_config_loads() -> None:
    settings = load_settings(SOURCES_PATH)
    assert settings.sites
    assert settings.keywords
    assert settings.top_answers_per_question > 0


def test_queries_search_each_tag_separately() -> None:
    assert queries(Site("so", ("excel", "vba")), ("sync", "import")) == [
        ("sync", "excel"),
        ("import", "excel"),
        ("sync", "vba"),
        ("import", "vba"),
    ]
    assert queries(Site("superuser", ()), ("sync",)) == [("sync", None)]


def test_search_params_only_tag_when_given() -> None:
    assert "tagged" not in search_params("superuser", "sync", None, 1, NOW, 1)
    params = search_params("stackoverflow", "sync", "excel", 2, NOW, 1)
    assert params["tagged"] == "excel"
    assert params["fromdate"] == int(NOW.timestamp())


def test_question_maps_to_a_raw_item_without_the_username() -> None:
    item = question_to_item(_question(7), "superuser", "salt")
    assert item.platform == PLATFORM
    assert item.external_id == "superuser:7"
    assert item.parent_external_id is None
    assert item.title == "Copy & paste between workbooks"
    assert item.text.startswith("Every week I manually copy rows")
    assert (item.score, item.num_comments, item.view_count) == (4, 2, 900)
    assert item.created_at == datetime.fromtimestamp(1_760_000_000, UTC)
    assert item.author_hash is not None
    assert "RealName" not in repr(item)


def test_deleted_owner_gives_no_author_hash() -> None:
    question = _question(7, owner={"user_type": "does_not_exist"})
    assert question_to_item(question, "superuser", "salt").author_hash is None


def test_answer_points_at_its_question() -> None:
    item = answer_to_item(_answer(70, 7, score=3), "superuser", "salt")
    assert (item.external_id, item.parent_external_id) == ("superuser:70", "superuser:7")


def test_pick_answers_prefers_accepted_then_votes() -> None:
    answers = [
        _answer(1, 7, score=50),
        _answer(2, 7, score=2, accepted=True),
        _answer(3, 7, score=9),
        _answer(4, 8, score=1),
    ]
    assert [a["answer_id"] for a in pick_answers(answers, 2)] == [2, 1, 4]


def test_client_follows_pages_until_has_more_is_false(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": [{"n": page}], "has_more": page < 2})

    pages = list(_client(tmp_path, handler).pages("/search/advanced", {"site": "s"}, 5))
    assert pages == [[{"n": 1}], [{"n": 2}]]


def test_client_stops_at_max_pages(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "has_more": True})

    assert len(list(_client(tmp_path, handler).pages("/search/advanced", {"site": "s"}, 3))) == 3


def test_client_honours_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "backoff": 7})

    _client(tmp_path, handler, sleeps).get("/search/advanced", {"site": "s"})
    assert sleeps == [7.0]


def test_client_stops_when_quota_runs_low(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "quota_remaining": 2})

    with pytest.raises(QuotaExhausted):
        _client(tmp_path, handler).get("/search/advanced", {"site": "s"})


def _api(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/search/advanced"):
        short = _question(3, body="<p>too short</p>")
        return httpx.Response(200, json={"items": [_question(1), _question(2), short]})
    return httpx.Response(
        200,
        json={
            "items": [
                _answer(10, 1, score=5, accepted=True),
                _answer(11, 1, score=9),
                _answer(12, 1, score=1),
                _answer(20, 2, score=3),
            ]
        },
    )


def test_run_site_stores_questions_and_top_answers_once(conn: Connection, tmp_path: Path) -> None:
    client = _client(tmp_path, _api)
    first = run_site(conn, client, _settings(), SITE, "test-run", "salt", NOW)
    try:
        assert (first.seen, first.items_new, first.skipped_short) == (6, 5, 1)
        rows = conn.execute(
            """
            SELECT r.external_id, r.parent_id IS NOT NULL
            FROM raw_items r JOIN sources s ON s.id = r.source_id
            WHERE s.target = %s ORDER BY r.external_id
            """,
            (SITE.name,),
        ).fetchall()
        assert rows == [
            ("test-site:1", False),
            ("test-site:10", True),
            ("test-site:11", True),
            ("test-site:2", False),
            ("test-site:20", True),
        ]

        second = run_site(conn, client, _settings(), SITE, "test-run", "salt", NOW)
        assert second.items_new == 0
        runs = conn.execute(
            """
            SELECT sr.items_new, sr.errors FROM scrape_runs sr
            JOIN sources s ON s.id = sr.source_id WHERE s.target = %s ORDER BY sr.id
            """,
            (SITE.name,),
        ).fetchall()
        assert runs == [(5, []), (0, [])]
    finally:
        # run_site commits as it goes, so this test has to clean up after itself
        delete_source(conn, PLATFORM, SITE.name)


def test_run_site_records_a_failed_query_and_carries_on(conn: Connection, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("q") == "broken":
            return httpx.Response(400, json={"error_id": 502})
        return _api(request)

    settings = _settings(keywords=("broken", "manually"), include_answers=False)
    try:
        stats = run_site(conn, _client(tmp_path, handler), settings, SITE, "test-run", "salt", NOW)
        assert stats.items_new == 2
        row = conn.execute(
            """
            SELECT jsonb_array_length(sr.errors), sr.errors -> 0 ->> 'stage' FROM scrape_runs sr
            JOIN sources s ON s.id = sr.source_id WHERE s.target = %s
            """,
            (SITE.name,),
        ).fetchone()
        assert row == (1, "search")
    finally:
        delete_source(conn, PLATFORM, SITE.name)


def test_run_site_respects_the_limit(conn: Connection, tmp_path: Path) -> None:
    settings = _settings(include_answers=False)
    try:
        stats = run_site(
            conn, _client(tmp_path, _api), settings, SITE, "test-run", "salt", NOW, limit=1
        )
        assert (stats.seen, stats.items_new) == (1, 1)
    finally:
        delete_source(conn, PLATFORM, SITE.name)
