from pathlib import Path

import httpx
import pytest

from ingest.cache import cache_key, get_json


def _client(calls: list[httpx.Request], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"items": [len(calls)]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cache_key_ignores_the_api_key() -> None:
    assert cache_key("u", {"q": "x", "key": "secret"}) == cache_key("u", {"q": "x"})
    assert cache_key("u", {"q": "x"}) != cache_key("u", {"q": "y"})


def test_second_call_is_served_from_disk(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    client = _client(calls)
    first = get_json(client, "https://api.test/q", {"q": "x"}, tmp_path)
    second = get_json(client, "https://api.test/q", {"q": "x"}, tmp_path)
    assert first == ({"items": [1]}, False)
    assert second == ({"items": [1]}, True)
    assert len(calls) == 1


def test_refresh_bypasses_the_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    client = _client(calls)
    get_json(client, "https://api.test/q", {"q": "x"}, tmp_path)
    payload, cached = get_json(client, "https://api.test/q", {"q": "x"}, tmp_path, refresh=True)
    assert (payload, cached) == ({"items": [2]}, False)


def test_failed_requests_are_not_cached(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with pytest.raises(httpx.HTTPStatusError):
        get_json(_client(calls, status=400), "https://api.test/q", {"q": "x"}, tmp_path)
    assert list(tmp_path.iterdir()) == []
