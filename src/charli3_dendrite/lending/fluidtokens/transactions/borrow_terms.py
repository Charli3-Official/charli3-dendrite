"""Pool-borrow terms shared by the FluidTokens transaction builders.

Everything here reads only the pool datum fields V3 and V4 have in common, the signed
oracle witness, and the chain's min-UTxO rule: the lender-bond address and datum
commitment, the chosen collateral unit and its floor, the borrow validity window, and
the min-ADA of a synthesized output.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from math import ceil
from typing import TYPE_CHECKING
from typing import Any

from pycardano import Address
from pycardano import TransactionOutput
from pycardano import min_lovelace

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.datums import CollateralAsset
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.transactions._common import (
    address_from_plutus,
)
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import asset_to_value

if TYPE_CHECKING:
    from collections.abc import Sequence

    from charli3_dendrite.lending.fluidtokens.oracles.witness import OracleReward

# The FluidTokens borrow validator caps the transaction's validity window at one hour
# (3600 slots), a distinct, wider bound than the 360-slot
# ``_common.LOAN_ACTION_VALIDITY_SLOTS`` the V3 loan-action validators use. The window
# additionally must fall inside the signed oracle witness's ms window.
BORROW_VALIDITY_SLOTS = 3600

# Plutus ``Bool`` True is constructor alternative 1.
_BOOL_TRUE = 1


def lender_bond_address(pool_datum: PoolDatum) -> Address:
    """Decode the pool's committed ``lender_bond_address`` to a pycardano `Address`."""
    raw = pool_datum.lender_bond_address
    data = raw.data if hasattr(raw, "data") else raw
    return address_from_plutus(data)


def lender_bond_datum_matches(pool_datum: PoolDatum, datum_hex: str) -> bool:
    """True if ``datum_hex`` hashes to the pool's committed lender-bond datum hash."""
    digest = hashlib.blake2b(bytes.fromhex(datum_hex), digest_size=32).digest()
    return digest == bytes(pool_datum.lender_bond_inline_datum_hash)


def collateral_unit(pool_datum: PoolDatum, chosen_collateral_index: int) -> str:
    """The unit (``policy_hex`` ++ ``name_hex``) of the chosen collateral option."""
    chosen = list(pool_datum.collateral_options)[chosen_collateral_index]
    if not isinstance(chosen, CollateralAsset):
        chosen = CollateralAsset.from_primitive(chosen)
    alt, fields = constr(chosen.maybe_asset_name)
    name = bytes(fields[0]) if alt == 0 and fields else b""
    return chosen.policy_id.hex() + name.hex()


def min_collateral_amount(
    pool_datum: PoolDatum,
    *,
    chosen_collateral_index: int,
    principal_amount: int,
    price_num: int,
    price_den: int,
) -> int:
    """Minimum collateral units required to back ``principal_amount`` (ADA principal).

    Mirrors the pool validator's collateral floor. For a dynamically-priced pool the
    principal is first expressed in lovelace (1:1 for an ADA principal), scaled by the
    option's ``min_collateral_divider / min_collateral`` ratio, and converted to
    collateral units at the oracle price ``price_num / price_den``
    (``ceil(collateral_lovelace * price_den / price_num)``). For a statically-priced
    pool the floor is ``ceil(principal_amount * min_collateral / min_collateral_div)``.
    Non-ADA principal pools would additionally price the principal via their own oracle
    and are out of scope.
    """
    principal_asset = pool_datum.common_data.principal_asset
    if principal_asset.policy_id or principal_asset.asset_name:
        raise NotImplementedError(
            "min-collateral for a non-ADA principal pool needs the principal oracle "
            "price; only ADA-principal pools are supported",
        )
    min_collateral = int(list(pool_datum.min_collateral)[chosen_collateral_index])
    divider = int(list(pool_datum.min_collateral_divider)[chosen_collateral_index])
    dynamic_alt, _ = constr(pool_datum.dynamic_collateral_price)
    if dynamic_alt == _BOOL_TRUE:
        collateral_in_lovelace = Fraction(principal_amount * divider, min_collateral)
        return ceil(collateral_in_lovelace * Fraction(price_den, price_num))
    return ceil(Fraction(principal_amount * min_collateral, divider))


def borrow_window(
    oracle: OracleReward,
    *,
    valid_from: int | None,
    valid_to: int | None,
    tip: int,
) -> tuple[int, int]:
    """The borrow's ``(valid_from, valid_to)`` slot window, covered by the witness.

    :func:`signed_window` for one witness under the one-hour borrow cap.
    """
    return signed_window(
        [oracle],
        valid_from=valid_from,
        valid_to=valid_to,
        tip=tip,
        cap=BORROW_VALIDITY_SLOTS,
    )


def signed_window(
    oracles: Sequence[OracleReward],
    *,
    valid_from: int | None,
    valid_to: int | None,
    tip: int,
    cap: int,
) -> tuple[int, int]:
    """A ``(valid_from, valid_to)`` slot window every signed price covers.

    A fully pinned window (byte-exact replay) is accepted only if it spans at most
    ``cap`` slots AND every witness covers it. Otherwise the window starts at the
    backend ``tip`` (or the latest witness start) and ends ``cap`` slots later or at
    the earliest witness end; a window no witness set can cover raises (a witness is
    stale relative to the tip). Without witnesses the window is ``cap`` slots from
    the tip.
    """
    if valid_from is not None and valid_to is not None:
        if valid_to - valid_from > cap:
            raise ValueError(f"validity window exceeds {cap} slots")
        if not all(o.contains_slot_window(valid_from, valid_to) for o in oracles):
            raise ValueError(
                "pinned validity window is not covered by the oracle witness",
            )
        return valid_from, valid_to
    covered = [o.tx_validity_slots() for o in oracles]
    lower = (
        valid_from if valid_from is not None else max([tip, *(f for f, _ in covered)])
    )
    upper = (
        valid_to
        if valid_to is not None
        else min([lower + cap, *(t for _, t in covered)])
    )
    if upper - lower > cap:
        upper = lower + cap
    if upper <= lower or not all(o.contains_slot_window(lower, upper) for o in oracles):
        raise ValueError(
            "could not fit a validity window inside the oracle witness; the witness "
            "may be stale relative to the backend tip",
        )
    return lower, upper


def min_output_lovelace(
    *,
    address: str,
    assets: dict[str, int],
    datum: Any,  # noqa: ANN401 - PlutusData, RawCBOR or None
    slot: int,
) -> int:
    """The protocol min-ADA of an output with ``assets`` and ``datum`` at ``address``.

    ``assets`` maps unit to quantity and excludes lovelace; the output is sized with a
    placeholder coin, which ``min_lovelace`` does not depend on beyond its encoding.
    """
    output = TransactionOutput(
        Address.decode(address),
        asset_to_value(Assets(**{"lovelace": OUTPUT_MIN_ADA, **assets})),
        datum=datum,
    )
    return min_lovelace(EvalContext(last_block_slot=slot), output=output)
