from dataclasses import replace
from datetime import UTC, datetime

from db.connection import Connection
from ingest.store import RawItem, finish_run, insert_item, start_run, upsert_source

PLATFORM = "test-platform"
LONG_TEXT = "Every week I copy the same numbers from one spreadsheet into another by hand."


def _item(external_id: str, **overrides: object) -> RawItem:
    base = RawItem(
        platform=PLATFORM,
        external_id=external_id,
        text=LONG_TEXT,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        author_hash="author-1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _count(conn: Connection) -> int:
    row = conn.execute("SELECT count(*) FROM raw_items WHERE platform = %s", (PLATFORM,)).fetchone()
    assert row is not None
    return int(row[0])


def test_upsert_source_is_idempotent(conn: Connection) -> None:
    assert upsert_source(conn, PLATFORM, "site", "run1") == upsert_source(
        conn, PLATFORM, "site", "run1"
    )


def test_rescraping_the_same_item_adds_nothing(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    assert insert_item(conn, source_id, _item("q1")) is True
    assert insert_item(conn, source_id, _item("q1", text=LONG_TEXT + " edited")) is False
    assert _count(conn) == 1


def test_same_author_reposting_is_dropped(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    assert insert_item(conn, source_id, _item("q1")) is True
    assert insert_item(conn, source_id, _item("q2", text=LONG_TEXT.upper())) is False
    assert _count(conn) == 1


def test_different_authors_with_the_same_text_are_both_kept(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    assert insert_item(conn, source_id, _item("q1")) is True
    assert insert_item(conn, source_id, _item("q2", author_hash="author-2")) is True


def test_anonymous_reposts_are_dropped(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    assert insert_item(conn, source_id, _item("q1", author_hash=None)) is True
    assert insert_item(conn, source_id, _item("q2", author_hash=None)) is False


def test_reply_points_at_its_parent(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    insert_item(conn, source_id, _item("q1"))
    reply = _item("a1", parent_external_id="q1", text="An answer: " + LONG_TEXT)
    assert insert_item(conn, source_id, reply) is True
    row = conn.execute(
        """
        SELECT parent.external_id FROM raw_items child
        JOIN raw_items parent ON parent.id = child.parent_id
        WHERE child.platform = %s AND child.external_id = %s
        """,
        (PLATFORM, "a1"),
    ).fetchone()
    assert row == ("q1",)


def test_reply_without_a_stored_parent_is_skipped(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    assert insert_item(conn, source_id, _item("a1", parent_external_id="missing")) is False
    assert _count(conn) == 0


def test_run_is_logged_with_its_errors(conn: Connection) -> None:
    source_id = upsert_source(conn, PLATFORM, "site", "run1")
    run_id = start_run(conn, source_id)
    finish_run(conn, run_id, 7, [{"stage": "search", "error": "boom"}])
    row = conn.execute(
        "SELECT items_new, errors, finished_at IS NOT NULL FROM scrape_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row == (7, [{"stage": "search", "error": "boom"}], True)
