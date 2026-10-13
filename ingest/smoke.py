"""Phase 0 smoke test: read one Stack Exchange question. Run with `python -m ingest.smoke`."""

import logging
import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
API_URL = "https://api.stackexchange.com/2.3/questions"


def stackexchange_config(sources_path: Path) -> dict[str, Any]:
    config = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    return dict(config["stackexchange"])


def newest_question(site: str, api_key: str | None) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "site": site,
        "pagesize": 1,
        "order": "desc",
        "sort": "creation",
    }
    # the key is optional: without it we share a small per-IP quota, which is plenty for one call
    if api_key:
        params["key"] = api_key
    response = httpx.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    logger.info("stack exchange quota remaining: %s", payload.get("quota_remaining"))
    return dict(payload["items"][0])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_dotenv()
    config = stackexchange_config(SOURCES_PATH)
    site = str(config["sites"][0]["site"])
    question = newest_question(site, os.environ.get(config["api_key_env"]))
    logger.info("read question %s from %s: %s", question["question_id"], site, question["title"])


if __name__ == "__main__":
    main()
