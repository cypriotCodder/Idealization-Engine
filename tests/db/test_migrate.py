from pathlib import Path

import pytest

from db.migrate import MIGRATIONS_DIR, Migration, discover, pending


def _touch(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;", encoding="utf-8")


def test_discover_orders_by_numeric_version(tmp_path: Path) -> None:
    _touch(tmp_path, "010_later.sql", "002_second.sql", "001_init.sql")
    assert [m.version for m in discover(tmp_path)] == [1, 2, 10]


def test_discover_rejects_duplicate_versions(tmp_path: Path) -> None:
    _touch(tmp_path, "001_a.sql", "001_b.sql")
    with pytest.raises(ValueError, match="duplicate migration version 1"):
        discover(tmp_path)


def test_discover_rejects_unnumbered_files(tmp_path: Path) -> None:
    _touch(tmp_path, "init.sql")
    with pytest.raises(ValueError, match="must look like"):
        discover(tmp_path)


def test_pending_skips_applied_versions(tmp_path: Path) -> None:
    migrations = [Migration(1, tmp_path / "001_a.sql"), Migration(2, tmp_path / "002_b.sql")]
    assert pending(migrations, {1}) == [migrations[1]]


def test_repo_migrations_are_well_formed() -> None:
    assert discover(MIGRATIONS_DIR)[0].version == 1
