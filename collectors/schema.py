"""Snapshot schema -- BUILD_BRIEF.md section 4.

Three rules from CLAUDE.md shape this module:

  * Hard rule 3, never impute. Every feature field is optional and defaults to
    ``None``. Absent is ``None`` all the way down; it is never 0, never a mean,
    never a carried-forward value. A NaN from an upstream API is missing data, so
    it is normalised to ``None`` on the way in.
  * Conventions: timestamps UTC, stored as epoch milliseconds.
  * Hard rule 1, append-only. Forward labels (section 2) arrive hours to days after
    the snapshot, so writing them into the snapshot row would be an update. They
    live in a separate ``labels`` table keyed by ``snapshot_id``. The ``labels``
    key in the section 4 JSON is a marker, not storage.

Every section 4 field exists here even where Phase 0 items 3-5 cannot fill it yet.
The columns are created now so the table never needs altering once rows exist.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

# 3 adds the `safety_observations` table (collectors/safety.py). 2 added
# market.fdv_usd and the whole `mindshare` group. All additive, but
# DuckDB tables are created once and never altered here (append-only, and
# store.py may contain no ALTER), so a database written under version 1 cannot
# take version 2 rows. Store.open refuses it by name rather than failing on the
# insert. Phase 0 has no production database yet; if one exists, start a new file
# and keep the old one -- the old rows are still the graveyard.
SCHEMA_VERSION = 3

# Groups whose leaf fields count toward the Data Completeness modifier in
# prompts/score.md. Identity and bookkeeping columns are excluded: they are always
# present, so counting them would inflate completeness toward 1.0 for every row.
FEATURE_GROUPS = (
    "market",
    "holders",
    "authorities",
    "deployer",
    "launch",
    "flows",
    "mindshare",
    "social_x",
    "social_tg",
    "socials_declared",
    "trends",
    "lineage",
)

# Feature fields that live directly on Snapshot rather than inside a group.
FEATURE_SCALARS = ("age_at_trigger_minutes", "listings", "regime")


def now_ms() -> int:
    """Current UTC time as epoch milliseconds."""
    return int(datetime.now(UTC).timestamp() * 1000)


def to_ms(value: datetime | int | float | str | None) -> int | None:
    """Coerce a timestamp to epoch millis. Returns ``None`` for missing input.

    An unparseable string raises rather than silently becoming ``None``: a parse
    failure is a bug in a collector, not a missing measurement, and the two must
    not be recorded the same way.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(aware.timestamp() * 1000)
    if isinstance(value, bool):
        raise TypeError("bool is not a timestamp")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        # Callers pass millis. Sniffing seconds-vs-millis would be a guess.
        return int(value)
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def clean(value: Any) -> Any:
    """Normalise a collected value. NaN and infinity are missing data, not numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class Market:
    mcap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    price_usd: float | None = None
    # Fully diluted valuation. Separate from mcap because the liquidity-depth
    # filter prefers it and the two differ by the unvested supply -- which on a
    # fresh launch is most of it. DexScreener reports both; Bitquery reports
    # neither as FDV, so this stays None on those rows rather than copying mcap
    # across, which would quietly turn the filter into a different filter.
    fdv_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Holders:
    count: int | None = None
    growth_6h_pct: float | None = None
    top10_ex_lp_pct: float | None = None


@dataclass(frozen=True, slots=True)
class Authorities:
    mint_revoked: bool | None = None
    freeze_active: bool | None = None
    lp_locked_until: int | None = None  # epoch millis; None = unknown or not locked


@dataclass(frozen=True, slots=True)
class Deployer:
    address: str | None = None
    prior_launches: int | None = None
    prior_rugs: int | None = None


@dataclass(frozen=True, slots=True)
class Launch:
    bundled_supply_pct: float | None = None
    sniper_wallets: int | None = None
    initial_buy_sol: float | None = None


@dataclass(frozen=True, slots=True)
class Flows:
    net_flow_by_cohort: dict[str, Any] | None = None
    smart_money_entries: int | None = None
    smart_money_hit_rate: float | None = None


@dataclass(frozen=True, slots=True)
class Mindshare:
    """Share of the attention observed across one measurement universe.

    See :mod:`collectors.mindshare` for the formula and its caveats. The row keeps
    both halves on purpose: the derived share *and* the raw components with the
    universe totals they were divided by. Derived formulas change; raw counts do
    not (BUILD_BRIEF.md section 3 item 3), so every past row stays recomputable
    when the formula is revised.

    ``share_pct`` is only comparable within one universe, which is why
    ``universe_size`` sits beside it and is never dropped.
    """

    share_pct: float | None = None
    rank: int | None = None
    percentile: float | None = None
    universe_size: int | None = None
    txns_24h: int | None = None
    txns_6h: int | None = None
    boost_amount: float | None = None
    boost_total: float | None = None
    boosts_active: float | None = None
    pair_count: int | None = None
    universe_txns_24h: int | None = None
    universe_volume_24h_usd: float | None = None
    universe_boost_total: float | None = None


@dataclass(frozen=True, slots=True)
class SocialX:
    mentions_6h: int | None = None
    mentions_24h: int | None = None
    unique_authors_24h: int | None = None
    follower_weighted_reach: float | None = None
    tier1_organic_engagements: int | None = None
    reply_to_post_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class SocialTG:
    exists: bool | None = None
    members: int | None = None
    member_growth_6h_pct: float | None = None
    msgs_per_hour: float | None = None
    unique_speakers_24h: int | None = None


@dataclass(frozen=True, slots=True)
class SocialsDeclared:
    """The cheapest and best-evidenced feature in the schema (BUILD_BRIEF.md section 4).

    All three present: 1.919% graduation versus 0.110% without -- a 17.4x lift. It is
    a boolean triple readable from token metadata at launch, so it is collected from
    block one, before any of the expensive social series exist.
    """

    telegram: bool | None = None
    x: bool | None = None
    website: bool | None = None


@dataclass(frozen=True, slots=True)
class Trends:
    google_trends_delta: float | None = None
    tiktok_video_count_delta: float | None = None


@dataclass(frozen=True, slots=True)
class Lineage:
    meta_tag: str | None = None
    position_in_meta: str | None = None  # first | second | derivative


_GROUP_TYPES: dict[str, type] = {
    "market": Market,
    "holders": Holders,
    "authorities": Authorities,
    "deployer": Deployer,
    "launch": Launch,
    "flows": Flows,
    "mindshare": Mindshare,
    "social_x": SocialX,
    "social_tg": SocialTG,
    "socials_declared": SocialsDeclared,
    "trends": Trends,
    "lineage": Lineage,
}


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One token, captured once, at the moment it first crossed the trigger.

    Immutable by construction (``frozen=True``) because hard rule 1 says a snapshot
    row is never updated. If a value turns out to be wrong, write a new row with a
    later ``ts`` and ``supersedes`` set; do not edit the original.
    """

    # Identity and bookkeeping. Always present, so excluded from completeness.
    chain: str
    contract: str
    trigger: str  # "mcap_250k" | "holders_500"
    source: str
    ts: int = field(default_factory=now_ms)
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    collected_at_ms: int = field(default_factory=now_ms)
    schema_version: int = SCHEMA_VERSION
    ticker: str | None = None
    supersedes: str | None = None  # corrections are new rows, never edits

    # Both crossing flags are kept even though ``trigger`` names only one. The
    # section 4 enum has no "both" member, and dropping the second flag would be a
    # small act of data loss on the one field the whole cohort design rests on.
    trigger_mcap_crossed: bool = False
    trigger_holders_crossed: bool = False

    # Features.
    age_at_trigger_minutes: int | None = None
    regime: str | None = None  # hot | neutral | cold
    listings: list[str] | None = None
    market: Market = field(default_factory=Market)
    holders: Holders = field(default_factory=Holders)
    authorities: Authorities = field(default_factory=Authorities)
    deployer: Deployer = field(default_factory=Deployer)
    launch: Launch = field(default_factory=Launch)
    flows: Flows = field(default_factory=Flows)
    mindshare: Mindshare = field(default_factory=Mindshare)
    social_x: SocialX = field(default_factory=SocialX)
    social_tg: SocialTG = field(default_factory=SocialTG)
    socials_declared: SocialsDeclared = field(default_factory=SocialsDeclared)
    trends: Trends = field(default_factory=Trends)
    lineage: Lineage = field(default_factory=Lineage)

    @property
    def snapshot_date(self) -> str:
        """UTC date of ``ts``, ISO 8601. The partition key (CLAUDE.md conventions)."""
        return datetime.fromtimestamp(self.ts / 1000, tz=UTC).date().isoformat()

    def completeness(self) -> tuple[int, int]:
        """``(fields_present, fields_expected)`` over feature fields only.

        Feeds the Data Completeness modifier in prompts/score.md, which multiplies
        the weighted score by present/expected. Missingness is a feature; it is
        never compensated for with a guess.
        """
        present = 0
        expected = 0
        for name in FEATURE_SCALARS:
            expected += 1
            if getattr(self, name) is not None:
                present += 1
        for group_name in FEATURE_GROUPS:
            group = getattr(self, group_name)
            for f in fields(group):
                expected += 1
                if getattr(group, f.name) is not None:
                    present += 1
        return present, expected

    def to_dict(self) -> dict[str, Any]:
        """Nested section 4 shaped dict. The JSON export form."""
        out: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "ts": self.ts,
            "trigger": self.trigger,
            "ticker": self.ticker,
            "chain": self.chain,
            "contract": self.contract,
            "age_at_trigger_minutes": self.age_at_trigger_minutes,
        }
        for group_name in FEATURE_GROUPS:
            out[group_name] = asdict(getattr(self, group_name))
        out["listings"] = self.listings
        out["regime"] = self.regime
        out["labels"] = {"filled_at": None}  # marker; labels live in their own table
        present, expected = self.completeness()
        out["_meta"] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "collected_at_ms": self.collected_at_ms,
            "trigger_mcap_crossed": self.trigger_mcap_crossed,
            "trigger_holders_crossed": self.trigger_holders_crossed,
            "fields_present": present,
            "fields_expected": expected,
            "data_completeness": present / expected if expected else 0.0,
            "supersedes": self.supersedes,
        }
        return out

    def to_row(self) -> dict[str, Any]:
        """Flat column -> value mapping, ordered to match :data:`SNAPSHOT_COLUMNS`."""
        present, expected = self.completeness()
        row: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "ts": self.ts,
            "snapshot_date": self.snapshot_date,
            "trigger_kind": self.trigger,
            "trigger_mcap_crossed": self.trigger_mcap_crossed,
            "trigger_holders_crossed": self.trigger_holders_crossed,
            "ticker": self.ticker,
            "chain": self.chain,
            "contract": self.contract,
            "age_at_trigger_minutes": self.age_at_trigger_minutes,
            "regime": self.regime,
            "listings": json.dumps(self.listings) if self.listings is not None else None,
            "schema_version": self.schema_version,
            "source": self.source,
            "collected_at_ms": self.collected_at_ms,
            "supersedes": self.supersedes,
            "fields_present": present,
            "fields_expected": expected,
            "data_completeness": present / expected if expected else 0.0,
        }
        for group_name in FEATURE_GROUPS:
            group = getattr(self, group_name)
            for f in fields(group):
                value = clean(getattr(group, f.name))
                if isinstance(value, dict):
                    value = json.dumps(value)
                row[f"{group_name}_{f.name}"] = value
        return row


_GROUP_COLUMN_TYPES: dict[str, str] = {
    "market_mcap_usd": "DOUBLE",
    "market_liquidity_usd": "DOUBLE",
    "market_volume_24h_usd": "DOUBLE",
    "market_price_usd": "DOUBLE",
    "market_fdv_usd": "DOUBLE",
    "holders_count": "BIGINT",
    "holders_growth_6h_pct": "DOUBLE",
    "holders_top10_ex_lp_pct": "DOUBLE",
    "authorities_mint_revoked": "BOOLEAN",
    "authorities_freeze_active": "BOOLEAN",
    "authorities_lp_locked_until": "BIGINT",
    "deployer_address": "VARCHAR",
    "deployer_prior_launches": "BIGINT",
    "deployer_prior_rugs": "BIGINT",
    "launch_bundled_supply_pct": "DOUBLE",
    "launch_sniper_wallets": "BIGINT",
    "launch_initial_buy_sol": "DOUBLE",
    "flows_net_flow_by_cohort": "JSON",
    "flows_smart_money_entries": "BIGINT",
    "flows_smart_money_hit_rate": "DOUBLE",
    "mindshare_share_pct": "DOUBLE",
    "mindshare_rank": "BIGINT",
    "mindshare_percentile": "DOUBLE",
    "mindshare_universe_size": "BIGINT",
    "mindshare_txns_24h": "BIGINT",
    "mindshare_txns_6h": "BIGINT",
    "mindshare_boost_amount": "DOUBLE",
    "mindshare_boost_total": "DOUBLE",
    "mindshare_boosts_active": "DOUBLE",
    "mindshare_pair_count": "BIGINT",
    "mindshare_universe_txns_24h": "BIGINT",
    "mindshare_universe_volume_24h_usd": "DOUBLE",
    "mindshare_universe_boost_total": "DOUBLE",
    "social_x_mentions_6h": "BIGINT",
    "social_x_mentions_24h": "BIGINT",
    "social_x_unique_authors_24h": "BIGINT",
    "social_x_follower_weighted_reach": "DOUBLE",
    "social_x_tier1_organic_engagements": "BIGINT",
    "social_x_reply_to_post_ratio": "DOUBLE",
    "social_tg_exists": "BOOLEAN",
    "social_tg_members": "BIGINT",
    "social_tg_member_growth_6h_pct": "DOUBLE",
    "social_tg_msgs_per_hour": "DOUBLE",
    "social_tg_unique_speakers_24h": "BIGINT",
    "socials_declared_telegram": "BOOLEAN",
    "socials_declared_x": "BOOLEAN",
    "socials_declared_website": "BOOLEAN",
    "trends_google_trends_delta": "DOUBLE",
    "trends_tiktok_video_count_delta": "DOUBLE",
    "lineage_meta_tag": "VARCHAR",
    "lineage_position_in_meta": "VARCHAR",
}


def _group_columns() -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    for group_name in FEATURE_GROUPS:
        for f in fields(_GROUP_TYPES[group_name]):
            name = f"{group_name}_{f.name}"
            columns.append((name, _GROUP_COLUMN_TYPES[name]))
    return columns


# Single source of truth for the snapshots DDL. store.py builds CREATE TABLE from
# this, so adding a field is one edit in one place and the row/column order cannot
# drift apart.
SNAPSHOT_COLUMNS: list[tuple[str, str]] = [
    ("snapshot_id", "VARCHAR"),
    ("ts", "BIGINT"),
    ("snapshot_date", "DATE"),
    # `trigger` is a SQL keyword, so the column is `trigger_kind`. The section 4
    # JSON export keeps the original name.
    ("trigger_kind", "VARCHAR"),
    ("trigger_mcap_crossed", "BOOLEAN"),
    ("trigger_holders_crossed", "BOOLEAN"),
    ("ticker", "VARCHAR"),
    ("chain", "VARCHAR"),
    ("contract", "VARCHAR"),
    ("age_at_trigger_minutes", "BIGINT"),
    ("regime", "VARCHAR"),
    ("listings", "JSON"),
    ("schema_version", "INTEGER"),
    ("source", "VARCHAR"),
    ("collected_at_ms", "BIGINT"),
    ("supersedes", "VARCHAR"),
    ("fields_present", "INTEGER"),
    ("fields_expected", "INTEGER"),
    ("data_completeness", "DOUBLE"),
    *_group_columns(),
]

# Forward labels (BUILD_BRIEF.md section 2), filled by collectors/outcomes.py once
# it exists. Separate table: filling a label is an insert, never an update to the
# snapshot row.
LABEL_COLUMNS: list[tuple[str, str]] = [
    ("label_id", "VARCHAR"),
    ("snapshot_id", "VARCHAR"),
    ("filled_at_ms", "BIGINT"),
    ("max_multiple_1h", "DOUBLE"),
    ("max_multiple_6h", "DOUBLE"),
    ("max_multiple_24h", "DOUBLE"),
    ("max_multiple_72h", "DOUBLE"),
    ("max_multiple_7d", "DOUBLE"),
    ("max_drawdown_before_peak_24h", "DOUBLE"),
    ("max_drawdown_before_peak_72h", "DOUBLE"),
    ("time_to_peak_minutes", "BIGINT"),
    ("survived_24h", "BOOLEAN"),
    ("survived_7d", "BOOLEAN"),
    ("source", "VARCHAR"),
]

# Raw social counts at a fixed offset from the snapshot. One row per
# (snapshot_id, platform, offset), append-only.
#
# BUILD_BRIEF.md section 3 item 3: "Store raw counts, not derived scores -- derived
# formulas will change, raw won't." Author diversity, mention slope and speaker
# ratio are all formulas that will be revised during calibration; mentions_24h and
# unique_authors_24h will not. Deriving at read time keeps every past row usable
# after a formula change.
SOCIAL_OBSERVATION_COLUMNS: list[tuple[str, str]] = [
    ("observation_id", "VARCHAR"),
    ("snapshot_id", "VARCHAR"),
    ("platform", "VARCHAR"),
    ("ts", "BIGINT"),
    ("offset_minutes", "BIGINT"),
    ("handle", "VARCHAR"),
    ("exists", "BOOLEAN"),
    # X, raw counts only.
    ("mentions_window", "BIGINT"),
    ("window_minutes", "BIGINT"),
    ("unique_authors", "BIGINT"),
    ("follower_weighted_reach", "DOUBLE"),
    ("tier1_organic_engagements", "BIGINT"),
    ("replies", "BIGINT"),
    ("posts", "BIGINT"),
    # Telegram, raw counts only.
    ("members", "BIGINT"),
    ("online", "BIGINT"),
    ("messages_window", "BIGINT"),
    ("unique_speakers", "BIGINT"),
    ("source", "VARCHAR"),
    ("error", "VARCHAR"),
]

# One scored candidate. Hard rule 6: the score and the full input snapshot that
# produced it live in the same row, together with the prompt version, because a
# score without its inputs cannot be back-tested and prompts/score.md will be
# edited many times before Phase 2.
SCORE_COLUMNS: list[tuple[str, str]] = [
    ("score_id", "VARCHAR"),
    # One id per scoring run. A run is the unit a ranking is read from; a
    # millisecond timestamp is not, because two runs can share one.
    ("run_id", "VARCHAR"),
    ("snapshot_id", "VARCHAR"),
    ("scored_at_ms", "BIGINT"),
    ("prompt_version", "INTEGER"),
    ("weights_version", "VARCHAR"),
    ("model", "VARCHAR"),
    ("paper_mode", "BOOLEAN"),
    ("batch_regime", "VARCHAR"),
    ("score", "DOUBLE"),
    ("raw_score", "DOUBLE"),
    ("rank", "INTEGER"),
    ("excluded", "BOOLEAN"),
    ("rejected_by", "JSON"),
    ("indeterminate_on", "JSON"),
    ("pillar_scores", "JSON"),
    ("pillar_components", "JSON"),
    ("modifiers_applied", "JSON"),
    ("data_completeness", "DOUBLE"),
    ("thesis", "VARCHAR"),
    ("bear_case", "VARCHAR"),
    ("falsifier", "VARCHAR"),
    ("confidence", "VARCHAR"),
    ("data_gaps", "JSON"),
    ("narrative_source", "VARCHAR"),
    # The complete candidate packet the score was computed from.
    ("input_snapshot", "JSON"),
]

# Every re-price of a snapshotted token. Append-only: each observation is a new row,
# so the price path stays reconstructable and a label can always be recomputed from
# the evidence that produced it.
OUTCOME_OBSERVATION_COLUMNS: list[tuple[str, str]] = [
    ("observation_id", "VARCHAR"),
    ("snapshot_id", "VARCHAR"),
    ("ts", "BIGINT"),
    ("minutes_since_snapshot", "BIGINT"),
    ("mcap_usd", "DOUBLE"),
    ("price_usd", "DOUBLE"),
    ("liquidity_usd", "DOUBLE"),
    ("volume_24h_usd", "DOUBLE"),
    ("holder_count", "BIGINT"),
    ("source", "VARCHAR"),
]

# One safety lookup, from collectors/safety.py. Append-only and separate from the
# snapshot for the usual reason: it arrives after the snapshot was written, so
# putting it on that row would be an edit. It is also genuinely a time series --
# "the mint authority was live when we looked and revoked an hour later" is a real
# sequence of events, and a table that overwrote the first reading would lose it.
#
# Every field is nullable and null means *not established*, never *fine*. The hard
# filters treat unknown as excluding, so a gap here costs a token its place in the
# ranking rather than buying it one.
SAFETY_OBSERVATION_COLUMNS: list[tuple[str, str]] = [
    ("observation_id", "VARCHAR"),
    ("snapshot_id", "VARCHAR"),
    ("chain", "VARCHAR"),
    ("contract", "VARCHAR"),
    ("ts", "BIGINT"),
    ("source", "VARCHAR"),
    ("collected_at_ms", "BIGINT"),
    ("honeypot", "BOOLEAN"),
    ("sells_failing", "BOOLEAN"),
    ("buy_tax_pct", "DOUBLE"),
    ("sell_tax_pct", "DOUBLE"),
    ("mint_revoked", "BOOLEAN"),
    ("freeze_active", "BOOLEAN"),
    ("lp_burned", "BOOLEAN"),
    ("lp_locked_pct", "DOUBLE"),
    ("top10_ex_lp_pct", "DOUBLE"),
    ("holder_count", "BIGINT"),
    ("upgradeable", "BOOLEAN"),
    ("admin_renounced", "BOOLEAN"),
    ("deployer_address", "VARCHAR"),
    ("deployer_prior_rugs", "BIGINT"),
    ("rugged", "BOOLEAN"),
    ("risk_labels", "JSON"),
    ("error", "VARCHAR"),
]

# One row per token that has ever fired. Doubles as the "already triggered" index,
# which is why the watcher needs no mutable state to survive a restart.
TRIGGER_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("event_id", "VARCHAR"),
    ("ts", "BIGINT"),
    ("chain", "VARCHAR"),
    ("contract", "VARCHAR"),
    ("trigger_kind", "VARCHAR"),
    ("trigger_mcap_crossed", "BOOLEAN"),
    ("trigger_holders_crossed", "BOOLEAN"),
    ("mcap_usd_at_trigger", "DOUBLE"),
    ("holder_count_at_trigger", "BIGINT"),
    ("snapshot_id", "VARCHAR"),
    ("source", "VARCHAR"),
]
