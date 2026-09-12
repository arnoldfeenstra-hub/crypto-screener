"""Canonical chain identities.

Three vocabularies have to line up and did not before this module existed:

* **The brief's.** BUILD_BRIEF.md section 4 names the target chains
  ``solana | bnb | robinhood``.
* **DexScreener's.** Its ``chainId`` for BNB Chain is ``bsc``, not ``bnb``.
* **The filters'.** ``filters/hard_filters.py`` needs to know whether a chain has
  EVM proxies at all, which is a property of the chain, not of its name.

A chain's DexScreener id can also be supplied at runtime through
``SCREENER_CHAIN_IDS`` (``"robinhood=someid,foo=bar"``). That exists for the case
this module cannot otherwise handle honestly: a chain that is real and named in the
brief, but whose id nobody here has observed. Configuration is an assertion by the
operator; a hardcoded guess would be an assertion by this file, and this file does
not know.

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

import os
import re
from dataclasses import dataclass, replace
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
# Robinhood Chain is the brief's third target. It is an Arbitrum Orbit rollup, so
# `evm=True` is a property of the chain and is safe to assert; what is NOT safe to
# assert is its DexScreener `chainId`, because nobody here has seen DexScreener
# return one. Guessing a string would produce the worst outcome available: a
# request that quietly matches nothing, indistinguishable from a quiet day on the
# chain.
#
# So it ships with `dexscreener_id=None` and two ways to switch it on the moment
# the id is known, neither of which needs a code change:
#
#   1. `python -m collectors.dexscreener --discover-chains` prints every chainId
#      the live discovery endpoints actually return, flagged known/unknown. That
#      is how you find the id.
#   2. `SCREENER_CHAIN_IDS="robinhood=<that id>"` binds it. `--chains robinhood`
#      then collects exactly like any other chain -- same trigger, same filters,
#      same mindshare universe.
#
# Until then `--chains robinhood` fails by name. That is the deliberate choice:
# a silent skip and a quiet chain look identical in the counts afterwards.
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

# Env override, read once at import: "name=dexscreener_id,name=dexscreener_id".
# Exists so a chain whose id was unknown when this file was written -- Robinhood
# Chain today -- can be switched on by configuration rather than by a release.
CHAIN_ID_ENV = "SCREENER_CHAIN_IDS"

DEFAULT_CHAINS: Final[tuple[str, ...]] = ("solana", "bnb", "base", "ethereum")

def _overrides(raw: str | None = None) -> dict[str, str]:
    """Parse ``SCREENER_CHAIN_IDS``. A malformed entry is skipped, not guessed at."""
    text = raw if raw is not None else os.environ.get(CHAIN_ID_ENV, "")
    out: dict[str, str] = {}
    for pair in text.split(","):
        name, _, chain_id = pair.partition("=")
        name, chain_id = name.strip().lower(), chain_id.strip()
        if name and chain_id:
            out[name] = chain_id
    return out


def _apply_overrides(chains: tuple[Chain, ...], overrides: dict[str, str]) -> tuple[Chain, ...]:
    if not overrides:
        return chains
    known = {c.name for c in chains}
    updated = tuple(
        replace(c, dexscreener_id=overrides[c.name]) if c.name in overrides else c
        for c in chains
    )
    # A name the registry has never seen is still honoured: it is configuration,
    # explicitly supplied, not a value inferred from a response.
    extra = tuple(
        Chain(name, name, evm=False, dexscreener_id=chain_id, known=False)
        for name, chain_id in overrides.items()
        if name not in known
    )
    return updated + extra


_ACTIVE: tuple[Chain, ...] = _apply_overrides(CHAINS, _overrides())

_BY_NAME: Final[dict[str, Chain]] = {c.name: c for c in _ACTIVE}
_BY_DEXSCREENER: Final[dict[str, Chain]] = {
    c.dexscreener_id: c for c in _ACTIVE if c.dexscreener_id
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
            supported = ", ".join(supported_names())
            raise ValueError(
                f"chain {raw!r} has no DexScreener source "
                f"({'not in the registry' if not chain.known else 'registered but uncovered'}). "
                f"Set {CHAIN_ID_ENV}=\"{name}=<dexscreener chainId>\" once you know the id "
                "(find it with: python -m collectors.dexscreener --discover-chains). "
                f"Supported now: {supported}"
            )
        if name not in resolved:
            resolved.append(name)
    return resolved


def supported_names() -> list[str]:
    return [c.name for c in _ACTIVE if c.has_dexscreener_source]


def registry() -> tuple[Chain, ...]:
    """The registry as it stands, with any ``SCREENER_CHAIN_IDS`` overrides applied."""
    return _ACTIVE


def reload_overrides(raw: str | None = None) -> tuple[Chain, ...]:
    """Re-read ``SCREENER_CHAIN_IDS``. For tests, and for a long-lived process.

    Rebinds the lookup tables in place so ``canonical`` and ``resolve_requested``
    see the change; there is no second copy of the registry to fall out of step.
    """
    global _ACTIVE
    _ACTIVE = _apply_overrides(CHAINS, _overrides(raw))
    _BY_NAME.clear()
    _BY_NAME.update({c.name: c for c in _ACTIVE})
    _BY_DEXSCREENER.clear()
    _BY_DEXSCREENER.update({c.dexscreener_id: c for c in _ACTIVE if c.dexscreener_id})
    return _ACTIVE
