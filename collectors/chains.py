"""Canonical chain identities.

Three vocabularies have to line up and did not before this module existed:

* **The brief's.** BUILD_BRIEF.md section 4 names the target chains
  ``solana | bnb | robinhood``.
* **DexScreener's.** Its ``chainId`` for BNB Chain is ``bsc``, not ``bnb``.
* **The filters'.** ``filters/hard_filters.py`` needs to know whether a chain has
  EVM proxies at all, which is a property of the chain, not of its name.

Writing whichever string the source happened to return into the ``chain`` column
would make the column unusable as a filter -- ``bnb`` and ``bsc`` rows would be two
different chains to every ``GROUP BY`` and every dropdown. So every source
normalises through :func:`canonical` on the way in, and the stored value is always
a name from :data:`CHAINS`.

An unknown chain is *not* dropped and *not* renamed to something plausible. It is
slugged and kept, with ``known=False``, because a token on a chain this registry
has not heard of is real data about a real token; silently discarding it would be
the same class of mistake as imputing a missing field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Chain:
    """One chain, and the three things the rest of the repo asks about it."""

    name: str  # the canonical value written to the `chain` column
    label: str  # for display
    evm: bool  # does the EVM proxy/upgradeability question even apply
    dexscreener_id: str | None  # DexScreener's `chainId`, None if not covered
    native_symbol: str | None = None
    known: bool = True

    @property
    def has_dexscreener_source(self) -> bool:
        return self.dexscreener_id is not None


# The registry. `name` is what lands in the database; everything else is lookup.
#
# Solana and BNB are the brief's first two targets. The rest are here because
# DexScreener covers them with the identical response shape, so supporting them
# costs one row each -- and because a screener that can only see one chain cannot
# answer "is this meta running everywhere or just here".
#
# Robinhood Chain is the brief's third target and has no DexScreener id yet, so it
# is registered with `dexscreener_id=None`: a named chain with no source is an
# honest gap, and `--chains robinhood` fails loudly rather than collecting nothing.
CHAINS: Final[tuple[Chain, ...]] = (
    Chain("solana", "Solana", evm=False, dexscreener_id="solana", native_symbol="SOL"),
    Chain("bnb", "BNB Chain", evm=True, dexscreener_id="bsc", native_symbol="BNB"),
    Chain("ethereum", "Ethereum", evm=True, dexscreener_id="ethereum", native_symbol="ETH"),
    Chain("base", "Base", evm=True, dexscreener_id="base", native_symbol="ETH"),
    Chain("arbitrum", "Arbitrum", evm=True, dexscreener_id="arbitrum", native_symbol="ETH"),
    Chain("polygon", "Polygon", evm=True, dexscreener_id="polygon", native_symbol="POL"),
    Chain("avalanche", "Avalanche", evm=True, dexscreener_id="avalanche", native_symbol="AVAX"),
    Chain("optimism", "Optimism", evm=True, dexscreener_id="optimism", native_symbol="ETH"),
    Chain("blast", "Blast", evm=True, dexscreener_id="blast", native_symbol="ETH"),
    Chain("sui", "Sui", evm=False, dexscreener_id="sui", native_symbol="SUI"),
    Chain("ton", "TON", evm=False, dexscreener_id="ton", native_symbol="TON"),
    Chain("tron", "Tron", evm=False, dexscreener_id="tron", native_symbol="TRX"),
    Chain("robinhood", "Robinhood Chain", evm=True, dexscreener_id=None),
)

DEFAULT_CHAINS: Final[tuple[str, ...]] = ("solana", "bnb", "base", "ethereum")

_BY_NAME: Final[dict[str, Chain]] = {c.name: c for c in CHAINS}
_BY_DEXSCREENER: Final[dict[str, Chain]] = {
    c.dexscreener_id: c for c in CHAINS if c.dexscreener_id
}

# Spellings seen in the wild that mean a chain already in the registry. Kept
# explicit rather than fuzzy-matched: a near-miss that silently resolves to the
# wrong chain is worse than one that comes back unknown.
_ALIASES: Final[dict[str, str]] = {
    "bsc": "bnb",
    "binance": "bnb",
    "binance-smart-chain": "bnb",
    "bnbchain": "bnb",
    "bnb-chain": "bnb",
    "sol": "solana",
    "eth": "ethereum",
    "mainnet": "ethereum",
    "matic": "polygon",
    "avax": "avalanche",
    "arb": "arbitrum",
    "op": "optimism",
    "robinhood-chain": "robinhood",
}

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.strip().lower()).strip("-")


def canonical(value: str | None) -> str | None:
    """Normalise any spelling of a chain to its canonical name.

    ``None`` in, ``None`` out -- an unreported chain is missing data, and this is
    not the place to guess Solana just because most rows are Solana.

    An unrecognised chain is slugged and returned as-is, so it stays queryable and
    stays visibly distinct from the chains this repo actually understands.
    """
    if value is None:
        return None
    slug = _slug(value)
    if not slug:
        return None
    if slug in _ALIASES:
        return _ALIASES[slug]
    if slug in _BY_NAME:
        return slug
    if slug in _BY_DEXSCREENER:
        return _BY_DEXSCREENER[slug].name
    return slug


def get(value: str | None) -> Chain | None:
    """The registry entry for a chain, or an ``known=False`` placeholder for one
    this repo has never heard of. ``None`` only for a missing chain."""
    name = canonical(value)
    if name is None:
        return None
    known = _BY_NAME.get(name)
    if known is not None:
        return known
    # Unknown chain: EVM-ness is genuinely unknown, and `evm=False` here would
    # quietly hand it a free pass through the proxy filter. `known=False` is what
    # check_proxy_risk keys on so it answers "unknown" instead.
    return Chain(name, name, evm=False, dexscreener_id=None, known=False)


def label(value: str | None) -> str | None:
    chain = get(value)
    return chain.label if chain else None


def is_evm(value: str | None) -> bool | None:
    """``True``/``False`` for a registered chain, ``None`` when it cannot be answered.

    ``None`` is load-bearing: ``check_proxy_risk`` must not pass a token just
    because the registry has never seen its chain.
    """
    chain = get(value)
    if chain is None or not chain.known:
        return None
    return chain.evm


def dexscreener_id(value: str | None) -> str | None:
    chain = get(value)
    return chain.dexscreener_id if chain else None


def from_dexscreener(chain_id: str | None) -> str | None:
    """Canonical name for a DexScreener ``chainId``."""
    return canonical(chain_id)


def resolve_requested(names: list[str] | tuple[str, ...]) -> list[str]:
    """Canonicalise a user-supplied chain list, rejecting ones with no source.

    Raises rather than skipping. ``--chains solana,robinhood`` quietly collecting
    only Solana would look identical to a quiet day on Robinhood Chain, and the
    difference matters to anyone reading the resulting counts.
    """
    resolved: list[str] = []
    for raw in names:
        name = canonical(raw)
        if name is None:
            raise ValueError(f"empty chain name in {names!r}")
        chain = get(name)
        assert chain is not None
        if not chain.has_dexscreener_source:
            supported = ", ".join(c.name for c in CHAINS if c.has_dexscreener_source)
            raise ValueError(
                f"chain {raw!r} has no DexScreener source "
                f"({'not in the registry' if not chain.known else 'registered but uncovered'}). "
                f"Supported: {supported}"
            )
        if name not in resolved:
            resolved.append(name)
    return resolved


def supported_names() -> list[str]:
    return [c.name for c in CHAINS if c.has_dexscreener_source]
