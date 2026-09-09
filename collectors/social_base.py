"""Shared machinery for the Phase 0 item 3 social collectors.

Both platforms need the same three things: a raw-counts record, a fixed offset
schedule, and a way to record that a collection was *attempted and failed* --
which is not the same as a count of zero, and must not be stored as one.

The offsets come from BUILD_BRIEF.md section 3: t+0, +1h, +6h, +24h.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from collectors.schema import now_ms

# t+0, +1h, +6h, +24h (BUILD_BRIEF.md section 3, item 3).
OFFSETS_MINUTES: tuple[int, ...] = (0, 60, 360, 1440)

# How close to a scheduled offset a collection has to land to count as that offset.
OFFSET_TOLERANCE_MINUTES = 20


@dataclass(frozen=True, slots=True)
class SocialObservation:
    """Raw counts for one token, one platform, one offset.

    Everything here is a count or a null. No ratios, no scores, no normalisation:
    those are formulas, and formulas change. ``error`` records a failed attempt so
    that "we looked and found nothing" stays distinguishable from "we never looked"
    and from "the API refused us".
    """

    snapshot_id: str
    platform: str
    offset_minutes: int
    ts: int = field(default_factory=now_ms)
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    handle: str | None = None
    exists: bool | None = None

    mentions_window: int | None = None
    window_minutes: int | None = None
    unique_authors: int | None = None
    follower_weighted_reach: float | None = None
    tier1_organic_engagements: int | None = None
    replies: int | None = None
    posts: int | None = None

    members: int | None = None
    online: int | None = None
    messages_window: int | None = None
    unique_speakers: int | None = None

    source: str = "unknown"
    error: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "snapshot_id": self.snapshot_id,
            "platform": self.platform,
            "ts": self.ts,
            "offset_minutes": self.offset_minutes,
            "handle": self.handle,
            "exists": self.exists,
            "mentions_window": self.mentions_window,
            "window_minutes": self.window_minutes,
            "unique_authors": self.unique_authors,
            "follower_weighted_reach": self.follower_weighted_reach,
            "tier1_organic_engagements": self.tier1_organic_engagements,
            "replies": self.replies,
            "posts": self.posts,
            "members": self.members,
            "online": self.online,
            "messages_window": self.messages_window,
            "unique_speakers": self.unique_speakers,
            "source": self.source,
            "error": self.error,
        }


def nearest_offset(minutes_since_snapshot: int) -> int | None:
    """The scheduled offset a collection at this age belongs to, if any."""
    for offset in OFFSETS_MINUTES:
        if abs(minutes_since_snapshot - offset) <= OFFSET_TOLERANCE_MINUTES:
            return offset
    return None


def due_offsets(minutes_since_snapshot: int, collected: Iterable[int]) -> list[int]:
    """Offsets that have come due and have not been collected yet.

    A missed offset stays due once its window has passed, because a late count is
    still worth more than a hole -- but it is recorded against the offset it was
    scheduled for, not the moment it happened, so the series stays comparable
    across tokens.
    """
    done = set(collected)
    return [
        offset
        for offset in OFFSETS_MINUTES
        if offset not in done and minutes_since_snapshot >= offset
    ]


def derive_x_metrics(observations: list[SocialObservation]) -> dict[str, Any]:
    """Turn raw X counts into the section 4 ``social_x`` fields, at read time.

    This is the derivation the brief says to keep out of storage. Changing it
    changes every row's interpretation at once, which is the whole point.
    """
    by_offset = {o.offset_minutes: o for o in observations if o.platform == "x"}
    latest = by_offset.get(1440) or by_offset.get(360) or by_offset.get(60) or by_offset.get(0)
    if latest is None:
        return {}

    six = by_offset.get(360)
    return {
        "mentions_6h": six.mentions_window if six else None,
        "mentions_24h": by_offset.get(1440).mentions_window if 1440 in by_offset else None,
        "unique_authors_24h": (
            by_offset.get(1440).unique_authors if 1440 in by_offset else None
        ),
        "follower_weighted_reach": latest.follower_weighted_reach,
        "tier1_organic_engagements": latest.tier1_organic_engagements,
        "reply_to_post_ratio": (
            latest.replies / latest.posts
            if latest.replies is not None and latest.posts
            else None
        ),
    }


def derive_tg_metrics(observations: list[SocialObservation]) -> dict[str, Any]:
    """Turn raw Telegram counts into the section 4 ``social_tg`` fields."""
    by_offset = {o.offset_minutes: o for o in observations if o.platform == "telegram"}
    if not by_offset:
        return {}
    latest = max(by_offset.values(), key=lambda o: o.offset_minutes)
    first = by_offset.get(0)

    growth = None
    six = by_offset.get(360)
    if first and six and first.members:
        growth = (six.members - first.members) / first.members * 100 if six.members else None

    msgs_per_hour = None
    if latest.messages_window is not None and latest.offset_minutes:
        msgs_per_hour = latest.messages_window / (latest.offset_minutes / 60)

    return {
        "exists": latest.exists,
        "members": latest.members,
        "member_growth_6h_pct": growth,
        "msgs_per_hour": msgs_per_hour,
        "unique_speakers_24h": (
            by_offset.get(1440).unique_speakers if 1440 in by_offset else None
        ),
    }
