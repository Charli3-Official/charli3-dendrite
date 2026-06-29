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
