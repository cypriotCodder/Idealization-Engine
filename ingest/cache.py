"""On-disk cache of raw API responses, so a re-run never fetches the same page twice."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "http"
# never let credentials into the cache key, or rotating a key would orphan the whole cache
_SECRET_PARAMS = frozenset({"key", "access_token", "token"})

type Params = Mapping[str, str | int]


def cache_key(url: str, params: Params) -> str:
    public = sorted((k, str(v)) for k, v in params.items() if k not in _SECRET_PARAMS)
    return hashlib.sha256(json.dumps([url, public]).encode("utf-8")).hexdigest()


def get_json(
    client: httpx.Client,
    url: str,
    params: Params,
    cache_dir: Path,
    refresh: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Return (payload, came_from_cache). Failed requests raise and are never cached."""
    path = cache_dir / f"{cache_key(url, params)}.json"
    if not refresh and path.exists():
        return dict(json.loads(path.read_text(encoding="utf-8"))), True
    response = client.get(url, params=dict(params))
    response.raise_for_status()
    payload = dict(response.json())
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, False
