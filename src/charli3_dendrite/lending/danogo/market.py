"""Typed view over the Danogo Market Param datum."""

from __future__ import annotations

from collections.abc import Sequence

from pycardano import RawPlutusData

from charli3_dendrite.dataclasses.models import DendriteBaseModel
from charli3_dendrite.lending.danogo.datums import asset_unit

# Plutus `Bool` derives False=Constr0 (tag 121), True=Constr1 (tag 122).
_BOOL_TRUE_CONSTR = 122


def _unit(pair: Sequence[bytes]) -> str:
    """('', '')/empty -> 'lovelace'; else policy.hex()+name.hex().

    Keys decode as `(policy, name)` tuples and the supply token as a `[policy, name]`
    list, so accept either and coerce non-bytes (defensive) to empty.
    """
    policy = pair[0] if len(pair) > 0 and isinstance(pair[0], bytes) else b""
    name = pair[1] if len(pair) > 1 and isinstance(pair[1], bytes) else b""
    return asset_unit(policy, name)


class DanogoMarket(DendriteBaseModel):
    """Plain-Python view of a market's parameters."""

    supply_token: str
    collaterals: dict[str, int]  # unit -> liquidation threshold (bps)
    alt_supply_tokens: dict[str, bool]  # unit -> allow_supply
    base_rate: int
    power_base: int
    util_cap: int
    loan_fee_rate: int
    loan_origination_fee_rate: int
    min_tx_amount: int

    def threshold_for(self, unit: str) -> int:
        """Liquidation threshold (bps) for a collateral unit, 0 if not accepted."""
        return self.collaterals.get(unit, 0)

    @classmethod
    def from_market_datum(cls, datum_cbor: str | bytes) -> DanogoMarket:
        """Parse the Market Param datum CBOR into a `DanogoMarket`.

        The datum is `Constr0` with 14 fields.

        BYTE-CONFIRMED against on-chain CBOR (structure decoded directly):
        ``[0]`` collaterals `Map{(policy,name) -> [threshold_bps, flag]}`,
        ``[9]`` supply token `[policy, name]`, and ``[10]`` alt-supply
        `Map{(policy,name) -> Bool}`.

        INFERRED (not yet byte-confirmed):
        ``[1..3]`` interest-rate curve params (base rate, power base, utilization
        cap), ``[4]`` loan origination fee rate, ``[5]`` loan fee rate. ``[11]`` is
        the minimum transaction amount, pinned by validator acceptance rather than a
        decoded byte layout: the topup/withdraw and create-loan scripts reject
        ``abs(pool_changed_amount) < fields[11]`` (the borrow likewise), which is how
        the index was placed. The remaining scalars (``[6..7]``, ``[12..13]``) are
        fee/limit params whose exact semantics are not used here; surfaced
        best-effort.
        """
        fields = RawPlutusData.from_cbor(datum_cbor).data.value

        collaterals: dict[str, int] = {}
        for key, val in fields[0].items():
            # The threshold is the head of the inner `[threshold_bps, flag]` array.
            # Plutus arrays decode to a `Sequence` whose concrete type varies by CBOR
            # backend (a plain `list`, or an indefinite-length `FrozenList`), so match
            # on `Sequence` rather than the `list`/`tuple` concrete types.
            is_seq = isinstance(val, Sequence) and not isinstance(
                val,
                (str, bytes, bytearray),
            )
            threshold = val[0] if is_seq and val else 0
            collaterals[_unit(key)] = int(threshold)

        # allow_supply is the Plutus `Bool` True variant; any unexpected encoding
        # defaults to False.
        alt_supply_tokens: dict[str, bool] = {}
        for key, val in fields[10].items():
            tag = getattr(val, "tag", 121)
            alt_supply_tokens[_unit(key)] = tag == _BOOL_TRUE_CONSTR

        return cls(
            supply_token=_unit(fields[9]),
            collaterals=collaterals,
            alt_supply_tokens=alt_supply_tokens,
            base_rate=int(fields[1]),
            power_base=int(fields[2]),
            util_cap=int(fields[3]),
            loan_origination_fee_rate=int(fields[4]),
            loan_fee_rate=int(fields[5]),
            min_tx_amount=int(fields[11]),  # [11] min tx amount
        )
