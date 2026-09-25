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
import sys
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
    newest_first,
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

# The per-cycle search budget. Read by collect.py and by this module's CLI, in both
# cases *after* load_config() has loaded .env -- reading it at argparse time, before
# .env is loaded, honoured the token in .env and silently ignored the budget beside it.
MAX_SEARCHES_ENV = "X_MAX_SEARCHES_PER_CYCLE"


def max_searches_from_env(raw: str | None = None) -> int | None:
    """The per-cycle X search budget from ``X_MAX_SEARCHES_PER_CYCLE``.

    Unset *or blank* is no cap: GitHub Actions passes an undefined repository
    variable as an empty string, and that must not crash the collector before it
    has restored anything. ``0`` is a budget of zero searches -- never "unlimited",
    which is what ``int(...) or None`` made of it -- because this is the one
    metered source and a cap set to 0 to pause spending has to pause it. Anything
    that is not a non-negative whole number raises, naming the variable, rather
    than being read as no cap.
    """
    text = (raw if raw is not None else os.environ.get(MAX_SEARCHES_ENV, "")).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        raise ValueError(
            f"{MAX_SEARCHES_ENV}={text!r} is not a whole number of searches"
        ) from None
    if value < 0:
        raise ValueError(f"{MAX_SEARCHES_ENV}={text!r} is negative")
    return value


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
        # Due offsets that a budget stopped this cycle from asking about. Not
        # written anywhere: a poll that never happened is not an observation.
        self.skipped_for_budget = 0

    def collect_one(
        self,
        snapshot: dict[str, Any],
        offset_minutes: int,
        *,
        age_minutes: int | None = None,
    ) -> SocialObservation:
        """One observation. A failure becomes a row with ``error``, never a zero."""
        base = {
            "snapshot_id": snapshot["snapshot_id"],
            "platform": PLATFORM,
            "offset_minutes": offset_minutes,
            "age_minutes": age_minutes,
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
            # The anticipated failures. Their messages already say what happened
            # ("rate limited, resets in 60s"), so they are recorded verbatim.
            return SocialObservation(**base, error=str(exc)[:200])
        except Exception as exc:
            # Everything else, and deliberately so. The client is an injection
            # point ("any object with search_recent"), and an unanticipated
            # failure from it is where narrow catching costs most: the exception
            # escapes the loop, every remaining snapshot in the cycle is skipped,
            # and the hour passes with no row anywhere saying so. This data cannot
            # be backfilled, so a gap has to become a row -- the module's own
            # opening rule, "a failed poll is recorded as a row with `error` set
            # rather than skipped. The gap is data too." The type is kept in the
            # message because an unexpected exception's text usually is not
            # self-describing.
            return SocialObservation(**base, error=f"{type(exc).__name__}: {exc}"[:200])
        counts = parse_search(payload, window)
        return SocialObservation(**base, exists=counts["mentions_window"] > 0, **counts)

    def run(
        self,
        *,
        as_of_ms: int | None = None,
        limit: int = 200,
        max_searches: int | None = None,
    ) -> list[SocialObservation]:
        """Collect every due offset, up to ``max_searches`` actual API calls.

        The budget is not a nicety. Unlike every other source in this repo, X
        search is metered and billed: the paid tiers cap *posts read per month*,
        one search returns up to 100 of them, and this runs hourly. An unbudgeted
        first cycle over a backlog of snapshots can spend a month's quota before
        anyone reads the log.

        Hitting the cap stops the loop rather than writing error rows for the
        remainder. An error row means "we asked and it failed", which is a real
        observation about the token; a row saying the same about a poll that was
        never attempted would put the collector's own rate limit into the dataset
        as if it were a fact about the token. The count that was skipped is
        returned to the caller instead, which is where a budget belongs.
        """
        as_of = as_of_ms if as_of_ms is not None else now_ms()
        written: list[SocialObservation] = []
        self.skipped_for_budget = 0
        searches = 0
        # Newest first puts the on-time t+0 polls ahead of the backlog of overdue
        # offsets when the budget binds.
        for snapshot in newest_first(self.store.snapshots_for_labelling(), limit):
            age = int((as_of - snapshot["ts"]) // 60_000)
            done = self.store.social_offsets_collected(snapshot["snapshot_id"], PLATFORM)
            for offset in due_offsets(age, done):
                if max_searches is not None and searches >= max_searches:
                    self.skipped_for_budget += 1
                    continue
                observation = self.collect_one(snapshot, offset, age_minutes=age)
                searches += 1
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
    parser.add_argument(
        "--max-searches",
        type=int,
        default=None,
        help=(
            "stop after this many API calls (default: X_MAX_SEARCHES_PER_CYCLE, "
            "or unlimited). X search is the one metered source in this repo."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()
    try:
        max_searches = (
            args.max_searches if args.max_searches is not None else max_searches_from_env()
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    token = os.environ.get("X_BEARER_TOKEN")
    client = XClient(bearer_token=token) if token else None
    if client is None:
        log.warning(
            "X_BEARER_TOKEN is not set; attempts will be recorded as errors rather "
            "than counts, which is still a truthful row"
        )

    with Store(args.db or str(config.db_path)) as store:
        collector = XCollector(store, client)
        written = collector.run(limit=args.limit, max_searches=max_searches)
        payload = {
            "observations_written": len(written),
            "with_counts": sum(1 for o in written if o.error is None),
            "with_errors": sum(1 for o in written if o.error is not None),
            "skipped_for_budget": collector.skipped_for_budget,
            "offsets": list(OFFSETS_MINUTES),
        }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
