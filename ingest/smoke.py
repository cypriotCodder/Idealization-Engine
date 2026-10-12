"""Phase 0 smoke test: read one Reddit post. Run with `python -m ingest.smoke`.

Uses PRAW's read-only mode, so only the client id, secret and user agent are needed.
"""

import logging
import os
from pathlib import Path

import praw
import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"


def first_subreddit(sources_path: Path, run_label: str = "run1") -> str:
    config = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    return str(config[run_label]["reddit"]["subreddits"][0])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_dotenv()
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )
    subreddit = first_subreddit(SOURCES_PATH)
    post = next(iter(reddit.subreddit(subreddit).new(limit=1)))
    logger.info("read post %s from r/%s: %s", post.id, subreddit, post.title)


if __name__ == "__main__":
    main()
