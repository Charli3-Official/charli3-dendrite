"""Resolved building blocks for FluidTokens V3 loan-action transactions.

FluidTokens lending is peer-to-peer: there is no shared pool UTxO. A loan is a single
script UTxO (the loan ``general_spend`` address) carrying the loan NFT + collateral
and an inline :class:`LoanDatum`. A loan action (repay / change-collateral / recast)
spends that loan UTxO with an EMPTY redeemer and drives all logic through the
loan-policy + per-action withdraw (reward) scripts, reading the global config NFT and
the lender-bond UTxO as reference inputs.

`Utxo` mirrors the Danogo resolved-UTxO shape (so `_to_utxo` is shared in spirit).
``*Snapshot.from_capture`` rebuilds a snapshot from a captured real on-chain action
(the decisive Ogmios e2e replays it); ``from_backend`` resolves the same live.
"""

from __future__ import annotations

from dataclasses import dataclass

import cbor2  # type: ignore[import-not-found]
from pycardano import Address
from pycardano import PlutusV3Script
from pycardano import RawCBOR
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value

from charli3_dendrite.lending.fluidtokens.constants import BORROWER_BOND_POLICY
from charli3_dendrite.lending.fluidtokens.constants import LENDER_BOND_POLICY
from charli3_dendrite.lending.fluidtokens.constants import (
    LOAN_CHANGE_COLLATERAL_ACTION_SKH,
)
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.constants import LOAN_RECAST_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.constants import LOAN_REPAY_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.constants import LOAN_SPEND_SKH
from charli3_dendrite.lending.fluidtokens.constants import POOL_POLICY
from charli3_dendrite.lending.fluidtokens.constants import POOL_SPEND_SKH
from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_POLICY
from charli3_dendrite.lending.fluidtokens.constants import REQUEST_POLICY
from charli3_dendrite.lending.fluidtokens.constants import REQUEST_SPEND_SKH
from charli3_dendrite.lending.fluidtokens.datums import LoanDatum
from charli3_dendrite.lending.fluidtokens.datums import PoolDatum
from charli3_dendrite.lending.fluidtokens.datums import RequestDatum
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot
from charli3_dendrite.utility import asset_to_value


@dataclass
class Utxo:
    """A resolved UTxO (transaction output or [reference] input)."""

    address: str
    lovelace: int
    assets: list[tuple[str, str, int]]  # (policy_hex, name_hex, qty)
    datum: str | None
    ref_script: str | None = None
    ref_script_type: str | None = None
    out_ref: tuple[str, int] | None = None

    def holds(self, policy: str, name: str, qty: int | None = None) -> bool:
        """True if this UTxO holds the given asset (optionally at an exact qty)."""
        return any(
            a[0] == policy and a[1] == name and (qty is None or a[2] == qty)
            for a in self.assets
        )

    def holds_policy(self, policy: str) -> bool:
        """True if this UTxO holds any asset under the given policy."""
        return any(a[0] == policy for a in self.assets)


def _value(lovelace: int, assets: list[tuple[str, str, int]]) -> Value:
    """A pycardano `Value` from a lovelace balance + (policy, name, qty) leaves."""
    from charli3_dendrite.dataclasses.models import Assets

    root: dict[str, int] = {"lovelace": lovelace}
    for policy, name, qty in assets:
        unit = "lovelace" if not policy and not name else policy + name
        root[unit] = root.get(unit, 0) + qty
    return asset_to_value(Assets(**root))


def _to_utxo(u: Utxo) -> UTxO:
    """Convert a resolved `Utxo` into a pycardano `UTxO` (inline datum + ref script).

    The inline-datum bytes are preserved exactly (wrapped as `RawCBOR`): these UTxOs are
    only referenced by out-ref, so the datum is not re-serialized into the tx body.
    """
    if u.out_ref is None:
        raise ValueError("cannot build a UTxO without an out-ref")
    tx_id_hex, out_idx = u.out_ref
    datum = RawCBOR(bytes.fromhex(u.datum)) if u.datum else None
    script = PlutusV3Script(bytes.fromhex(u.ref_script)) if u.ref_script else None
    return UTxO(
        input=TransactionInput(
            transaction_id=TransactionId(bytes.fromhex(tx_id_hex)),
            index=out_idx,
        ),
        output=TransactionOutput(
            address=Address.decode(u.address),
            amount=_value(u.lovelace, u.assets),
            datum=datum,
            script=script,
        ),
    )


def _as_utxo(d: dict) -> Utxo:
    return Utxo(
        address=d["address"],
        lovelace=int(d["lovelace"]),
        assets=[(p, n, int(q)) for p, n, q in d["assets"]],
        datum=d.get("datum"),
        ref_script=d.get("ref_script"),
        ref_script_type=d.get("ref_script_type"),
        out_ref=tuple(d["out_ref"]) if d.get("out_ref") else None,
    )


def _script_ref_by_hash(ref_inputs: list[Utxo], script_hash: str) -> Utxo:
    """The reference-input UTxO whose PlutusV3 script hashes to `script_hash`."""
    from pycardano import plutus_script_hash

    target = bytes.fromhex(script_hash)
    for u in ref_inputs:
        if not u.ref_script:
            continue
        h = plutus_script_hash(PlutusV3Script(bytes.fromhex(u.ref_script))).payload
        if h == target:
            return u
    raise ValueError(f"no reference script for hash {script_hash}")


@dataclass
class RepaySnapshot(PoolActionSnapshot):
    """Resolved building blocks for a single-loan repay (full / partial).

    The loan UTxO is spent (empty redeemer) and -- on a full repay -- the loan NFT is
    burned; the lender is paid via a wallet output carrying a repayment-receipt datum,
    the borrower-bond NFT is returned to the borrower, and a protocol fee is paid. The
    config NFT and the lender-bond UTxO are reference inputs; the three loan scripts
    (general_spend, loan policy, repay-action withdraw) are supplied by reference.
    """

    loan: Utxo
    borrower_bond: Utxo
    lender_bond: Utxo
    config: Utxo
    spend_script_ref: Utxo
    loan_policy_script_ref: Utxo
    repay_action_script_ref: Utxo
    loan_id: bytes
    loan_policy: str
    bond_policy: str
    lender_bond_policy: str
    fee_address: str
    fee_lovelace: int

    @property
    def loan_datum(self) -> LoanDatum:
        """The loan UTxO's decoded :class:`LoanDatum`."""
        if self.loan.datum is None:
            raise ValueError("snapshot loan UTxO is missing its datum")
        return LoanDatum.from_cbor(bytes.fromhex(self.loan.datum))

    @classmethod
    def from_capture(cls, fix: dict) -> RepaySnapshot:
        """Rebuild a `RepaySnapshot` from a captured real repay (fixture replay).

        The captured tx's spent inputs are the loan UTxO (under the loan-spend script
        address, holding the loan NFT), the borrower-bond input (holding the bond NFT),
        and the borrower's funding inputs. Its reference inputs are the config NFT, the
        lender-bond UTxO, and the three loan scripts. The fee output (the one paying the
        protocol fee address) pins the fee amount + address.
        """
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        loan = next(
            u
            for u in inputs
            if u.datum and _parses_loan(u.datum) and u.holds_policy(LOAN_POLICY)
        )
        loan_id = next(bytes.fromhex(n) for p, n, _ in loan.assets if p == LOAN_POLICY)
        borrower_bond = next(
            u for u in inputs if u.holds(BORROWER_BOND_POLICY, loan_id.hex())
        )
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        lender_bond = next(
            u for u in ref_inputs if u.holds(LENDER_BOND_POLICY, loan_id.hex())
        )
        fee_out = _fee_output(outputs, loan)

        return cls(
            loan=loan,
            borrower_bond=borrower_bond,
            lender_bond=lender_bond,
            config=config,
            spend_script_ref=_script_ref_by_hash(ref_inputs, LOAN_SPEND_SKH),
            loan_policy_script_ref=_script_ref_by_hash(ref_inputs, LOAN_POLICY),
            repay_action_script_ref=_script_ref_by_hash(
                ref_inputs,
                LOAN_REPAY_ACTION_SKH,
            ),
            loan_id=loan_id,
            loan_policy=LOAN_POLICY,
            bond_policy=BORROWER_BOND_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            fee_address=fee_out.address,
            fee_lovelace=fee_out.lovelace,
        )


def _parses_loan(datum_hex: str) -> bool:
    try:
        LoanDatum.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def _parses_pool(datum_hex: str) -> bool:
    try:
        PoolDatum.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def _parses_request(datum_hex: str) -> bool:
    try:
        RequestDatum.from_cbor(bytes.fromhex(datum_hex))
        return True
    except Exception:  # noqa: BLE001
        return False


def _mint_input_ref(fix: dict, policy: str) -> tuple[str, int]:
    """The ``input_ref`` (out-ref) carried on the request-mint redeemer for `policy`."""
    redeemer = next(
        r
        for r in fix["redeemers"]
        if r["purpose"] == "mint" and r["script_hash"] == policy
    )
    decoded = cbor2.loads(bytes.fromhex(redeemer["cbor"]))
    out_ref = decoded.value[1]
    return out_ref.value[0].hex(), int(out_ref.value[1])


def _fee_output(outputs: list[Utxo], loan: Utxo) -> Utxo:
    """The protocol-fee output: a small datum-less ADA-only output to a script address.

    It carries no native assets and no datum, pays a script (fee) address distinct from
    the loan address, and is the lowest-ADA such output (the protocol fee, ~5 ADA).
    """
    candidates = [
        u
        for u in outputs
        if not u.assets and u.datum is None and u.address != loan.address
    ]
    return min(candidates, key=lambda u: u.lovelace)


@dataclass
class RecastSnapshot(PoolActionSnapshot):
    """Resolved building blocks for a single-loan perpetual recast.

    The loan UTxO is spent (empty redeemer) and re-created at the same address with an
    UPDATED datum (``done_recasts`` + 1, the new capitalized ``principal_amount``, and
    the new ``lend_date``) and the same collateral; the lender is paid the recast amount
    via a wallet output carrying a recast receipt; the borrower-bond NFT is returned and
    a protocol fee is paid. The config NFT + the lender-bond UTxO are reference inputs;
    the three loan scripts (general_spend, loan policy, recast action) are by reference.
    The recomputed datum values + paid amount are sourced from the captured action (the
    e2e replays them); a live builder would derive them from the recast math.
    """

    loan: Utxo
    borrower_bond: Utxo
    funding: Utxo
    lender_bond: Utxo
    config: Utxo
    spend_script_ref: Utxo
    loan_policy_script_ref: Utxo
    action_script_ref: Utxo
    lender_address: str
    fee_address: str
    fee_lovelace: int
    amount_paid: int
    new_principal_amount: int
    new_lend_date: int
    valid_from: int
    valid_to: int
    loan_id: bytes
    loan_policy: str
    bond_policy: str
    lender_bond_policy: str

    @property
    def new_loan_datum(self) -> LoanDatum:
        """The continuing loan's updated :class:`LoanDatum` (recast applied).

        Mutates a copy of the spent loan's datum: ``done_recasts`` + 1, the new
        capitalized principal, and the new lend date. Reproduces the on-chain loan
        output datum byte-exact (verified in test_recast).
        """
        if self.loan.datum is None:
            raise ValueError("snapshot loan UTxO is missing its datum")
        datum = LoanDatum.from_cbor(bytes.fromhex(self.loan.datum))
        datum.done_recasts += 1
        datum.principal_amount = self.new_principal_amount
        datum.lend_date = self.new_lend_date
        return datum

    @classmethod
    def from_capture(cls, fix: dict) -> RecastSnapshot:
        """Rebuild a `RecastSnapshot` from a captured real recast (fixture replay)."""
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        loan = next(
            u
            for u in inputs
            if u.datum and _parses_loan(u.datum) and u.holds_policy(LOAN_POLICY)
        )
        loan_id = next(bytes.fromhex(n) for p, n, _ in loan.assets if p == LOAN_POLICY)
        borrower_bond = next(
            u for u in inputs if u.holds(BORROWER_BOND_POLICY, loan_id.hex())
        )
        # The borrower's funding input (no native assets); its out-ref can sort before
        # the loan, so it must be present for the action's input indices to line up.
        funding = next(u for u in inputs if not u.assets and u.out_ref != loan.out_ref)
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        lender_bond = next(
            u for u in ref_inputs if u.holds(LENDER_BOND_POLICY, loan_id.hex())
        )
        loan_out = next(
            u
            for u in outputs
            if u.address == loan.address
            and u.datum is not None
            and u.holds(LOAN_POLICY, loan_id.hex())
        )
        if loan_out.datum is None:
            raise ValueError("recast loan output is missing its datum")
        new_loan_datum = LoanDatum.from_cbor(bytes.fromhex(loan_out.datum))
        lender_out = next(
            u for u in outputs if u.datum is not None and u.address != loan.address
        )
        fee_out = _fee_output(outputs, loan)
        return cls(
            loan=loan,
            borrower_bond=borrower_bond,
            funding=funding,
            lender_bond=lender_bond,
            config=config,
            spend_script_ref=_script_ref_by_hash(ref_inputs, LOAN_SPEND_SKH),
            loan_policy_script_ref=_script_ref_by_hash(ref_inputs, LOAN_POLICY),
            action_script_ref=_script_ref_by_hash(ref_inputs, LOAN_RECAST_ACTION_SKH),
            lender_address=lender_out.address,
            fee_address=fee_out.address,
            fee_lovelace=fee_out.lovelace,
            amount_paid=lender_out.lovelace,
            new_principal_amount=new_loan_datum.principal_amount,
            new_lend_date=new_loan_datum.lend_date,
            valid_from=int(fix["invalid_before"]),
            valid_to=int(fix["invalid_hereafter"]),
            loan_id=loan_id,
            loan_policy=LOAN_POLICY,
            bond_policy=BORROWER_BOND_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
        )


@dataclass
class ChangeCollateralSnapshot(PoolActionSnapshot):
    """Resolved building blocks for a single-loan change-collateral.

    The loan UTxO is spent (empty redeemer) and re-created at the same address with the
    SAME datum but a NEW locked-collateral amount; the borrower-bond NFT is returned. No
    NFT is minted/burned. The action re-prices the collateral via a signed oracle feed,
    so it additionally drives the oracle reward (``Withdraw``) script -- whose redeemer
    carries an off-chain oracle signature and is therefore replayed verbatim
    (`oracle_reward_cbor`) rather than synthesized. The config NFT + oracle feed are
    reference inputs; the four scripts (general_spend, loan policy, change-collateral
    action, oracle) are supplied by reference.
    """

    loan: Utxo
    borrower_bond: Utxo
    config: Utxo
    oracle_feed: Utxo
    spend_script_ref: Utxo
    loan_policy_script_ref: Utxo
    action_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_reward_cbor: str
    loan_id: bytes
    loan_policy: str
    bond_policy: str

    @property
    def loan_datum(self) -> LoanDatum:
        """The loan UTxO's decoded :class:`LoanDatum` (carried through unchanged)."""
        if self.loan.datum is None:
            raise ValueError("snapshot loan UTxO is missing its datum")
        return LoanDatum.from_cbor(bytes.fromhex(self.loan.datum))

    @classmethod
    def from_capture(cls, fix: dict) -> ChangeCollateralSnapshot:
        """Rebuild a `ChangeCollateralSnapshot` from a captured real change-collateral.

        The captured spent inputs are the loan UTxO, the borrower-bond input (holding
        the loan's bond NFT), and the borrower's funding. The reference inputs are the
        config NFT, the oracle feed (holding the loan's principal-oracle NFT), and the
        four loan scripts. The signed oracle reward redeemer is read from the captured
        redeemer set.
        """
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]

        loan = next(
            u
            for u in inputs
            if u.datum and _parses_loan(u.datum) and u.holds_policy(LOAN_POLICY)
        )
        loan_id = next(bytes.fromhex(n) for p, n, _ in loan.assets if p == LOAN_POLICY)
        borrower_bond = next(
            u for u in inputs if u.holds(BORROWER_BOND_POLICY, loan_id.hex())
        )
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        oracle_feed = next(
            u
            for u in ref_inputs
            if u.ref_script is None and not u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        # The oracle withdraw script varies by collateral; identify it (and its signed
        # reward redeemer) by elimination -- it is the reference script / reward
        # redeemer that is none of loan-spend, loan-policy, change-collateral-action.
        known = {LOAN_SPEND_SKH, LOAN_POLICY, LOAN_CHANGE_COLLATERAL_ACTION_SKH}
        oracle_reward = next(
            r
            for r in fix["redeemers"]
            if r["purpose"] == "reward" and r["script_hash"] not in known
        )
        oracle_skh = oracle_reward["script_hash"]
        return cls(
            loan=loan,
            borrower_bond=borrower_bond,
            config=config,
            oracle_feed=oracle_feed,
            spend_script_ref=_script_ref_by_hash(ref_inputs, LOAN_SPEND_SKH),
            loan_policy_script_ref=_script_ref_by_hash(ref_inputs, LOAN_POLICY),
            action_script_ref=_script_ref_by_hash(
                ref_inputs,
                LOAN_CHANGE_COLLATERAL_ACTION_SKH,
            ),
            oracle_script_ref=_script_ref_by_hash(ref_inputs, oracle_skh),
            oracle_reward_cbor=oracle_reward["cbor"],
            loan_id=loan_id,
            loan_policy=LOAN_POLICY,
            bond_policy=BORROWER_BOND_POLICY,
        )


@dataclass
class BorrowSnapshot(PoolActionSnapshot):
    """Resolved building blocks for a single pool-origin borrow.

    A pool UTxO is spent (empty redeemer) and continued at the same address with the
    SAME datum but its principal reduced by the borrowed amount; a new loan UTxO is
    created (carrying the loan NFT + the locked collateral + a synthesized
    :class:`LoanDatum`), and the loan NFT, borrower-bond and lender-bond NFTs are minted
    (asset name = the loan id = hash of the spent pool out-ref). The borrow re-prices
    the collateral via a signed oracle feed, so it also drives the oracle reward
    (``Withdraw``) script -- replayed verbatim (`oracle_reward_cbor`). The config NFT +
    oracle feed are reference inputs; the six scripts (pool spend, pool policy, loan
    policy, lender-/borrower-bond policies, oracle) are supplied by reference.

    Fields the validator's per-borrow checks pin (the continuing pool's remaining
    principal, the lender-bond output's verbatim datum + address, the chosen collateral
    option, the borrower address, the borrowed principal, the validity window) are
    resolved from the captured borrow; a live builder would source them from the chosen
    pool + the borrow request.
    """

    pool: Utxo
    funding: list[Utxo]
    config: Utxo
    oracle_feed: Utxo
    pool_spend_script_ref: Utxo
    pool_policy_script_ref: Utxo
    loan_policy_script_ref: Utxo
    lender_bond_policy_script_ref: Utxo
    borrower_bond_policy_script_ref: Utxo
    oracle_script_ref: Utxo
    oracle_reward_cbor: str
    loan_address: str
    lender_bond_out: Utxo
    borrower_address: str
    borrower_output_lovelace: int
    collateral_unit: str
    collateral_amount: int
    loan_lovelace: int
    fee_address: str
    fee_lovelace: int
    pool_continuation_lovelace: int
    principal_amount: int
    chosen_collateral_index: int
    permissioned_condition_withdraw_index: int
    valid_from: int
    valid_to: int
    loan_id: bytes
    pool_id: bytes
    loan_policy: str
    pool_policy: str
    lender_bond_policy: str
    borrower_bond_policy: str

    @property
    def pool_datum(self) -> PoolDatum:
        """The spent pool UTxO's decoded :class:`PoolDatum` (carried through)."""
        if self.pool.datum is None:
            raise ValueError("snapshot pool UTxO is missing its datum")
        return PoolDatum.from_cbor(bytes.fromhex(self.pool.datum))

    @classmethod
    def from_capture(cls, fix: dict) -> BorrowSnapshot:
        """Rebuild a `BorrowSnapshot` from a captured real pool-origin borrow.

        The captured spent inputs are the pool UTxO (parsing as a ``PoolDatum`` +
        holding the pool NFT) and the borrower's funding/collateral inputs. The outputs
        are the continuing pool (index 0), the loan UTxO (loan NFT + collateral), the
        lender-bond output (its datum hashes to the pool's bond-datum hash), a
        protocol fee, and the borrower's bond + change. The reference inputs are the
        config NFT, the collateral oracle feed, and the six scripts. The Borrow action +
        the signed oracle reward redeemer are read from the captured redeemer set.
        """
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        pool = next(
            u
            for u in inputs
            if u.datum and _parses_pool(u.datum) and u.holds_policy(POOL_POLICY)
        )
        pool_id = next(bytes.fromhex(n) for p, n, _ in pool.assets if p == POOL_POLICY)
        funding = [u for u in inputs if u.out_ref != pool.out_ref]

        loan_out = next(
            u for u in outputs if u.datum is not None and u.holds_policy(LOAN_POLICY)
        )
        loan_id = next(
            bytes.fromhex(n) for p, n, _ in loan_out.assets if p == LOAN_POLICY
        )
        collateral = next((p, n, q) for p, n, q in loan_out.assets if p != LOAN_POLICY)
        lender_bond_out = next(u for u in outputs if u.holds_policy(LENDER_BOND_POLICY))
        borrower_out = next(u for u in outputs if u.holds_policy(BORROWER_BOND_POLICY))
        pool_out = outputs[0]

        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        oracle_feed = next(
            u
            for u in ref_inputs
            if u.ref_script is None and not u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        oracle_reward = next(
            r
            for r in fix["redeemers"]
            if r["purpose"] == "reward" and r["script_hash"] != POOL_POLICY
        )
        pool_withdraw = next(
            r
            for r in fix["redeemers"]
            if r["purpose"] == "reward" and r["script_hash"] == POOL_POLICY
        )
        borrow = cbor2.loads(bytes.fromhex(pool_withdraw["cbor"])).value[1][0]
        (
            _addr,
            _out_with_lender,
            _principal_oracle_idx,
            chosen_collateral_index,
            _collateral_oracle_idx,
            principal_amount,
            _pool_id,
            permissioned_idx,
        ) = borrow.value
        fee_out = _fee_output(outputs, pool)

        return cls(
            pool=pool,
            funding=funding,
            config=config,
            oracle_feed=oracle_feed,
            pool_spend_script_ref=_script_ref_by_hash(ref_inputs, POOL_SPEND_SKH),
            pool_policy_script_ref=_script_ref_by_hash(ref_inputs, POOL_POLICY),
            loan_policy_script_ref=_script_ref_by_hash(ref_inputs, LOAN_POLICY),
            lender_bond_policy_script_ref=_script_ref_by_hash(
                ref_inputs,
                LENDER_BOND_POLICY,
            ),
            borrower_bond_policy_script_ref=_script_ref_by_hash(
                ref_inputs,
                BORROWER_BOND_POLICY,
            ),
            oracle_script_ref=_script_ref_by_hash(
                ref_inputs,
                oracle_reward["script_hash"],
            ),
            oracle_reward_cbor=oracle_reward["cbor"],
            loan_address=loan_out.address,
            lender_bond_out=lender_bond_out,
            borrower_address=borrower_out.address,
            borrower_output_lovelace=borrower_out.lovelace,
            collateral_unit=collateral[0] + collateral[1],
            collateral_amount=collateral[2],
            loan_lovelace=loan_out.lovelace,
            fee_address=fee_out.address,
            fee_lovelace=fee_out.lovelace,
            pool_continuation_lovelace=pool_out.lovelace,
            principal_amount=int(principal_amount),
            chosen_collateral_index=int(chosen_collateral_index),
            permissioned_condition_withdraw_index=int(permissioned_idx),
            valid_from=int(fix["invalid_before"]),
            valid_to=int(fix["invalid_hereafter"]),
            loan_id=loan_id,
            pool_id=pool_id,
            loan_policy=LOAN_POLICY,
            pool_policy=POOL_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            borrower_bond_policy=BORROWER_BOND_POLICY,
        )


@dataclass
class CreateRequestSnapshot(PoolActionSnapshot):
    """Resolved building blocks for creating a borrow request.

    The borrower's wallet inputs fund the request: one request NFT is minted (asset
    name ``0x00`` ++ ``blake2b_224`` of the chosen input out-ref) and locked, together
    with the collateral and an inline :class:`RequestDatum`, at the request spend
    address. Only the request mint policy runs; the ``RequestDatum`` is the borrower's
    choice and is carried through verbatim (it is validated only when the request is
    later spent). The config NFT + the request policy script are reference inputs.
    """

    funding: list[Utxo]
    config: Utxo
    request_policy_script_ref: Utxo
    request_address: str
    request_datum: str
    request_lovelace: int
    collateral: list[tuple[str, str, int]]
    input_ref: tuple[str, int]
    request_policy: str

    @classmethod
    def from_capture(cls, fix: dict) -> CreateRequestSnapshot:
        """Rebuild a `CreateRequestSnapshot` from a captured real create-request."""
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        request_out = next(
            u for u in outputs if u.datum is not None and u.holds_policy(REQUEST_POLICY)
        )
        if request_out.datum is None:
            raise ValueError("create-request output is missing its datum")
        collateral = [
            (p, n, q) for p, n, q in request_out.assets if p != REQUEST_POLICY
        ]
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        return cls(
            funding=inputs,
            config=config,
            request_policy_script_ref=_script_ref_by_hash(ref_inputs, REQUEST_POLICY),
            request_address=request_out.address,
            request_datum=request_out.datum,
            request_lovelace=request_out.lovelace,
            collateral=collateral,
            input_ref=_mint_input_ref(fix, REQUEST_POLICY),
            request_policy=REQUEST_POLICY,
        )


@dataclass
class CancelRequestSnapshot(PoolActionSnapshot):
    """Resolved building blocks for cancelling a borrow request.

    The request UTxO is spent (empty redeemer via the request spend script), the
    request NFT is burned, and the request-policy reward (``Cancel``) drives the logic
    -- authorized by the borrower (its ``borrowerAuth`` verification-key hash must be a
    required signer). The collateral returns to the borrower. The config NFT + the
    request spend / request policy scripts are reference inputs; the burn redeemer's
    ``input_ref`` is any spent input (replayed from the capture for byte-exactness).
    """

    request: Utxo
    funding: list[Utxo]
    config: Utxo
    request_spend_script_ref: Utxo
    request_policy_script_ref: Utxo
    request_id: bytes
    borrower_pkh: bytes
    mint_input_ref: tuple[str, int]
    request_policy: str

    @classmethod
    def from_capture(cls, fix: dict) -> CancelRequestSnapshot:
        """Rebuild a `CancelRequestSnapshot` from a captured real cancel-request."""
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]

        request = next(
            u
            for u in inputs
            if u.datum and _parses_request(u.datum) and u.holds_policy(REQUEST_POLICY)
        )
        request_id = next(
            bytes.fromhex(n) for p, n, _ in request.assets if p == REQUEST_POLICY
        )
        funding = [u for u in inputs if u.out_ref != request.out_ref]
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        if request.datum is None:
            raise ValueError("cancel-request input is missing its datum")
        datum = RequestDatum.from_cbor(bytes.fromhex(request.datum))
        borrower_pkh = bytes(datum.borrower_auth.data.value[0])
        return cls(
            request=request,
            funding=funding,
            config=config,
            request_spend_script_ref=_script_ref_by_hash(
                ref_inputs,
                REQUEST_SPEND_SKH,
            ),
            request_policy_script_ref=_script_ref_by_hash(ref_inputs, REQUEST_POLICY),
            request_id=request_id,
            borrower_pkh=borrower_pkh,
            mint_input_ref=_mint_input_ref(fix, REQUEST_POLICY),
            request_policy=REQUEST_POLICY,
        )
