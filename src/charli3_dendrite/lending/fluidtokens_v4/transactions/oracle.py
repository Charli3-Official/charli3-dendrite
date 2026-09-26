"""Signed oracle prices for the V4 builders that re-price collateral.

A validator that needs a price reads a feed reference input and finds its signed
price as the withdraw redeemer of the feed's own payment credential (one oracle
validator per priced token). ADA is priced 1:1 without a feed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import TransactionBuilder

from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens.datums import Asset
    from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
        OracleWitnessBundle,
    )

# A collateral option without an oracle names this token.
NO_ORACLE_TOKEN = (b"NONE", b"NONE")


@dataclass
class OracleWitness:
    """A signed price: its feed UTxO, its oracle script and the signed redeemer."""

    feed: Utxo
    script_ref: Utxo
    reward_cbor: str

    @property
    def script_hash(self) -> str:
        """The oracle withdraw script hash (hex)."""
        return script_hash_of(self.script_ref)

    @property
    def reward(self) -> OracleReward:
        """The decoded signed price and window."""
        return OracleReward.parse(self.reward_cbor)

    @property
    def price(self) -> Fraction:
        """Lovelace per smallest unit of the priced token."""
        reward = self.reward
        return Fraction(reward.price_num, reward.price_den)

    def serves(self, oracle_token: Asset) -> bool:
        """True if the feed holds ``oracle_token``."""
        return self.feed.holds(
            oracle_token.policy_id.hex(),
            oracle_token.asset_name.hex(),
            1,
        )

    @classmethod
    def from_bundle(
        cls,
        backend: AbstractBackend,
        bundle: OracleWitnessBundle,
    ) -> OracleWitness:
        """Resolve a registry witness bundle's out-refs into UTxOs."""
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )

        return cls(
            feed=resolve_utxo_by_outref(backend, *bundle.oracle_feed_outref),
            script_ref=resolve_utxo_by_outref(
                backend,
                *bundle.oracle_script_ref_outref,
            ),
            reward_cbor=bundle.oracle_reward_cbor,
        )


def has_oracle(oracle_token: Asset) -> bool:
    """False for the no-oracle marker token."""
    return (oracle_token.policy_id, oracle_token.asset_name) != NO_ORACLE_TOKEN


def witness_for(oracles: Sequence[OracleWitness], oracle_token: Asset) -> OracleWitness:
    """The witness whose feed holds ``oracle_token``."""
    for oracle in oracles:
        if oracle.serves(oracle_token):
            return oracle
    raise ValueError(
        "no oracle witness holds token "
        f"{oracle_token.policy_id.hex()}.{oracle_token.asset_name.hex()}",
    )


def add_oracles(
    tx_builder: TransactionBuilder,
    oracles: Sequence[OracleWitness],
) -> None:
    """Reference each feed and replay each signed price as its oracle's withdraw."""
    unique = {oracle.script_hash: oracle for oracle in oracles}
    for oracle in unique.values():
        tx_builder.reference_inputs.add(to_pycardano_utxo(oracle.feed))
        tx_builder.add_withdrawal_script(
            to_pycardano_utxo(oracle.script_ref),
            Redeemer(RawPlutusData.from_cbor(oracle.reward_cbor)),
        )
    add_zero_withdrawals(tx_builder, list(unique))


def oracles_from_capture(
    fix: dict,
    refs: list[Utxo],
    *,
    exclude: set[str],
) -> list[OracleWitness]:
    """The oracle witnesses of a captured transaction.

    Every withdraw redeemer whose script is not in ``exclude`` is an oracle's signed
    price; its feed is the reference input at that script without a reference script.
    """
    oracles = []
    for red in fix["redeemers"]:
        if red["purpose"] != "reward" or red["script_hash"] in exclude:
            continue
        feed = next(
            u
            for u in refs
            if not u.ref_script
            and Address.decode(u.address).payment_part.payload.hex()
            == red["script_hash"]
        )
        oracles.append(
            OracleWitness(
                feed=feed,
                script_ref=script_ref_by_hash(refs, red["script_hash"]),
                reward_cbor=red["cbor"],
            ),
        )
    return oracles
