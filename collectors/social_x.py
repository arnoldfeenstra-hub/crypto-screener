"""Phase 0 item 3 -- X collector.

Polls X for every triggered token at t+0, +1h, +6h, +24h and appends **raw counts**
to ``social_observations``. Never an update to the snapshot row.

Two constraints from BUILD_BRIEF.md shape this:

**Store raw counts, not derived scores** (section 3, item 3). Author diversity,
mention slope and reach quality are formulas that will change during calibration;
``mentions_window`` and ``unique_authors`` will not. The derivation lives in
``collectors/social_base.derive_x_metrics`` and runs at read time, so a formula
change re-interprets every row ever collected instead of splitting the dataset into
incompatible eras.

**This data cannot be backfilled** (section 1). X full-archive search is
enterprise-tier; LunarCrush and Santiment index roughly 4,000 tracked assets and a
token that launched three hours ago is not among them. Every hour this is not
running is an hour that cannot be recovered. That is why a failed poll is recorded
as a row with ``error`` set rather than skipped -- the gap is data too.

The search endpoint used here is the standard v2 recent-search
(``/2/tweets/search/recent``), which covers the last 7 days. That is enough for the
24h series, and is available on tiers below enterprise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import requests

from collectors.config import load_config
from collectors.schema import now_ms
from collectors.social_base import (
    OFFSETS_MINUTES,
    SocialObservation,
    due_offsets,
)
from collectors.store import Store

log = logging.getLogger("social_x")

PHASE = "0.3"
PLATFORM = "x"
API_BASE = "https://api.x.com/2"
# A follower count above this makes an engaging account "tier 1" (prompts/score.md
# pillar A: first unpaid engagement from a >100k-follower account is a step change).
TIER1_FOLLOWERS = 100_000
# An account posting more than this many distinct tickers a day is a paid caller.
PAID_CALLER_TICKERS_PER_DAY = 3


class XError(RuntimeError):
    """An X API request failed."""


@dataclass
class XClient:
    """Thin X API v2 client. Reads only; posts nothing."""

    bearer_token: str
    base_url: str = API_BASE
    timeout: float = 30.0
    max_retries: int = 3
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                assert self.session is not None
                response = self.session.get(
                    f"{self.base_url}{path}",
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    # X rate limits hard. Respect the reset header rather than
                    # hammering: a burned quota costs the whole polling round.
                    reset = response.headers.get("x-rate-limit-reset")
                    wait = 60.0
                    if reset:
                        wait = max(1.0, float(reset) - time.time())
                    raise XError(f"rate limited, resets in {wait:.0f}s")
                response.raise_for_status()
                return response.json()
            except (XError, requests.RequestException) as exc:
                last = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 30))
        raise XError(f"x api failed after {self.max_retries} attempts: {last}")

    def search_recent(self, query: str, minutes: int, max_results: int = 100) -> dict[str, Any]:
        return self._get(
            "/tweets/search/recent",
            {
                "query": query,
                "max_results": min(100, max_results),
                "start_time": _iso_since(minutes),
                "tweet.fields": "author_id,public_metrics,created_at,referenced_tweets",
                "expansions": "author_id",
                "user.fields": "public_metrics,username",
            },
        )


def build_query(ticker: str | None, contract: str | None) -> str:
    """Search for the ticker or the contract address, excluding retweets.

    Retweets are excluded because pillar A measures author diversity, and a
    retweet cascade from one source would read as many independent authors.
    """
    terms = []
    if ticker:
        terms.append(ticker if ticker.startswith("$") else f"${ticker}")
    if contract:
        terms.append(contract)
    if not terms:
        raise ValueError("need a ticker or a contract to search for")
    return f"({' OR '.join(terms)}) -is:retweet"


def parse_search(payload: dict[str, Any], window_minutes: int) -> dict[str, Any]:
    """Reduce a search response to raw counts. Pure and total."""
    tweets = payload.get("data") or []
    users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}

    authors: set[str] = set()
    reach = 0.0
    tier1 = 0
    replies = 0
    posts = 0

    for tweet in tweets:
        author_id = tweet.get("author_id")
        if author_id:
            authors.add(author_id)
            user = users.get(author_id) or {}
            followers = (user.get("public_metrics") or {}).get("followers_count")
            if followers:
                reach += float(followers)
                metrics = tweet.get("public_metrics") or {}
                engaged = (metrics.get("like_count", 0) or 0) + (
                    metrics.get("retweet_count", 0) or 0
                )
                if followers >= TIER1_FOLLOWERS and engaged > 0:
                    tier1 += 1
        referenced = tweet.get("referenced_tweets") or []
        if any(r.get("type") == "replied_to" for r in referenced):
            replies += 1
        else:
            posts += 1

    return {
        "mentions_window": len(tweets),
        "window_minutes": window_minutes,
        "unique_authors": len(authors) or None,
        "follower_weighted_reach": reach or None,
        "tier1_organic_engagements": tier1,
        "replies": replies,
        "posts": posts,
    }


class XCollector:
    def __init__(self, store: Store, client: XClient | None = None) -> None:
        self.store = store
        self.client = client

    def collect_one(
        self, snapshot: dict[str, Any], offset_minutes: int
    ) -> SocialObservation:
        """One observation. A failure becomes a row with ``error``, never a zero."""
        base = {
            "snapshot_id": snapshot["snapshot_id"],
            "platform": PLATFORM,
            "offset_minutes": offset_minutes,
            "handle": snapshot.get("ticker"),
            "source": "x_api_v2",
        }
        if self.client is None:
            return SocialObservation(**base, error="no client configured")
        window = max(offset_minutes, 60)
        try:
            payload = self.client.search_recent(
                build_query(snapshot.get("ticker"), snapshot.get("contract")), window
            )
        except (XError, ValueError) as exc:
            return SocialObservation(**base, error=str(exc)[:200])
        counts = parse_search(payload, window)
        return SocialObservation(**base, exists=counts["mentions_window"] > 0, **counts)

    def run(self, *, as_of_ms: int | None = None, limit: int = 200) -> list[SocialObservation]:
        as_of = as_of_ms if as_of_ms is not None else now_ms()
        written: list[SocialObservation] = []
        for snapshot in self.store.snapshots_for_labelling()[:limit]:
            age = int((as_of - snapshot["ts"]) // 60_000)
            done = self.store.social_offsets_collected(snapshot["snapshot_id"], PLATFORM)
            for offset in due_offsets(age, done):
                observation = self.collect_one(snapshot, offset)
                self.store.append_social_observations([observation])
                written.append(observation)
        return written


def _iso_since(minutes: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="social-x",
        description="Phase 0 item 3. Collect raw X counts for triggered tokens.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()
    token = os.environ.get("X_BEARER_TOKEN")
    client = XClient(bearer_token=token) if token else None
    if client is None:
        log.warning(
            "X_BEARER_TOKEN is not set; attempts will be recorded as errors rather "
            "than counts, which is still a truthful row"
        )

    with Store(args.db or str(config.db_path)) as store:
        written = XCollector(store, client).run(limit=args.limit)
        payload = {
            "observations_written": len(written),
            "with_counts": sum(1 for o in written if o.error is None),
            "with_errors": sum(1 for o in written if o.error is not None),
            "offsets": list(OFFSETS_MINUTES),
        }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
