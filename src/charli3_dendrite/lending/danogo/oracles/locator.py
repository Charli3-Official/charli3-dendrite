"""Observation-based oracle recipes: re-price a collateral from live leaves.

Danogo stores no on-chain price; a collateral's price is reproduced from a fixed
set of source-leaf UTxOs (each contributing a forward rate, optionally inverted,
scaled by a power of ten). A `Recipe` is that frozen, ordered description for one
collateral, learned from a historical price-gen tx and replayed live (read each
handle's current UTxO, then `forward.replay_recipe`). `LeafHandle` says *where* to
re-read a leaf; `RecipeStep` pairs a handle with its orientation/scale; the registry
the loader consults is keyed `f"{collateral_unit}|{quote_unit}"` -> `Recipe`.
`load_registry` reads the packaged `recipes.json`; the db-sync miner that fills it
lives in `tests/lending/danogo/fixtures/_capture_recipes.py`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

from pycardano import Address

if TYPE_CHECKING:
    from charli3_dendrite.lending.danogo.market import DanogoMarket

# The Danogo pool redemption leaf kind (an ``OracleUtxoType`` member name). Named as a
# literal here to keep the recipe assembler free of the oracle-redeemer import; it is
# the same string the mined recipes and `forward.leaf_rate` dispatch on.
_BOND_DTOKEN_OTYPE = "TDANOGO_POOL"
# ADA is the universal intermediate quote for cross-quote composition (mirrors
# `forward.INTERMEDIATE_QUOTE`; redeclared to avoid importing the forward module, which
# imports this one).
_INTERMEDIATE_QUOTE = "lovelace"
# On-chain unit = 28-byte policy id (56 hex chars) followed by the asset name (hex).
_POLICY_ID_HEX_LEN = 56


def _payment_cred_hex(address: str) -> str:
    """Payment-credential (script hash) hex of an enterprise/base address."""
    return Address.decode(address).payment_part.payload.hex()


@dataclass(frozen=True)
class LeafHandle:
    """Where to re-read a source leaf live: parser type + address + identifying NFT.

    `nft_policy`/`nft_name` (hex) identify the specific UTxO at `address` when the
    address holds several; both empty means "the only relevant UTxO at the address".
    `datum_key` is a further discriminator for kinds that share one marker token across
    many feeds (e.g. Liqwid V2 oracles): the stable asset-identity parsed from the leaf
    datum (see `forward.leaf_discriminator`). Empty when the NFT alone pins the UTxO.
    """

    otype: str
    address: str
    nft_policy: str
    nft_name: str
    datum_key: str = ""


@dataclass(frozen=True)
class RecipeStep:
    """One leaf's contribution: its handle, whether to invert, and its power-of-ten."""

    handle: LeafHandle
    is_reverse: bool
    scale_exp: int


@dataclass(frozen=True)
class Recipe:
    """Ordered steps whose composed product is one collateral's price."""

    steps: tuple[RecipeStep, ...]


def recipe_from_observation(leaves: Sequence[Mapping]) -> Recipe:
    """Build a `Recipe` from a captured case's ordered `leaves`.

    See ``forward_pricing.json``; each leaf mapping must carry `otype`, `address`,
    `nft_policy`, `nft_name`, `is_reverse`, `scale_exp` (and optionally `datum_key`).
    """
    steps = tuple(
        RecipeStep(
            handle=LeafHandle(
                otype=lf["otype"],
                address=lf["address"],
                nft_policy=lf["nft_policy"],
                nft_name=lf["nft_name"],
                datum_key=lf.get("datum_key", ""),
            ),
            is_reverse=bool(lf["is_reverse"]),
            scale_exp=int(lf["scale_exp"]),
        )
        for lf in leaves
    )
    return Recipe(steps=steps)


def registry_to_dict(registry: Mapping[str, Recipe]) -> dict:
    """Serialize `{unit: Recipe}` to a JSON-safe dict."""
    return {
        unit: {
            "steps": [
                {
                    "otype": s.handle.otype,
                    "address": s.handle.address,
                    "nft_policy": s.handle.nft_policy,
                    "nft_name": s.handle.nft_name,
                    **({"datum_key": s.handle.datum_key} if s.handle.datum_key else {}),
                    "is_reverse": s.is_reverse,
                    "scale_exp": s.scale_exp,
                }
                for s in recipe.steps
            ],
        }
        for unit, recipe in registry.items()
    }


def load_registry() -> dict[str, Recipe]:
    """Load the packaged ``{collateral|quote: Recipe}`` registry; ``{}`` if unavailable.

    The registry is package data (``recipes.json``) mined offline from historical
    price-gen transactions. Missing/unreadable data degrades to an empty registry so
    the loader prices nothing rather than crashing.
    """
    try:
        text = (
            files("charli3_dendrite.lending.danogo.oracles")
            .joinpath("recipes.json")
            .read_text()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        return registry_from_dict(json.loads(text))
    except (ValueError, KeyError, TypeError):
        return {}


def registry_from_dict(data: Mapping[str, Mapping]) -> dict[str, Recipe]:
    """Inverse of `registry_to_dict`."""
    out: dict[str, Recipe] = {}
    for unit, rec in data.items():
        steps = tuple(
            RecipeStep(
                handle=LeafHandle(
                    otype=s["otype"],
                    address=s["address"],
                    nft_policy=s["nft_policy"],
                    nft_name=s["nft_name"],
                    datum_key=s.get("datum_key", ""),
                ),
                is_reverse=bool(s["is_reverse"]),
                scale_exp=int(s["scale_exp"]),
            )
            for s in rec["steps"]
        )
        out[unit] = Recipe(steps=steps)
    return out


def _reverse_steps(steps: tuple[RecipeStep, ...]) -> tuple[RecipeStep, ...]:
    """Reverse a recipe's steps so the composed rate is inverted.

    Each step contributes ``rate**(±1) * 10**scale_exp``; the reciprocal of the whole
    product flips every step's orientation and negates its scale, in reverse order. The
    leaf handles are unchanged, so the reversed steps resolve to the same live UTxOs.
    """
    return tuple(
        RecipeStep(
            handle=s.handle,
            is_reverse=not s.is_reverse,
            scale_exp=-s.scale_exp,
        )
        for s in reversed(steps)
    )


def synthesize_bond_dtoken_recipe(
    collateral: str,
    quote: str,
    *,
    pool_address: str,
    config_pool_cred: str,
    markets: Mapping[str, DanogoMarket],
    registry: Mapping[str, Recipe],
) -> Recipe | None:
    """Assemble a live pricing `Recipe` for a Danogo bond-dToken collateral, or None.

    A bond dToken's on-chain unit is ``<pool script hash> + N`` where ``N`` is a live
    pool/market NFT name; the dToken's own pool redeems it into the market's supply
    token at ``total_supply / circulating_dtoken`` (the ``TDANOGO_POOL`` leaf, priced
    bit-exact by `reproduce`/`leaves`). This ASSEMBLES the recipe that reprices such a
    dToken into `quote`; it computes no rate -- `forward.replay_recipe` /
    `forward.compose_cross_quote` do, over the same leaf handles the mined recipes use.

    `collateral` is recognised as a bond dToken only when its policy id is the
    deployment's pool validator hash (derived from `pool_address`, resolved from
    on-chain config -- never hardcoded) AND its asset name is a known pool/market name
    (``markets`` keyed by ``N``). Requiring the policy, not the name alone, rejects an
    unrelated token that happens to reuse a pool/market name as its asset name, while
    staying deployment-agnostic (no hardcoded policy, no pool from another deployment).
    Four cases:

    * ``underlying == quote`` -- the single redemption step prices the dToken directly.
    * a mined ``underlying|quote`` recipe exists -- append the redemption to it.
    * ``quote == ADA`` (``underlying != ADA``) -- ``dToken -> underlying -> ADA``: the
      redemption times the reversed ``lovelace|underlying`` recipe, priced in ADA.
    * cross-quote (the registry has ``lovelace|underlying`` and ``lovelace|quote``) --
      ``dToken -> underlying -> ADA -> quote`` shaped for `compose_cross_quote`'s
      pool-leading branch (the redemption leads, the reversed ``lovelace|underlying``
      values it in ADA, and a trailing bridge step is dropped in favour of the resolved
      ``lovelace|quote`` intermediate).
    * otherwise None -- no fabricated price; the collateral stays unpriced
      (not-liquidatable), the existing safe degrade.
    """
    # A bond dToken is minted under the Danogo pool validator hash: its unit is
    # ``<pool script hash> + <pool/market name>``. Require BOTH the policy id (the pool
    # validator hash, from `pool_address`) and a known pool/market name, so an unrelated
    # token that merely reuses a pool/market name is not mistaken for a bond dToken.
    name = collateral[_POLICY_ID_HEX_LEN:]
    market = markets.get(name)
    if market is None or collateral[:_POLICY_ID_HEX_LEN] != _payment_cred_hex(
        pool_address,
    ):
        return None
    redemption_step = RecipeStep(
        handle=LeafHandle(
            otype=_BOND_DTOKEN_OTYPE,
            address=pool_address,
            nft_policy=config_pool_cred,
            nft_name=name,
            datum_key="",
        ),
        is_reverse=False,
        scale_exp=0,
    )
    underlying = market.supply_token
    if underlying == quote:
        return Recipe(steps=(redemption_step,))
    conv = registry.get(f"{underlying}|{quote}")
    if conv is not None:
        return Recipe(steps=(*conv.steps, redemption_step))

    # Compose through ADA: value the dToken in ADA via its redemption then the reversed
    # ADA->underlying recipe (ADA is the universal intermediate). No bridge -> unpriced.
    under_from_ada = registry.get(f"{_INTERMEDIATE_QUOTE}|{underlying}")
    if under_from_ada is None:
        return None
    underlying_to_ada = _reverse_steps(under_from_ada.steps)
    if quote == _INTERMEDIATE_QUOTE:
        # Target IS ADA: dToken -> underlying -> ADA, priced single-hop in ADA.
        steps = (redemption_step, *underlying_to_ada)
    elif (ada_to_quote := registry.get(f"{_INTERMEDIATE_QUOTE}|{quote}")) is not None:
        # Cross-quote pool-leading shape: (redemption, underlying->ADA, dropped bridge).
        # compose_cross_quote values the collateral in ADA over all-but-the-last steps,
        # and takes ADA->quote from the separately-resolved lovelace|quote intermediate,
        # dropping the trailing step -- it only has to resolve live; its rate is unused.
        steps = (redemption_step, *underlying_to_ada, ada_to_quote.steps[0])
    else:
        return None
    return Recipe(steps=steps)


def augment_registry_with_bond_dtokens(
    registry: dict[str, Recipe],
    *,
    pool_address: str,
    config_pool_cred: str,
    markets: Mapping[str, DanogoMarket],
) -> dict[str, Recipe]:
    """Add synthesized bond-dToken recipes to `registry` in place; return it.

    For every market's accepted collateral that is a bond dToken with no mined recipe
    for that market's quote, synthesize one (`synthesize_bond_dtoken_recipe`) and
    register it under ``f"{collateral}|{quote}"``. Additive: mined recipes are never
    overwritten, and a collateral with no composable path is left unpriced. Both live
    seams that consult the registry -- `loader.snapshot` and the tx-builder leaf
    resolution -- call this so they price bond dTokens identically.
    """
    for market in markets.values():
        quote = market.supply_token
        for collateral in market.collaterals:
            key = f"{collateral}|{quote}"
            if key in registry:
                continue
            recipe = synthesize_bond_dtoken_recipe(
                collateral,
                quote,
                pool_address=pool_address,
                config_pool_cred=config_pool_cred,
                markets=markets,
                registry=registry,
            )
            if recipe is not None:
                registry[key] = recipe
    return registry
