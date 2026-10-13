"""G2 / Capterra review ingest through a paid Apify actor.

Run with `python -m ingest.apify_reviews --product Pipedrive`.

Every actor run costs real money, so this module is built around never paying twice:
the run id is written to disk the moment a run starts, and the downloaded reviews are
kept in `.cache/apify/`. A re-run imports from those files and starts nothing new.
"""

import argparse
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from db.connection import Connection, connect
from ingest.common import RunStats, load_salt, setup_logging, store_items
from ingest.hashing import author_hash
from ingest.store import RawItem, finish_run, start_run, upsert_source

logger = logging.getLogger(__name__)

API_URL = "https://api.apify.com/v2"
SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
DEFAULT_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "apify"
FINISHED = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
# each poll asks Apify to hold the request open this long, so we aren't hammering it
POLL_WAIT_SECONDS = 60
MAX_POLLS = 30


@dataclass(frozen=True)
class Settings:
    active: bool
    run_label: str
    api_token_env: str
    actor: str
    platforms: tuple[str, ...]
    star_ratings: tuple[int, ...]
    sort: str
    max_results_per_product: int
    max_charge_usd_per_product: float
    max_total_charge_usd: float
    products: tuple[str, ...]
    min_text_length: int


def load_settings(path: Path) -> Settings:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = config["apify_reviews"]
    return Settings(
        active=bool(section["active"]),
        run_label=str(config["run_label"]),
        api_token_env=str(section["api_token_env"]),
        actor=str(section["actor"]),
        platforms=tuple(section["platforms"]),
        star_ratings=tuple(int(s) for s in section["star_ratings"]),
        sort=str(section["sort"]),
        max_results_per_product=int(section["max_results_per_product"]),
        max_charge_usd_per_product=float(section["max_charge_usd_per_product"]),
        max_total_charge_usd=float(section["max_total_charge_usd"]),
        products=tuple(section["products"]),
        min_text_length=int(config["defaults"]["min_text_length"]),
    )


def actor_input(settings: Settings, product: str) -> dict[str, Any]:
    return {
        "query": product,
        "platforms": list(settings.platforms),
        "maxResults": settings.max_results_per_product,
        "sort": settings.sort,
        # the actor's schema wants the stars as strings
        "starRatings": [str(star) for star in settings.star_ratings],
    }


def run_key(actor: str, actor_input_: dict[str, Any]) -> str:
    """Same actor + same input = same key, which is how a re-run finds its earlier result."""
    blob = json.dumps([actor, actor_input_], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class ApifyRunFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Reviews:
    items: list[dict[str, Any]]
    # what Apify says the run cost; None when the reviews came from the local cache
    charged_usd: float | None
    status: str


class ApifyClient:
    def __init__(
        self,
        http: httpx.Client,
        token: str,
        cache_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        # header, not ?token=, so the secret never lands in a logged URL
        self._headers = {"Authorization": f"Bearer {token}"}
        self._cache_dir = cache_dir
        self._sleep = sleep

    def _items_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.items.json"

    def _run_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.run.json"

    def is_cached(self, actor: str, actor_input_: dict[str, Any]) -> bool:
        key = run_key(actor, actor_input_)
        # a saved run id counts too: that run is already paid for, we only need to collect it
        return self._items_path(key).exists() or self._run_path(key).exists()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(
            method, f"{API_URL}{path}", headers=self._headers, timeout=120, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def _start(self, actor: str, actor_input_: dict[str, Any], max_charge_usd: float) -> str:
        payload = self._request(
            "POST",
            f"/acts/{actor.replace('/', '~')}/runs",
            # Apify enforces this cap on its side, so a bug here can't overspend
            params={"maxTotalChargeUsd": max_charge_usd},
            json=actor_input_,
        )
        return str(payload["data"]["id"])

    def _wait(self, run_id: str) -> dict[str, Any]:
        for _ in range(MAX_POLLS):
            run = dict(
                self._request(
                    "GET", f"/actor-runs/{run_id}", params={"waitForFinish": POLL_WAIT_SECONDS}
                )["data"]
            )
            if run["status"] in FINISHED:
                return run
            self._sleep(1)
        raise ApifyRunFailed(f"run {run_id} did not finish in time; re-run to keep waiting")

    def reviews(self, actor: str, actor_input_: dict[str, Any], max_charge_usd: float) -> Reviews:
        key = run_key(actor, actor_input_)
        items_path, run_path = self._items_path(key), self._run_path(key)
        if items_path.exists():
            return Reviews(json.loads(items_path.read_text(encoding="utf-8")), None, "CACHED")

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        if run_path.exists():
            # we crashed or were interrupted after paying; pick the same run back up
            run_id = str(json.loads(run_path.read_text(encoding="utf-8"))["run_id"])
            logger.info("resuming apify run_id=%s", run_id)
        else:
            run_id = self._start(actor, actor_input_, max_charge_usd)
            # write this before anything else can fail, or a crash would make us pay again
            run_path.write_text(
                json.dumps({"run_id": run_id, "input": actor_input_}), encoding="utf-8"
            )

        run = self._wait(run_id)
        items = list(
            self._request(
                "GET",
                f"/datasets/{run['defaultDatasetId']}/items",
                params={"clean": "true", "format": "json"},
            )
        )
        # a run stopped by the cost cap may not report SUCCEEDED but still has paid-for reviews
        if not items:
            raise ApifyRunFailed(
                f"run {run_id} ended {run['status']} with no reviews; "
                f"delete {run_path.name} from .cache/apify to pay for a new attempt"
            )
        items_path.write_text(json.dumps(items), encoding="utf-8")
        charged = run.get("usageTotalUsd")
        return Reviews(items, float(charged) if charged is not None else None, str(run["status"]))


def _author(reviewer: dict[str, Any], platform: str, salt: str) -> str | None:
    name = reviewer.get("name")
    if not name:
        return None
    # many reviewers show as "Verified User in <industry>", so the name alone is weak;
    # company and size make two different anonymous reviewers less likely to merge
    identity = "|".join(str(reviewer.get(k) or "") for k in ("name", "company", "companySize"))
    return author_hash(platform, identity, salt)


def review_to_item(review: dict[str, Any], salt: str) -> RawItem | None:
    platform, review_id, date = review.get("platform"), review.get("reviewId"), review.get("date")
    if not platform or not review_id or not date:
        return None
    body = " ".join(str(review.get("text") or "").split())
    cons = " ".join(str(review.get("cons") or "").split())
    # the "what do you dislike" answer is where the pain is, so it rides along with the body
    text = f"{body} Dislikes: {cons}".strip() if cons else body
    headline = review.get("title")
    product = review.get("productName")
    rating = review.get("rating")
    return RawItem(
        platform=str(platform),
        external_id=str(review_id),
        text=text,
        created_at=datetime.fromisoformat(str(date)).replace(tzinfo=UTC),
        url=review.get("reviewUrl"),
        author_hash=_author(review.get("reviewer") or {}, str(platform), salt),
        # reviews rarely name the product, so the title carries it for the filter
        title=": ".join(str(part) for part in (product, headline) if part) or None,
        # G2 gives half stars; the column is a whole number, so 2.5 is stored as 2
        rating=int(rating) if rating is not None else None,
    )


def planned_spend(client: ApifyClient, settings: Settings, products: list[str]) -> float:
    new_runs = [
        p for p in products if not client.is_cached(settings.actor, actor_input(settings, p))
    ]
    return len(new_runs) * settings.max_charge_usd_per_product


def check_budget(client: ApifyClient, settings: Settings, products: list[str]) -> float:
    spend = planned_spend(client, settings, products)
    # small tolerance so ten runs at 0.45 isn't rejected by floating point noise
    if spend > settings.max_total_charge_usd + 1e-9:
        raise BudgetExceeded(
            f"this would start runs costing up to ${spend:.2f}, above the "
            f"${settings.max_total_charge_usd:.2f} limit in config/sources.yaml"
        )
    return spend


def run_product(
    conn: Connection,
    client: ApifyClient,
    settings: Settings,
    product: str,
    run_label: str,
    salt: str,
) -> dict[str, RunStats]:
    """Fetch one product's reviews and store them. Returns the stats per review site."""
    # one source row and one logged run per review site, even though one actor run feeds both
    runs = {}
    for platform in settings.platforms:
        source_id = upsert_source(conn, platform, product, run_label)
        runs[platform] = (source_id, start_run(conn, source_id))
    conn.commit()

    stats = {platform: RunStats() for platform in settings.platforms}
    errors: list[dict[str, Any]] = []
    try:
        result = client.reviews(
            settings.actor, actor_input(settings, product), settings.max_charge_usd_per_product
        )
    except (httpx.HTTPError, ApifyRunFailed) as error:
        logger.warning("product=%r error=%r", product, error)
        errors.append({"stage": "apify", "detail": f"product={product}", "error": repr(error)})
    else:
        by_platform: dict[str, list[RawItem]] = defaultdict(list)
        for review in result.items:
            item = review_to_item(review, salt)
            if item is not None and item.platform in runs:
                by_platform[item.platform].append(item)
        for platform, items in by_platform.items():
            store_items(conn, runs[platform][0], items, settings.min_text_length, stats[platform])
        logger.info(
            "product=%r status=%s reviews=%d charged_usd=%s",
            product,
            result.status,
            len(result.items),
            "cached" if result.charged_usd is None else f"{result.charged_usd:.4f}",
        )

    for platform, (_, run_id) in runs.items():
        finish_run(conn, run_id, stats[platform].items_new, errors)
        logger.info(
            "platform=%s product=%r run_label=%s seen=%d items_new=%d skipped_short=%d errors=%d",
            platform,
            product,
            run_label,
            stats[platform].seen,
            stats[platform].items_new,
            stats[platform].skipped_short,
            len(errors),
        )
    conn.commit()
    return stats


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", help="defaults to run_label in config/sources.yaml")
    parser.add_argument("--product", help="only this product (must be in config/sources.yaml)")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings(SOURCES_PATH)
    if not settings.active:
        logger.info("apify_reviews is not active in config/sources.yaml; nothing to do")
        return
    products = [p for p in settings.products if args.product in (None, p)]
    if not products:
        raise SystemExit(f"{args.product!r} is not in the products list in config/sources.yaml")
    token = os.environ.get(settings.api_token_env)
    if not token:
        raise SystemExit(f"set {settings.api_token_env} in .env")
    salt = load_salt()

    with httpx.Client() as http, connect() as conn:
        client = ApifyClient(http, token, DEFAULT_CACHE_DIR)
        spend = check_budget(client, settings, products)
        logger.info("products=%d max_new_spend_usd=%.2f", len(products), spend)
        for product in products:
            run_product(conn, client, settings, product, args.run_label or settings.run_label, salt)


if __name__ == "__main__":
    main()
