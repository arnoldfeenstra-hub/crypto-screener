"""Phase 0 item 3 -- Telegram collector.

Polls each triggered token's declared Telegram group at t+0, +1h, +6h, +24h and
appends raw counts, same discipline as the X collector: counts in, formulas at read
time (``collectors/social_base.derive_tg_metrics``).

Why the public preview rather than an API client
------------------------------------------------
``https://t.me/<channel>`` serves a public HTML preview carrying the member count
and, for groups, the online count. It needs no API id, no hash, no phone number and
no session file -- which means this collector can start running the day the repo is
cloned. Since member counts at a past timestamp are archived nowhere public
(BUILD_BRIEF.md section 1), starting today rather than after a credentials setup is
worth more than the extra fields a full client would give.

What that costs: message rates and unique speakers need a real client (Telethon or
similar) reading group history, and are left ``None`` here. That matters, because
pillar B in prompts/score.md cares about **speaker ratio** far more than raw
membership -- below roughly 2% is a dead room with a big number on it. So the
preview path collects the number that is cheap and the model wants least. Wiring a
full client is the single highest-value addition to this module, and
``TelegramCollector`` takes any client exposing ``fetch(handle)``, so it slots in
without touching the storage or scheduling code.

``socials_declared.telegram`` is collected at snapshot time and does not depend on
this module at all. On its own it is an 8.94x graduation lift -- the single
best-evidenced feature in the schema.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import requests

from collectors.config import load_config
from collectors.schema import now_ms
from collectors.social_base import OFFSETS_MINUTES, SocialObservation, due_offsets
from collectors.store import Store

log = logging.getLogger("social_tg")

PHASE = "0.3"
PLATFORM = "telegram"
PREVIEW_BASE = "https://t.me"

# The preview page renders counts as "12 345 members, 678 online".
_MEMBERS_RE = re.compile(r"([\d\s,  ]+)\s+(?:members|subscribers)", re.I)
_ONLINE_RE = re.compile(r"([\d\s,  ]+)\s+online", re.I)
_HANDLE_RE = re.compile(r"(?:t\.me/|telegram\.me/|@)([A-Za-z0-9_]{4,32})")


class TelegramError(RuntimeError):
    """A Telegram preview request failed."""


def extract_handle(value: str | None) -> str | None:
    """Pull a channel handle out of a URL, an @mention, or a bare handle."""
    if not value:
        return None
    match = _HANDLE_RE.search(value)
    if match:
        return match.group(1)
    candidate = value.strip().lstrip("@")
    return candidate if re.fullmatch(r"[A-Za-z0-9_]{4,32}", candidate) else None


def _parse_count(raw: str | None) -> int | None:
    """Parse a count that may carry spaces, commas or narrow no-break spaces."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def parse_preview(html: str) -> dict[str, Any]:
    """Read member and online counts out of a t.me preview page. Pure and total.

    A page that exists but shows no counts returns ``exists=True`` with null
    counts -- the channel is real and the number is unknown, which is a different
    row from a channel that does not exist.
    """
    if "tgme_page" not in html and "tgme_channel_info" not in html:
        return {"exists": False, "members": None, "online": None}
    members = _parse_count(m.group(1) if (m := _MEMBERS_RE.search(html)) else None)
    online = _parse_count(m.group(1) if (m := _ONLINE_RE.search(html)) else None)
    return {"exists": True, "members": members, "online": online}


@dataclass
class TelegramPreviewClient:
    """Reads the public preview page. No credentials, no session, read-only."""

    timeout: float = 20.0
    session: requests.Session | None = None
    base_url: str = PREVIEW_BASE

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()

    def fetch(self, handle: str) -> dict[str, Any]:
        assert self.session is not None
        try:
            response = self.session.get(
                f"{self.base_url}/{handle}",
                timeout=self.timeout,
                headers={"User-Agent": "crypto-screener/0.1 (+phase0 collector)"},
            )
        except requests.RequestException as exc:
            raise TelegramError(str(exc)) from exc
        if response.status_code == 404:
            return {"exists": False, "members": None, "online": None}
        if response.status_code >= 400:
            raise TelegramError(f"HTTP {response.status_code}")
        return parse_preview(response.text)


class TelegramCollector:
    def __init__(self, store: Store, client: Any | None = None) -> None:
        self.store = store
        self.client = client

    def collect_one(
        self, snapshot: dict[str, Any], offset_minutes: int
    ) -> SocialObservation:
        handle = extract_handle(snapshot.get("telegram") or snapshot.get("ticker"))
        base = {
            "snapshot_id": snapshot["snapshot_id"],
            "platform": PLATFORM,
            "offset_minutes": offset_minutes,
            "handle": handle,
            "source": "t.me_preview",
        }
        if handle is None:
            return SocialObservation(**base, error="no telegram handle on the snapshot")
        if self.client is None:
            return SocialObservation(**base, error="no client configured")
        try:
            counts = self.client.fetch(handle)
        except TelegramError as exc:
            return SocialObservation(**base, error=str(exc)[:200])
        return SocialObservation(
            **base,
            exists=counts.get("exists"),
            members=counts.get("members"),
            online=counts.get("online"),
            # A real client reading group history fills these; the preview cannot.
            messages_window=None,
            unique_speakers=None,
        )

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="social-tg",
        description="Phase 0 item 3. Collect raw Telegram counts for triggered tokens.",
    )
    parser.add_argument("--db", help="DuckDB path (default: SCREENER_DB_PATH)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="record attempts without making requests (useful for a dry run)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = load_config()
    client = None if args.offline else TelegramPreviewClient()

    with Store(args.db or str(config.db_path)) as store:
        written = TelegramCollector(store, client).run(limit=args.limit)
        payload = {
            "observations_written": len(written),
            "with_counts": sum(1 for o in written if o.error is None),
            "with_errors": sum(1 for o in written if o.error is not None),
            "offsets": list(OFFSETS_MINUTES),
            "note": (
                "messages_per_hour and unique_speakers need a full Telegram client; "
                "the preview page does not expose them. See the module docstring."
            ),
        }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
