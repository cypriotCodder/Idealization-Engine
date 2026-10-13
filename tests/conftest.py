from collections.abc import Iterator

import psycopg
import pytest

from db.connection import Connection, connect


@pytest.fixture
def conn() -> Iterator[Connection]:
    """A connection to the local dev database whose writes are rolled back after the test."""
    try:
        connection = connect()
    except (RuntimeError, psycopg.OperationalError) as error:
        pytest.skip(f"database not available: {error}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
