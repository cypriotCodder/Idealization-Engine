from ingest.hashing import author_hash, normalize_text, text_hash


def test_normalize_ignores_case_and_whitespace() -> None:
    assert normalize_text("  Every  WEEK\n\tI copy paste ") == "every week i copy paste"


def test_normalize_folds_unicode_variants() -> None:
    assert normalize_text("ｅｘｃｅｌ ﬁle") == "excel file"


def test_normalize_keeps_punctuation_and_urls() -> None:
    assert normalize_text("Is there a tool?") != normalize_text("Is there a tool")
    assert "https://example.com/a" in normalize_text("see https://example.com/a")


def test_text_hash_matches_for_reposts_only() -> None:
    assert text_hash("I export it  manually") == text_hash("i export it manually")
    assert text_hash("I export it manually") != text_hash("I import it manually")


def test_author_hash_is_stable() -> None:
    assert author_hash("stackexchange", "12345", "salt") == author_hash(
        "stackexchange", "12345", "salt"
    )


def test_author_hash_changes_with_salt() -> None:
    assert author_hash("stackexchange", "12345", "a") != author_hash("stackexchange", "12345", "b")


def test_author_hash_separates_platforms() -> None:
    assert author_hash("stackexchange", "jo", "salt") != author_hash("hackernews", "jo", "salt")


def test_author_hash_does_not_leak_the_author() -> None:
    assert "some_username" not in author_hash("hackernews", "some_username", "salt")
