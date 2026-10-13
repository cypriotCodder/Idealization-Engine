from ingest.html_text import html_to_text


def test_strips_tags_and_separates_blocks() -> None:
    assert html_to_text("<p>First line</p><p>Second <b>bold</b> line</p>") == (
        "First line Second bold line"
    )


def test_unescapes_entities() -> None:
    assert html_to_text("<p>R&amp;D &lt;team&gt;</p>") == "R&D <team>"


def test_replaces_code_blocks_but_keeps_inline_code() -> None:
    html = "<p>I run <code>VLOOKUP</code> then</p><pre><code>Sub Foo()\nEnd Sub</code></pre>"
    assert html_to_text(html) == "I run VLOOKUP then [code]"


def test_plain_text_passes_through() -> None:
    assert html_to_text("no markup here") == "no markup here"
