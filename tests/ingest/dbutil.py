from db.connection import Connection


def delete_source(conn: Connection, platform: str, target: str) -> None:
    """Remove a test source and everything scraped under it.

    The scrapers commit as they go, so tests that call them can't rely on a rollback.
    """
    conn.rollback()
    params = (platform, target)
    # replies first, they reference their parent row
    for only_replies in (True, False):
        conn.execute(
            """
            DELETE FROM raw_items
            WHERE (parent_id IS NOT NULL OR NOT %s)
              AND source_id IN (SELECT id FROM sources WHERE platform = %s AND target = %s)
            """,
            (only_replies, *params),
        )
    conn.execute(
        """
        DELETE FROM scrape_runs
        WHERE source_id IN (SELECT id FROM sources WHERE platform = %s AND target = %s)
        """,
        params,
    )
    conn.execute("DELETE FROM sources WHERE platform = %s AND target = %s", params)
    conn.commit()
