"""FluidTokens V4 pool borrow: one or several pools in one transaction.

Each borrowed pool ("leg") is spent with an empty redeemer and continued at the same
address with the same datum and its principal reduced; a loan UTxO is created at the
loan spend script under the borrower's stake credential; and the loan NFT, the lender
bond and the borrower bond are minted, all named after the hash of the spent pool
out-ref. One pool dispatch withdraw and one borrow-action withdraw (one entry per
leg) carry the checks, and each oracle-priced collateral adds its signed oracle
withdraw.

The pool validator reads the continuing pool of the i-th spent pool from output ``i``,
so a borrow's outputs come first in the transaction: continuing pools, loans, borrower
bonds, lender bonds, all in pool-input order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Asset
from pycardano import AssetName
from pycardano import MultiAsset
from pycardano import RawCBOR
from pycardano import RawPlutusData
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import TransactionOutput
from pycardano import VerificationKeyHash

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.lending.fluidtokens.datums import TxOutRef
from charli3_dendrite.lending.fluidtokens.transactions._common import (
    address_from_plutus,
)
from charli3_dendrite.lending.fluidtokens.transactions._common import plutus_address
from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    BORROW_VALIDITY_SLOTS,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    lender_bond_address,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_collateral_amount,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import (
    min_output_lovelace,
)
from charli3_dendrite.lending.fluidtokens.transactions.borrow_terms import signed_window
from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
    synth_loan_datum,
)
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import loan_id_from_out_ref
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSignature
from charli3_dendrite.lending.fluidtokens_v4.datums import AuthCardanoSpendScript
from charli3_dendrite.lending.fluidtokens_v4.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LockedBorrowerManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens_v4.state import collateral_asset_unit
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import is_policy_wide
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import loan_address
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import script_hash_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    withdraw_redeemer_position,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.lender_bond import (
    lender_bond_datum,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.loan_terms import with_fields
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import OracleWitness
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import add_oracles
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import has_oracle
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import (
    oracles_from_capture,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.oracle import witness_for
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    BondMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BoolTrue
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import BorrowData
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanMintRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolActionBorrow,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolBorrowActionWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    PoolWithdrawRedeemer,
)
from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.lending.units import constr
from charli3_dendrite.utility import asset_to_value
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from pycardano import PlutusData

    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
        FluidTokensProviderClient,
    )

# An ADA principal is priced 1:1 without reading its oracle reference input, and a
# permissionless pool ignores the permissioned-condition withdraw index; FluidTokens'
# own transactions carry these placeholder values.
PRINCIPAL_ORACLE_PLACEHOLDER = 0
PERMISSIONLESS_WITHDRAW_PLACEHOLDER = 1

_BOOL_TRUE = 1
_PERMISSIONLESS = b"NONE"

# The widest a loan's counters and collateral can encode: ``repaid_installments`` and
# ``done_recasts`` far beyond any real count (a 5-byte CBOR integer), and the largest
# quantity a value holds (a 9-byte CBOR integer).
_WIDEST_COUNT = 2**32 - 1
_WIDEST_QUANTITY = 2**63 - 1


@dataclass
class BorrowLeg:
    """One pool of a borrow and the outputs it produces."""

    pool: Utxo
    principal_amount: int
    chosen_collateral_index: int
    collateral_amount: int
    lender_bond_datum: str
    loan_lovelace: int
    lender_bond_lovelace: int
    borrower_bond_lovelace: int

    @property
    def out_ref(self) -> tuple[str, int]:
        """The spent pool out-ref."""
        if self.pool.out_ref is None:
            raise ValueError("pool UTxO is missing its out-ref")
        return self.pool.out_ref

    @property
    def pool_datum(self) -> PoolDatum:
        """The spent pool's datum."""
        if self.pool.datum is None:
            raise ValueError("pool UTxO is missing its datum")
        return PoolDatum.from_cbor(self.pool.datum)

    @property
    def pool_id(self) -> bytes:
        """The pool NFT asset name."""
        return pool_nft_name(self.pool)

    @property
    def loan_id(self) -> bytes:
        """The loan / bond asset name: the hash of the spent pool out-ref."""
        return loan_id_from_out_ref(self.out_ref)

    @property
    def collateral_unit(self) -> str:
        """The unit of the chosen collateral option."""
        return option_unit(self.pool_datum, self.chosen_collateral_index)

    @property
    def is_oracle_priced(self) -> bool:
        """True if the pool re-prices the collateral through its oracle."""
        return constr(self.pool_datum.dynamic_collateral_price)[0] == _BOOL_TRUE


def option_unit(pool_datum: PoolDatum, index: int) -> str:
    """The unit of the pool's ``index``-th collateral option ("lovelace" for ADA)."""
    return collateral_asset_unit(list(pool_datum.collateral_options)[index])


def pool_nft_name(pool: Utxo) -> bytes:
    """The name of the one pool NFT a pool UTxO holds."""
    names = [n for p, n, q in pool.assets if p == c.POOL_POLICY and q == 1]
    if len(names) != 1:
        raise ValueError("pool UTxO must hold exactly one pool NFT")
    return bytes.fromhex(names[0])


@dataclass(frozen=True)
class PoolBorrow:
    """A borrow from one pool: how much, against which collateral option.

    ``collateral_amount`` defaults to the least the pool accepts.
    """

    pool_out_ref: tuple[str, int]
    principal_amount: int
    chosen_collateral_index: int = 0
    collateral_amount: int | None = None


@dataclass
class BorrowSnapshot(PoolActionSnapshot):
    """Everything a borrow from one or several pools needs, resolved from chain."""

    legs: list[BorrowLeg]
    funding: list[Utxo]
    config: Utxo
    pool_spend_script_ref: Utxo
    pool_policy_script_ref: Utxo
    borrow_action_script_ref: Utxo
    loan_policy_script_ref: Utxo
    lender_bond_policy_script_ref: Utxo
    borrower_bond_policy_script_ref: Utxo
    borrower_address: str
    valid_from: int
    valid_to: int
    oracles: list[OracleWitness] = field(default_factory=list)

    @property
    def ordered_legs(self) -> list[BorrowLeg]:
        """The legs in the order the ledger sorts their pool inputs."""
        return sorted(
            self.legs,
            key=lambda leg: (bytes.fromhex(leg.out_ref[0]), leg.out_ref[1]),
        )

    def oracle_for(self, leg: BorrowLeg) -> OracleWitness | None:
        """The witness pricing the leg's collateral (None if the pool reads none)."""
        if not leg.is_oracle_priced:
            return None
        option = list(leg.pool_datum.collateral_options)[leg.chosen_collateral_index]
        return witness_for(self.oracles, option.oracle_token_asset)

    @classmethod
    def from_capture(cls, fix: dict) -> BorrowSnapshot:
        """Rebuild the snapshot of a captured mainnet borrow (for byte-exact replay)."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        outputs = [utxo_from_dict(u) for u in fix["outputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        pools = ledger_order(
            u for u in inputs if u.holds_policy(c.POOL_POLICY) and u.datum
        )
        action = next(
            PoolBorrowActionWithdrawRedeemer.from_cbor(r["cbor"])
            for r in fix["redeemers"]
            if r["script_hash"] == c.POOL_BORROW_ACTION_SKH
        )
        legs = []
        for pool, data in zip(pools, action.actions_for_each_input):
            loan_id = loan_id_from_out_ref(out_ref_of(pool)).hex()
            loan = next(u for u in outputs if u.holds(c.LOAN_POLICY, loan_id))
            lender_bond = next(
                u for u in outputs if u.holds(c.LENDER_BOND_POLICY, loan_id)
            )
            borrower_bond = next(
                u for u in outputs if u.holds(c.BORROWER_BOND_POLICY, loan_id)
            )
            unit = option_unit(
                PoolDatum.from_cbor(pool.datum),
                data.chosen_collateral_index,
            )
            legs.append(
                BorrowLeg(
                    pool=pool,
                    principal_amount=data.wanted_principal_amount,
                    chosen_collateral_index=data.chosen_collateral_index,
                    collateral_amount=next(
                        q for p, n, q in loan.assets if p + n == unit
                    ),
                    lender_bond_datum=lender_bond.datum or "",
                    loan_lovelace=loan.lovelace,
                    lender_bond_lovelace=lender_bond.lovelace,
                    borrower_bond_lovelace=borrower_bond.lovelace,
                ),
            )
        borrower = action.actions_for_each_input[0].borrower_address
        if isinstance(borrower, RawPlutusData):
            borrower = borrower.data
        return cls(
            legs=legs,
            funding=[u for u in inputs if u not in pools],
            config=next(
                u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
            ),
            pool_spend_script_ref=script_ref_by_hash(refs, c.POOL_SPEND_SKH),
            pool_policy_script_ref=script_ref_by_hash(refs, c.POOL_POLICY),
            borrow_action_script_ref=script_ref_by_hash(refs, c.POOL_BORROW_ACTION_SKH),
            loan_policy_script_ref=script_ref_by_hash(refs, c.LOAN_POLICY),
            lender_bond_policy_script_ref=script_ref_by_hash(
                refs,
                c.LENDER_BOND_POLICY,
            ),
            borrower_bond_policy_script_ref=script_ref_by_hash(
                refs,
                c.BORROWER_BOND_POLICY,
            ),
            borrower_address=str(address_from_plutus(borrower)),
            valid_from=fix["invalid_before"],
            valid_to=fix["invalid_hereafter"],
            oracles=oracles_from_capture(
                fix,
                refs,
                exclude={c.POOL_POLICY, c.POOL_BORROW_ACTION_SKH},
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        borrows: Sequence[PoolBorrow],
        borrower_address: str,
        oracles: Sequence[OracleWitness] | None = None,
        provider: FluidTokensProviderClient | None = None,
        funding: Sequence[Utxo] | None = None,
        valid_from: int | None = None,
        valid_to: int | None = None,
        allow_spent: bool = False,
    ) -> BorrowSnapshot:
        """Resolve a borrow from each of ``borrows`` live from the backend.

        Each pool is resolved by out-ref (``allow_spent`` replays a spent one), and its
        lender-bond datum is rebuilt from the pool and its pool manager. The scripts
        come from the live config. An oracle-priced pool needs a signed price for its
        collateral: pass ``oracles``, or leave them out to fetch each from the
        FluidTokens registry through ``provider`` (a default client reads the API key
        from the environment). The validity window defaults to one hour from the tip,
        inside every signed price. Every output carries the least ADA it needs; the
        loan carries enough for the largest output later actions can turn it into.
        Refuses permissioned pools, pools that send borrower bonds to a script, pools
        that lend a token rather than ADA, ADA and policy-wide collateral, a principal
        the pool cannot lend, and a borrow that would leave the continuing pool below
        its minimum ADA.
        """
        from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
            FluidTokensProviderClient,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            config_datum,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_pool_manager,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_script,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_utxo,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_wallet_funding,
        )
        from charli3_dendrite.lending.transactions.infra import current_slot

        if not borrows:
            raise ValueError("a borrow needs at least one pool")
        pools = [
            resolve_utxo(backend, b.pool_out_ref, allow_spent=allow_spent)
            for b in borrows
        ]
        for borrow, pool in zip(borrows, pools):
            _check_borrowable(pool, borrow)

        witnesses = list(oracles or [])
        for borrow, pool in zip(borrows, pools):
            datum = PoolDatum.from_cbor(pool.datum or "")
            option = list(datum.collateral_options)[borrow.chosen_collateral_index]
            if constr(datum.dynamic_collateral_price)[0] != _BOOL_TRUE:
                continue
            if any(w.serves(option.oracle_token_asset) for w in witnesses):
                continue
            if oracles is not None or not has_oracle(option.oracle_token_asset):
                raise ValueError(
                    f"no oracle witness prices the collateral of {borrow.pool_out_ref}",
                )
            provider = provider or FluidTokensProviderClient()
            bundle = provider.fetch_oracle_witness(
                collateral_unit=option_unit(datum, borrow.chosen_collateral_index),
            )
            witnesses.append(OracleWitness.from_bundle(backend, bundle))

        tip = current_slot(backend) if valid_from is None or valid_to is None else 0
        window = signed_window(
            [w.reward for w in witnesses],
            valid_from=valid_from,
            valid_to=valid_to,
            tip=tip,
            cap=BORROW_VALIDITY_SLOTS,
        )

        config = resolve_config_utxo(backend)
        scripts = config_datum(config)
        legs = []
        for borrow, pool in zip(borrows, pools):
            pool_id = pool_nft_name(pool)
            legs.append(
                _size_leg(
                    pool=pool,
                    borrow=borrow,
                    lender_bond_datum=lender_bond_datum(
                        PoolDatum.from_cbor(pool.datum or ""),
                        pool_id=pool_id,
                        pool_manager=resolve_pool_manager(backend, pool_id),
                    ),
                    witnesses=witnesses,
                    borrower_address=borrower_address,
                    window=window,
                ),
            )
        return cls(
            legs=legs,
            funding=list(funding)
            if funding is not None
            else resolve_wallet_funding(backend, borrower_address),
            config=config,
            pool_spend_script_ref=resolve_script(
                backend,
                scripts.pool_spend_script_hash,
            ),
            pool_policy_script_ref=resolve_script(backend, scripts.pool_policy_id),
            borrow_action_script_ref=resolve_script(
                backend,
                scripts.pool_borrow_action_script_hash,
            ),
            loan_policy_script_ref=resolve_script(backend, scripts.loan_policy_id),
            lender_bond_policy_script_ref=resolve_script(
                backend,
                scripts.lender_bond_policy_id,
            ),
            borrower_bond_policy_script_ref=resolve_script(
                backend,
                scripts.borrower_bond_policy_id,
            ),
            borrower_address=borrower_address,
            valid_from=window[0],
            valid_to=window[1],
            oracles=witnesses,
        )


def _check_borrowable(pool: Utxo, borrow: PoolBorrow) -> None:
    """Raise if the pool cannot serve ``borrow`` with this builder."""
    if pool.datum is None:
        raise ValueError(f"pool {borrow.pool_out_ref} is missing its datum")
    datum = PoolDatum.from_cbor(pool.datum)
    if bytes(datum.permissioned_condition_script_hash) != _PERMISSIONLESS:
        raise NotImplementedError("permissioned pools are not supported")
    if datum.common_data.borrower_bond_destination_script_hash:
        # Every loan action refuses a script-held bond, so the loan could not be served.
        raise NotImplementedError(
            "pools that send borrower bonds to a script are not supported",
        )
    if borrow.principal_amount <= 0:
        raise ValueError("principal_amount must be positive")
    if borrow.chosen_collateral_index not in range(len(datum.collateral_options)):
        raise ValueError(
            f"pool {borrow.pool_out_ref} has no collateral option "
            f"{borrow.chosen_collateral_index}",
        )
    if datum.common_data.principal_asset.unit() != "lovelace":
        raise NotImplementedError(
            "pools that lend a token rather than ADA are not supported",
        )
    if option_unit(datum, borrow.chosen_collateral_index) == "lovelace":
        raise NotImplementedError("ADA collateral is not supported")
    if is_policy_wide(list(datum.collateral_options)[borrow.chosen_collateral_index]):
        raise NotImplementedError("policy-wide collateral is not supported")
    available = pool.lovelace
    if borrow.principal_amount > available:
        raise ValueError(
            f"pool {borrow.pool_out_ref} lends at most {available}, "
            f"not {borrow.principal_amount}",
        )


def _size_leg(
    *,
    pool: Utxo,
    borrow: PoolBorrow,
    lender_bond_datum: str,
    witnesses: Sequence[OracleWitness],
    borrower_address: str,
    window: tuple[int, int],
) -> BorrowLeg:
    """Pick the collateral amount and the ADA of each output a leg creates."""
    leg = BorrowLeg(
        pool=pool,
        principal_amount=borrow.principal_amount,
        chosen_collateral_index=borrow.chosen_collateral_index,
        collateral_amount=0,
        lender_bond_datum=lender_bond_datum,
        loan_lovelace=0,
        lender_bond_lovelace=0,
        borrower_bond_lovelace=0,
    )
    datum = leg.pool_datum
    price_num, price_den = 1, 1
    if leg.is_oracle_priced:
        option = list(datum.collateral_options)[leg.chosen_collateral_index]
        reward = witness_for(witnesses, option.oracle_token_asset).reward
        price_num, price_den = reward.price_num, reward.price_den
    minimum = min_collateral_amount(
        datum,
        chosen_collateral_index=leg.chosen_collateral_index,
        principal_amount=leg.principal_amount,
        price_num=price_num,
        price_den=price_den,
    )
    if borrow.collateral_amount is not None and borrow.collateral_amount < minimum:
        raise ValueError(
            f"collateral {borrow.collateral_amount} is below the pool's minimum "
            f"{minimum} for principal {leg.principal_amount}",
        )
    leg.collateral_amount = borrow.collateral_amount or minimum
    slot = window[0]
    # Repay and recast must keep the loan's value unchanged, and change collateral can
    # top up only a token-collateral loan's ADA, so the loan carries the ADA of the
    # largest output it can become: its counters and collateral at their widest. The
    # placeholder coin encodes at the width of the minimum it sizes.
    widest = replace(
        leg,
        collateral_amount=_WIDEST_QUANTITY,
        loan_lovelace=OUTPUT_MIN_ADA,
    )
    widest_datum = with_fields(
        leg_loan_datum(leg, slot_to_posix_ms(window[1])),
        repaid_installments=_WIDEST_COUNT,
        done_recasts=_WIDEST_COUNT,
    )
    leg.loan_lovelace = min_ada(_loan_output(widest, borrower_address, widest_datum))
    leg.borrower_bond_lovelace = min_output_lovelace(
        address=borrower_bond_address(leg, borrower_address),
        assets={c.BORROWER_BOND_POLICY + leg.loan_id.hex(): 1},
        datum=borrower_bond_datum(leg, borrower_address),
        slot=slot,
    )
    leg.lender_bond_lovelace = min_output_lovelace(
        address=str(lender_bond_address(datum)),
        assets={c.LENDER_BOND_POLICY + leg.loan_id.hex(): 1},
        datum=RawCBOR(bytes.fromhex(lender_bond_datum)),
        slot=slot,
    )
    continuing = _pool_output(leg)
    floor = min_output_lovelace(
        address=leg.pool.address,
        assets={u: q for u, q in _pool_assets(leg).items() if u != "lovelace" and q},
        datum=continuing.datum,
        slot=slot,
    )
    if continuing.amount.coin < floor:
        raise ValueError(
            f"borrowing {leg.principal_amount} leaves pool {leg.out_ref} below the "
            f"{floor} lovelace an output needs",
        )
    return leg


def build_borrow(tx_builder: TransactionBuilder, *, snapshot: BorrowSnapshot) -> None:
    """Add a borrow from every leg of ``snapshot`` to ``tx_builder``.

    The borrow's outputs must be the first outputs of the transaction, so the builder
    must hold no outputs yet. Funding inputs are recorded for Ogmios evaluation. The
    caller balances, signs and submits.

    Everything else the caller wants among the transaction's inputs, reference
    inputs, mints and withdrawals must be added before this call, which fills their
    indexes last; outputs may be added after. Discard the builder if this raises.
    """
    if tx_builder.outputs:
        raise ValueError("a borrow's outputs must come first; add them before others")
    legs = snapshot.ordered_legs
    if not legs:
        raise ValueError("a borrow needs at least one pool")

    tx_builder.validity_start = snapshot.valid_from
    tx_builder.ttl = snapshot.valid_to

    for leg in legs:
        tx_builder.add_script_input(
            to_pycardano_utxo(leg.pool),
            script=to_pycardano_utxo(snapshot.pool_spend_script_ref),
            redeemer=Redeemer(LoanSpendRedeemer()),
        )
    for funding in snapshot.funding:
        tx_builder.add_input(to_pycardano_utxo(funding))
        snapshot.add_actor_additional_utxo(ogmios_entry(funding))

    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.config))
    leg_oracles = [snapshot.oracle_for(leg) for leg in legs]

    loan_mint = LoanMintRedeemer(
        config_ref_input_index=0,
        is_pool_origin=BoolTrue(),
        origin_withdraw_redeemer_index=0,
    )
    bond_mint = BondMintRedeemer(
        origin_input_refs=[
            TxOutRef(tx_id=bytes.fromhex(leg.out_ref[0]), index=leg.out_ref[1])
            for leg in sorted(legs, key=lambda leg: leg.loan_id)
        ],
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.loan_policy_script_ref),
        redeemer=Redeemer(loan_mint),
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.lender_bond_policy_script_ref),
        redeemer=Redeemer(bond_mint),
    )
    tx_builder.add_minting_script(
        to_pycardano_utxo(snapshot.borrower_bond_policy_script_ref),
        redeemer=Redeemer(bond_mint),
    )
    names = {AssetName(leg.loan_id): 1 for leg in legs}
    mint = MultiAsset(
        {
            ScriptHash(bytes.fromhex(policy)): Asset(dict(names))
            for policy in (c.LOAN_POLICY, c.LENDER_BOND_POLICY, c.BORROWER_BOND_POLICY)
        },
    )
    tx_builder.mint = mint if tx_builder.mint is None else tx_builder.mint + mint

    dispatch = PoolWithdrawRedeemer(config_ref_input_index=0, action=PoolActionBorrow())
    borrow_data = [
        BorrowData(
            borrower_address=plutus_address(Address.decode(snapshot.borrower_address)),
            output_with_lender_token_index=0,
            output_with_borrower_token_index=0,
            principal_oracle_ref_input_index=PRINCIPAL_ORACLE_PLACEHOLDER,
            chosen_collateral_index=leg.chosen_collateral_index,
            chosen_collateral_oracle_ref_input_index=0,
            wanted_principal_amount=leg.principal_amount,
            pool_id=leg.pool_id,
            permissioned_condition_withdraw_index=PERMISSIONLESS_WITHDRAW_PLACEHOLDER,
        )
        for leg in legs
    ]
    action = PoolBorrowActionWithdrawRedeemer(
        config_ref_input_index=0,
        actions_for_each_input=borrow_data,
    )
    tx_builder.add_withdrawal_script(
        to_pycardano_utxo(snapshot.pool_policy_script_ref),
        Redeemer(dispatch),
    )
    tx_builder.add_withdrawal_script(
        to_pycardano_utxo(snapshot.borrow_action_script_ref),
        Redeemer(action),
    )
    add_zero_withdrawals(
        tx_builder,
        [c.POOL_POLICY, script_hash_of(snapshot.borrow_action_script_ref)],
    )
    add_oracles(tx_builder, [o for o in leg_oracles if o is not None])

    lend_date = slot_to_posix_ms(snapshot.valid_to)
    for leg in legs:
        tx_builder.add_output(_pool_output(leg))
    for leg in legs:
        loan_datum = leg_loan_datum(leg, lend_date)
        tx_builder.add_output(_loan_output(leg, snapshot.borrower_address, loan_datum))
    for leg in legs:
        tx_builder.add_output(_borrower_bond_output(leg, snapshot.borrower_address))
    for leg in legs:
        tx_builder.add_output(_lender_bond_output(leg))

    refs = ref_index(tx_builder)
    config_index = refs[out_ref_of(snapshot.config)]
    dispatch.config_ref_input_index = config_index
    action.config_ref_input_index = config_index
    loan_mint.config_ref_input_index = config_index
    loan_mint.origin_withdraw_redeemer_index = withdraw_redeemer_position(
        tx_builder,
        c.POOL_POLICY,
    )
    for index, (data, oracle) in enumerate(zip(borrow_data, leg_oracles)):
        data.output_with_borrower_token_index = len(legs) * 2 + index
        data.output_with_lender_token_index = len(legs) * 3 + index
        if oracle is not None:
            data.chosen_collateral_oracle_ref_input_index = refs[
                out_ref_of(oracle.feed)
            ]


def _pool_assets(leg: BorrowLeg) -> dict[str, int]:
    """The continuing pool's value: the spent pool minus the borrowed principal."""
    unit = leg.pool_datum.common_data.principal_asset.unit()
    assets = {"lovelace": leg.pool.lovelace} | {p + n: q for p, n, q in leg.pool.assets}
    assets[unit] = assets.get(unit, 0) - leg.principal_amount
    return assets


def _pool_output(leg: BorrowLeg) -> TransactionOutput:
    """The continuing pool: same address and datum, principal reduced."""
    if leg.pool.datum is None:
        raise ValueError("pool UTxO is missing its datum")
    assets = {u: q for u, q in _pool_assets(leg).items() if q or u == "lovelace"}
    return TransactionOutput(
        Address.decode(leg.pool.address),
        asset_to_value(Assets(**assets)),
        datum=RawCBOR(bytes.fromhex(leg.pool.datum)),
    )


def leg_loan_datum(leg: BorrowLeg, lend_date: int) -> LoanDatum:
    """The new loan's datum: the pool terms, the principal and the lend date."""
    return synth_loan_datum(
        pool_datum=leg.pool_datum,
        pool_id=leg.pool_id,
        principal_amount=leg.principal_amount,
        lend_date=lend_date,
        chosen_collateral_index=leg.chosen_collateral_index,
        loan_datum_cls=LoanDatum,
    )


def _loan_output(
    leg: BorrowLeg,
    borrower_address: str,
    datum: LoanDatum,
) -> TransactionOutput:
    """The new loan UTxO: loan NFT and collateral under the borrower's stake."""
    return TransactionOutput(
        Address.decode(loan_address(borrower_address)),
        asset_to_value(
            Assets(
                **{
                    "lovelace": leg.loan_lovelace,
                    c.LOAN_POLICY + leg.loan_id.hex(): 1,
                    leg.collateral_unit: leg.collateral_amount,
                },
            ),
        ),
        datum=datum,
    )


def borrower_bond_address(leg: BorrowLeg, borrower_address: str) -> str:
    """Where the borrower bond goes: the wallet, or the pool's bond destination."""
    destination = leg.pool_datum.common_data.borrower_bond_destination_script_hash
    if not destination:
        return borrower_address
    borrower = Address.decode(borrower_address)
    return Address(
        payment_part=ScriptHash(destination),
        staking_part=borrower.staking_part,
        network=borrower.network,
    ).encode()


def borrower_bond_datum(leg: BorrowLeg, borrower_address: str) -> PlutusData:
    """The borrower bond's datum: the origin pool out-ref and the borrower's auth."""
    payment = Address.decode(borrower_address).payment_part
    auth = (
        AuthCardanoSignature(key_hash=payment.payload)
        if isinstance(payment, VerificationKeyHash)
        else AuthCardanoSpendScript(script_hash=payment.payload)
    )
    return LockedBorrowerManagerDatum(
        origin_ref=TxOutRef(tx_id=bytes.fromhex(leg.out_ref[0]), index=leg.out_ref[1]),
        borrower_auth=auth,
    )


def _borrower_bond_output(leg: BorrowLeg, borrower_address: str) -> TransactionOutput:
    """The borrower bond output, carrying its origin and the borrower's auth."""
    return TransactionOutput(
        Address.decode(borrower_bond_address(leg, borrower_address)),
        asset_to_value(
            Assets(
                **{
                    "lovelace": leg.borrower_bond_lovelace,
                    c.BORROWER_BOND_POLICY + leg.loan_id.hex(): 1,
                },
            ),
        ),
        datum=borrower_bond_datum(leg, borrower_address),
    )


def _lender_bond_output(leg: BorrowLeg) -> TransactionOutput:
    """The lender bond output at the pool's committed address and datum."""
    return TransactionOutput(
        lender_bond_address(leg.pool_datum),
        asset_to_value(
            Assets(
                **{
                    "lovelace": leg.lender_bond_lovelace,
                    c.LENDER_BOND_POLICY + leg.loan_id.hex(): 1,
                },
            ),
        ),
        datum=RawCBOR(bytes.fromhex(leg.lender_bond_datum)),
    )
