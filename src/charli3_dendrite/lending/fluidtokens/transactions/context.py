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

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

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
from charli3_dendrite.lending.fluidtokens.constants import LOAN_ADDRESS
from charli3_dendrite.lending.fluidtokens.constants import (
    LOAN_CHANGE_COLLATERAL_ACTION_SKH,
)
from charli3_dendrite.lending.fluidtokens.constants import LOAN_POLICY
from charli3_dendrite.lending.fluidtokens.constants import LOAN_RECAST_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.constants import LOAN_REPAY_ACTION_SKH
from charli3_dendrite.lending.fluidtokens.constants import LOAN_SPEND_SKH
from charli3_dendrite.lending.fluidtokens.constants import POOL_ADDRESS
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

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import PoolTerms


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


_SCRIPT_LANG = {
    "plutusV1": "plutus:v1",
    "plutusV2": "plutus:v2",
    "plutusV3": "plutus:v3",
}


def ogmios_entry(u: Utxo) -> dict[str, Any]:
    """Serialize any resolved `Utxo` into its Ogmios ``additionalUtxo`` entry.

    Used both for funding (actor) inputs when building a tx and, in the e2e replays, for
    spent/reference inputs. Funding inputs may already be spent / freshly created, so
    Ogmios cannot resolve them from its own ledger snapshot; they are supplied here for
    evaluation.
    """
    if u.out_ref is None:
        raise ValueError("cannot build an additionalUtxo entry without an out-ref")
    value: dict[str, Any] = {"ada": {"lovelace": u.lovelace}}
    for policy, name, qty in u.assets:
        value.setdefault(policy, {})[name] = qty
    entry: dict[str, Any] = {
        "transaction": {"id": u.out_ref[0]},
        "index": u.out_ref[1],
        "address": u.address,
        "value": value,
    }
    if u.datum:
        entry["datum"] = u.datum
    if u.ref_script:
        entry["script"] = {
            "language": _SCRIPT_LANG.get(u.ref_script_type or "", "plutus:v3"),
            "cbor": u.ref_script,
        }
    return entry


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

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        loan_utxo: tuple[str, int],
        actor_address: str,
        allow_spent: bool = False,
        config_outref: tuple[str, int] | None = None,
        spend_ref_outref: tuple[str, int] | None = None,
        loan_policy_ref_outref: tuple[str, int] | None = None,
        repay_action_ref_outref: tuple[str, int] | None = None,
        lender_bond_outref: tuple[str, int] | None = None,
        borrower_bond_outref: tuple[str, int] | None = None,
        fee_address: str | None = None,
        fee_lovelace: int | None = None,
    ) -> RepaySnapshot:
        """Resolve a full-repay `RepaySnapshot` live from chain state via the backend.

        The loan UTxO is resolved by ``loan_utxo`` out-ref (``allow_spent`` replays a
        captured/closed loan); its loan NFT supplies the ``loan_id``. The borrower-bond
        input (in ``actor_address``'s wallet) and the lender-bond reference input are
        located by the bond NFTs (``BORROWER_BOND_POLICY`` / ``LENDER_BOND_POLICY`` +
        ``loan_id``) unless pinned by out-ref. The config NFT and the three loan scripts
        (loan spend, loan policy, repay-action) are resolved unless pinned. The protocol
        fee defaults to :func:`_resolve_protocol_fee`; explicit ``fee_address`` /
        ``fee_lovelace`` are used as-is (byte-exact replay).
        """
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_script_ref,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_asset,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )

        loan = resolve_utxo_by_outref(backend, *loan_utxo, allow_spent=allow_spent)
        if loan.datum is None:
            raise ValueError("resolved loan UTxO is missing its datum")
        loan_id = next(bytes.fromhex(n) for p, n, _ in loan.assets if p == LOAN_POLICY)

        def _pin_or(
            outref: tuple[str, int] | None,
            resolver: Callable[[], Utxo],
        ) -> Utxo:
            if outref:
                return resolve_utxo_by_outref(backend, *outref, allow_spent=True)
            return resolver()

        config = _pin_or(config_outref, lambda: resolve_config_utxo(backend))
        spend_script_ref = _pin_or(
            spend_ref_outref,
            lambda: resolve_script_ref(backend, LOAN_SPEND_SKH),
        )
        loan_policy_script_ref = _pin_or(
            loan_policy_ref_outref,
            lambda: resolve_script_ref(backend, LOAN_POLICY),
        )
        repay_action_script_ref = _pin_or(
            repay_action_ref_outref,
            lambda: resolve_script_ref(backend, LOAN_REPAY_ACTION_SKH),
        )
        lender_bond = _pin_or(
            lender_bond_outref,
            lambda: resolve_utxo_by_asset(backend, LENDER_BOND_POLICY, loan_id.hex()),
        )
        borrower_bond = _pin_or(
            borrower_bond_outref,
            lambda: resolve_utxo_by_asset(backend, BORROWER_BOND_POLICY, loan_id.hex()),
        )
        if borrower_bond.address != actor_address:
            raise ValueError(
                "borrower-bond UTxO is not held by actor_address "
                f"({borrower_bond.address} != {actor_address})",
            )

        if fee_address is None or fee_lovelace is None:
            resolved_addr, resolved_amt = _resolve_protocol_fee(config)
            fee_address = fee_address if fee_address is not None else resolved_addr
            fee_lovelace = fee_lovelace if fee_lovelace is not None else resolved_amt

        return cls(
            loan=loan,
            borrower_bond=borrower_bond,
            lender_bond=lender_bond,
            config=config,
            spend_script_ref=spend_script_ref,
            loan_policy_script_ref=loan_policy_script_ref,
            repay_action_script_ref=repay_action_script_ref,
            loan_id=loan_id,
            loan_policy=LOAN_POLICY,
            bond_policy=BORROWER_BOND_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            fee_address=fee_address,
            fee_lovelace=fee_lovelace,
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

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        request_utxo: tuple[str, int],
        borrower_address: str | None = None,
        allow_spent_request: bool = False,
        funding_outrefs: list[tuple[str, int]] | None = None,
        config_outref: tuple[str, int] | None = None,
        request_spend_ref_outref: tuple[str, int] | None = None,
        request_policy_ref_outref: tuple[str, int] | None = None,
    ) -> CancelRequestSnapshot:
        """Resolve a `CancelRequestSnapshot` live from chain state via the backend.

        The request UTxO is resolved by ``request_utxo`` out-ref
        (``allow_spent_request`` lets a captured/historical request be replayed); its
        ``RequestDatum`` supplies the request id + the borrower vkey hash. The config
        NFT and the request spend / request policy scripts are resolved unless pinned by
        out-ref. Funding is resolved from ``funding_outrefs`` or the borrower's wallet
        (``borrower_address``) and is optional for a burn-only cancel: the burn
        redeemer's ``input_ref`` only needs to reference a spent input, so it falls back
        to the request out-ref.
        """
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_funding,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_script_ref,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )

        request = resolve_utxo_by_outref(
            backend,
            *request_utxo,
            allow_spent=allow_spent_request,
        )
        if request.datum is None or request.out_ref is None:
            raise ValueError("resolved request UTxO is missing its datum/out-ref")
        request_id = next(
            bytes.fromhex(n) for p, n, _ in request.assets if p == REQUEST_POLICY
        )
        datum = RequestDatum.from_cbor(bytes.fromhex(request.datum))
        borrower_pkh = bytes(datum.borrower_auth.data.value[0])

        if config_outref:
            config = resolve_utxo_by_outref(backend, *config_outref, allow_spent=True)
        else:
            config = resolve_config_utxo(backend)

        if request_spend_ref_outref:
            request_spend_script_ref = resolve_utxo_by_outref(
                backend,
                *request_spend_ref_outref,
                allow_spent=True,
            )
        else:
            request_spend_script_ref = resolve_script_ref(backend, REQUEST_SPEND_SKH)

        if request_policy_ref_outref:
            request_policy_script_ref = resolve_utxo_by_outref(
                backend,
                *request_policy_ref_outref,
                allow_spent=True,
            )
        else:
            request_policy_script_ref = resolve_script_ref(backend, REQUEST_POLICY)

        if funding_outrefs:
            funding = [
                resolve_utxo_by_outref(backend, h, i, allow_spent=True)
                for h, i in funding_outrefs
            ]
        elif borrower_address:
            funding = resolve_funding(backend, borrower_address)
        else:
            funding = []
        mint_input_ref = funding[0].out_ref if funding else request_utxo
        if mint_input_ref is None:
            raise ValueError("could not determine mint input_ref for request cancel")

        return cls(
            request=request,
            funding=funding,
            config=config,
            request_spend_script_ref=request_spend_script_ref,
            request_policy_script_ref=request_policy_script_ref,
            request_id=request_id,
            borrower_pkh=borrower_pkh,
            mint_input_ref=mint_input_ref,
            request_policy=REQUEST_POLICY,
        )


def _resolve_loan_lovelace(
    loan_lovelace: int | None,
    *,
    loan_address: str,
    loan_id: bytes,
    collateral: tuple[str, str, int],
    request_datum: RequestDatum,
    request_id: bytes,
    principal: int,
    valid_from: int,
    valid_to: int,
) -> int:
    """The loan output's coin: an explicit value as-is, else its protocol min-ADA.

    ``build_lend`` writes ``loan_lovelace`` onto the loan output verbatim (balancing
    does not raise a fixed output's coin), so the live default must be the output's real
    min-UTxO rather than a flat guess that could fall below it. Mirrors the loan output
    ``_add_lend_outputs`` builds (loan NFT + collateral + synthesized ``LoanDatum``) and
    floors it via ``pycardano.min_lovelace`` over the nominal mainnet params in
    ``EvalContext`` (the mechanism the sibling loan builders use for their outputs).
    """
    if loan_lovelace is not None:
        return loan_lovelace

    from pycardano import min_lovelace

    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
        synth_loan_datum_from_request,
    )
    from charli3_dendrite.lending.transactions.infra import OUTPUT_MIN_ADA
    from charli3_dendrite.lending.transactions.infra import EvalContext
    from charli3_dendrite.utility import slot_to_posix_ms

    loan_datum = synth_loan_datum_from_request(
        request_datum=request_datum,
        request_id=request_id,
        given_principal_amount=principal,
        lend_date=slot_to_posix_ms(valid_to),
    )
    # OUTPUT_MIN_ADA is only a nominal coin so the output serializes for sizing; its
    # byte length matches the resulting min, so it does not skew `min_lovelace`.
    loan_output = TransactionOutput(
        Address.decode(loan_address),
        asset_to_value(
            Assets(
                **{
                    "lovelace": OUTPUT_MIN_ADA,
                    LOAN_POLICY + loan_id.hex(): 1,
                    collateral[0] + collateral[1]: collateral[2],
                },
            ),
        ),
        datum=loan_datum,
    )
    return min_lovelace(EvalContext(last_block_slot=valid_from), output=loan_output)


# The protocol fee a loan action pays: a fixed script (fee) address and a flat 5-ADA
# amount. Neither the fee address nor the amount appears in the config NFT datum (which
# carries only the protocol's script hashes / policy ids); both are pinned to the fee
# value the fee output in ``repay_full.json`` carries (see test_protocol_fee).
#
# MAINTENANCE: these are fixed deployment constants, NOT validated against the live
# chain. They must be updated if the protocol redeploys or changes its fee -- a stale
# value would be returned silently, with no runtime signal.
_PROTOCOL_FEE_ADDRESS = "addr1x9z4cq2qm23tvtn4uxeuzf8arnvqgqw6whzjh3r8jqkxdk4f02nfch7l297055r7z37fwamryqrd3en97sp7jq7gtffsfkk0p6"  # noqa: E501
_PROTOCOL_FEE_LOVELACE = 5_000_000


def _resolve_protocol_fee(config: Utxo) -> tuple[str, int]:  # noqa: ARG001
    """The protocol fee (address, lovelace) a loan action must pay.

    Not present in the config datum; pinned to the fee value the fee output in
    ``repay_full.json`` carries (see test_protocol_fee) via the deployment constants
    above. ``config`` is accepted for API symmetry with a datum-sourced resolver (and so
    a redeploy can re-source it here). See the constants' MAINTENANCE note: the pinned
    values are not validated against the live chain.
    """
    return _PROTOCOL_FEE_ADDRESS, _PROTOCOL_FEE_LOVELACE


@dataclass
class LendSnapshot(PoolActionSnapshot):
    """Resolved building blocks for filling a borrow request (``Lend``).

    A lender spends the borrower's request UTxO (empty redeemer via the request spend
    ``general_spend`` script) and drives the request-policy reward (``Lend``): the
    request NFT is burned, and the loan NFT + borrower-bond + lender-bond are minted
    (asset name = the loan id = ``blake2b_224`` of the spent request out-ref). The loan
    UTxO carries the loan NFT + the request's collateral (unchanged) + a synthesized
    :class:`LoanDatum`; the borrower output (absolute index 0) carries the principal +
    the borrower bond + ``InlineDatum(requestRef)``. The lender bond is unconstrained on
    chain (it rides in the tx change). Targets the simplest case: permissionless, ADA
    principal, static pricing (no oracle witnesses).
    """

    request: Utxo
    funding: list[Utxo]
    config: Utxo
    request_spend_script_ref: Utxo
    request_policy_script_ref: Utxo
    loan_policy_script_ref: Utxo
    lender_bond_policy_script_ref: Utxo
    borrower_bond_policy_script_ref: Utxo
    loan_address: str
    borrower_address: str
    request_id: bytes
    loan_id: bytes
    given_principal_amount: int
    principal_oracle_ref_input_index: int
    collateral_oracle_ref_input_index: int
    permissioned_condition_withdraw_index: int
    mint_input_ref: tuple[str, int]
    collateral_unit: str
    collateral_amount: int
    loan_lovelace: int
    borrower_output_lovelace: int
    valid_from: int
    valid_to: int
    request_policy: str
    loan_policy: str
    lender_bond_policy: str
    borrower_bond_policy: str

    @property
    def request_datum(self) -> RequestDatum:
        """The spent request UTxO's decoded :class:`RequestDatum`."""
        if self.request.datum is None:
            raise ValueError("snapshot request UTxO is missing its datum")
        return RequestDatum.from_cbor(bytes.fromhex(self.request.datum))

    @classmethod
    def from_capture(cls, fix: dict) -> LendSnapshot:
        """Rebuild a `LendSnapshot` from a captured real request-fill.

        The spent request UTxO parses as a ``RequestDatum`` + holds the request NFT; the
        remaining spent inputs are the lender's funding. The loan output holds the loan
        NFT + the collateral (+ a ``LoanDatum``); the borrower output holds the borrower
        bond. The ``Lend`` action's indices/amount are read from the captured
        request-policy reward redeemer.
        """
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        request = next(
            u
            for u in inputs
            if u.datum and _parses_request(u.datum) and u.holds_policy(REQUEST_POLICY)
        )
        if request.out_ref is None:
            raise ValueError("request input is missing its out-ref")
        request_id = next(
            bytes.fromhex(n) for p, n, _ in request.assets if p == REQUEST_POLICY
        )
        funding = [u for u in inputs if u.out_ref != request.out_ref]

        loan_out = next(
            u for u in outputs if u.datum is not None and u.holds_policy(LOAN_POLICY)
        )
        loan_id = next(
            bytes.fromhex(n) for p, n, _ in loan_out.assets if p == LOAN_POLICY
        )
        collateral = next((p, n, q) for p, n, q in loan_out.assets if p != LOAN_POLICY)
        borrower_out = next(u for u in outputs if u.holds_policy(BORROWER_BOND_POLICY))

        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        lend_reward = next(
            r
            for r in fix["redeemers"]
            if r["purpose"] == "reward" and r["script_hash"] == REQUEST_POLICY
        )
        lend = cbor2.loads(bytes.fromhex(lend_reward["cbor"])).value[1][0]
        (
            principal_oracle_idx,
            collateral_oracle_idx,
            given_principal_amount,
            _request_id,
            permissioned_idx,
        ) = lend.value

        return cls(
            request=request,
            funding=funding,
            config=config,
            request_spend_script_ref=_script_ref_by_hash(ref_inputs, REQUEST_SPEND_SKH),
            request_policy_script_ref=_script_ref_by_hash(ref_inputs, REQUEST_POLICY),
            loan_policy_script_ref=_script_ref_by_hash(ref_inputs, LOAN_POLICY),
            lender_bond_policy_script_ref=_script_ref_by_hash(
                ref_inputs,
                LENDER_BOND_POLICY,
            ),
            borrower_bond_policy_script_ref=_script_ref_by_hash(
                ref_inputs,
                BORROWER_BOND_POLICY,
            ),
            loan_address=loan_out.address,
            borrower_address=borrower_out.address,
            request_id=request_id,
            loan_id=loan_id,
            given_principal_amount=int(given_principal_amount),
            principal_oracle_ref_input_index=int(principal_oracle_idx),
            collateral_oracle_ref_input_index=int(collateral_oracle_idx),
            permissioned_condition_withdraw_index=int(permissioned_idx),
            mint_input_ref=_mint_input_ref(fix, REQUEST_POLICY),
            collateral_unit=collateral[0] + collateral[1],
            collateral_amount=collateral[2],
            loan_lovelace=loan_out.lovelace,
            borrower_output_lovelace=borrower_out.lovelace,
            valid_from=int(fix["invalid_before"]),
            valid_to=int(fix["invalid_hereafter"]),
            request_policy=REQUEST_POLICY,
            loan_policy=LOAN_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            borrower_bond_policy=BORROWER_BOND_POLICY,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        request_utxo: tuple[str, int],
        given_principal_amount: int | None = None,
        lender_address: str | None = None,
        allow_spent_request: bool = False,
        funding_outrefs: list[tuple[str, int]] | None = None,
        config_outref: tuple[str, int] | None = None,
        request_spend_ref_outref: tuple[str, int] | None = None,
        request_policy_ref_outref: tuple[str, int] | None = None,
        loan_policy_ref_outref: tuple[str, int] | None = None,
        lender_bond_ref_outref: tuple[str, int] | None = None,
        borrower_bond_ref_outref: tuple[str, int] | None = None,
        valid_from: int | None = None,
        valid_to: int | None = None,
        loan_lovelace: int | None = None,
    ) -> LendSnapshot:
        """Resolve a `LendSnapshot` live from chain state via the backend.

        The request UTxO is resolved by ``request_utxo`` out-ref
        (``allow_spent_request`` lets a captured/historical request be replayed); its
        ``RequestDatum`` supplies
        the borrower address, collateral, and principal bounds, and its value supplies
        the request NFT name. The config NFT + the five script refs (request policy,
        request spend, loan policy, both bond policies) are resolved unless pinned by
        out-ref. ``given_principal_amount`` defaults to the request's ``maxPrincipal``
        (and is validated against the request's static ``[min, max]`` bounds); the
        oracle / permissioned indices default to ``0`` (inert in the static
        permissionless case). The borrower output lovelace equals the principal (ADA),
        and the loan output carries ``loan_lovelace`` + the request's collateral
        unchanged. ``loan_lovelace`` defaults (when ``None``) to the loan output's
        protocol min-ADA -- computed via ``pycardano.min_lovelace`` over the actual
        loan output (loan NFT + collateral + synthesized ``LoanDatum``) -- since
        ``build_lend`` uses it verbatim as the output coin and balancing does not raise
        a fixed output's coin; an explicit value is used as-is (byte-exact replay).

        ``valid_from`` / ``valid_to`` pin the transaction's validity window (and the
        loan's baked-in maturity). When omitted they default to a live window derived
        from the backend tip -- ``valid_from`` = the current slot, ``valid_to`` =
        ``valid_from`` + ``LOAN_ACTION_VALIDITY_SLOTS`` -- matching the
        ``set_validity_window`` convention the other loan actions use; pass them
        explicitly to replay a captured window byte-exact.
        """
        from charli3_dendrite.lending.fluidtokens.transactions._common import (
            LOAN_ACTION_VALIDITY_SLOTS,
        )
        from charli3_dendrite.lending.fluidtokens.transactions._common import (
            address_from_plutus,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
            request_principal_bounds,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.lend import loan_nft_name
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_funding,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_script_ref,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )
        from charli3_dendrite.lending.transactions.infra import current_slot

        request = resolve_utxo_by_outref(
            backend,
            *request_utxo,
            allow_spent=allow_spent_request,
        )
        if request.datum is None or request.out_ref is None:
            raise ValueError("resolved request UTxO is missing its datum/out-ref")
        request_id = next(
            bytes.fromhex(n) for p, n, _ in request.assets if p == REQUEST_POLICY
        )
        datum = RequestDatum.from_cbor(bytes.fromhex(request.datum))
        borrower_address = str(address_from_plutus(datum.borrower_address.data))
        collateral = next(
            (p, n, q) for p, n, q in request.assets if p != REQUEST_POLICY
        )
        min_principal, max_principal = request_principal_bounds(
            datum,
            collateral_amount=collateral[2],
        )
        principal = given_principal_amount or max_principal
        if not min_principal <= principal <= max_principal:
            raise ValueError(
                f"lend principal {principal} is outside the request's static bounds "
                f"[{min_principal}, {max_principal}]",
            )

        # When the caller does not pin the validity window, derive it live from the
        # backend tip, mirroring `set_validity_window` (used by build_repay /
        # build_change_collateral): lower bound = current slot, upper bound = lower +
        # LOAN_ACTION_VALIDITY_SLOTS. `build_lend` consumes these verbatim, so leaving
        # them at 0 would emit an unusable (slot-0 validity + maturity) transaction.
        if valid_from is None or valid_to is None:
            tip = current_slot(backend)
            resolved_valid_from = valid_from if valid_from is not None else tip
            resolved_valid_to = (
                valid_to
                if valid_to is not None
                else resolved_valid_from + LOAN_ACTION_VALIDITY_SLOTS
            )
        else:
            resolved_valid_from = valid_from
            resolved_valid_to = valid_to

        def _ref(
            outref: tuple[str, int] | None,
            script_hash: str,
        ) -> Utxo:
            if outref:
                return resolve_utxo_by_outref(backend, *outref, allow_spent=True)
            return resolve_script_ref(backend, script_hash)

        if config_outref:
            config = resolve_utxo_by_outref(backend, *config_outref, allow_spent=True)
        else:
            config = resolve_config_utxo(backend)

        if funding_outrefs:
            funding = [
                resolve_utxo_by_outref(backend, h, i, allow_spent=True)
                for h, i in funding_outrefs
            ]
        elif lender_address:
            funding = resolve_funding(backend, lender_address)
        else:
            funding = []

        # The new loan is created at the loan spend script's payment credential paired
        # with the staking credential inherited from the spent request UTxO -- the
        # borrower's chosen action-withdraw credential that governs the loan lifecycle.
        # The Lend validator enforces this carry-over, so the loan output address must
        # reuse the request's stake part; LOAN_ADDRESS only pins the payment credential
        # (its stake part is a placeholder that does not match a live request).
        if request.address is None:
            raise ValueError("resolved request UTxO is missing its address")
        request_address = Address.decode(request.address)
        loan_address = str(
            Address(
                payment_part=Address.decode(LOAN_ADDRESS).payment_part,
                staking_part=request_address.staking_part,
                network=request_address.network,
            ),
        )

        loan_id = loan_nft_name(request.out_ref)
        resolved_loan_lovelace = _resolve_loan_lovelace(
            loan_lovelace,
            loan_address=loan_address,
            loan_id=loan_id,
            collateral=collateral,
            request_datum=datum,
            request_id=request_id,
            principal=principal,
            valid_from=resolved_valid_from,
            valid_to=resolved_valid_to,
        )

        return cls(
            request=request,
            funding=funding,
            config=config,
            request_spend_script_ref=_ref(request_spend_ref_outref, REQUEST_SPEND_SKH),
            request_policy_script_ref=_ref(request_policy_ref_outref, REQUEST_POLICY),
            loan_policy_script_ref=_ref(loan_policy_ref_outref, LOAN_POLICY),
            lender_bond_policy_script_ref=_ref(
                lender_bond_ref_outref,
                LENDER_BOND_POLICY,
            ),
            borrower_bond_policy_script_ref=_ref(
                borrower_bond_ref_outref,
                BORROWER_BOND_POLICY,
            ),
            loan_address=loan_address,
            borrower_address=borrower_address,
            request_id=request_id,
            loan_id=loan_id,
            given_principal_amount=principal,
            principal_oracle_ref_input_index=0,
            collateral_oracle_ref_input_index=0,
            # Intentionally differs from a captured fill (which may carry 1): the
            # permissioned-condition index is ignored for permissionless requests, so 0
            # is inert here. Not a bug -- do not "restore" the captured value.
            permissioned_condition_withdraw_index=0,
            # Intentionally differs from a captured fill (which used a different spent
            # input): the request-NFT burn redeemer's input_ref only needs to point at
            # ANY spent input, and the request UTxO is itself spent, so its out-ref is a
            # valid, always-available choice.
            mint_input_ref=request.out_ref,
            collateral_unit=collateral[0] + collateral[1],
            collateral_amount=collateral[2],
            loan_lovelace=resolved_loan_lovelace,
            borrower_output_lovelace=principal,
            valid_from=resolved_valid_from,
            valid_to=resolved_valid_to,
            request_policy=REQUEST_POLICY,
            loan_policy=LOAN_POLICY,
            lender_bond_policy=LENDER_BOND_POLICY,
            borrower_bond_policy=BORROWER_BOND_POLICY,
        )


@dataclass
class CreatePoolSnapshot(PoolActionSnapshot):
    """Resolved building blocks for creating a lender pool.

    The lender's wallet inputs fund the pool: one pool NFT is minted (asset name
    ``0x00`` ++ ``blake2b_224`` of the chosen input out-ref) and locked, together with
    the lender's liquidity and an inline :class:`PoolDatum`, at the pool spend address.
    Only the pool mint policy runs; the ``PoolDatum`` is the lender's choice and is
    carried through verbatim (it is validated only when the pool is later
    borrowed/cancelled). The config NFT + the pool policy script are reference inputs.
    """

    funding: list[Utxo]
    config: Utxo
    pool_policy_script_ref: Utxo
    pool_address: str
    pool_datum: str
    pool_lovelace: int
    liquidity: list[tuple[str, str, int]]
    input_ref: tuple[str, int]
    pool_policy: str

    @classmethod
    def from_capture(cls, fix: dict) -> CreatePoolSnapshot:
        """Rebuild a `CreatePoolSnapshot` from a captured real pool create."""
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]
        outputs = [_as_utxo(u) for u in fix["outputs"]]

        pool_out = next(
            u for u in outputs if u.datum is not None and u.holds_policy(POOL_POLICY)
        )
        if pool_out.datum is None:
            raise ValueError("pool-create output is missing its datum")
        liquidity = [(p, n, q) for p, n, q in pool_out.assets if p != POOL_POLICY]
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        return cls(
            funding=inputs,
            config=config,
            pool_policy_script_ref=_script_ref_by_hash(ref_inputs, POOL_POLICY),
            pool_address=pool_out.address,
            pool_datum=pool_out.datum,
            pool_lovelace=pool_out.lovelace,
            liquidity=liquidity,
            input_ref=_mint_input_ref(fix, POOL_POLICY),
            pool_policy=POOL_POLICY,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        terms: PoolTerms,
        lender_address: str,
        pool_lovelace: int,
        liquidity: list[tuple[str, str, int]],
        pool_address: str | None = None,
        funding_outrefs: list[tuple[str, int]] | None = None,
        config_outref: tuple[str, int] | None = None,
        pool_policy_ref_outref: tuple[str, int] | None = None,
        input_ref: tuple[str, int] | None = None,
    ) -> CreatePoolSnapshot:
        """Resolve a `CreatePoolSnapshot` live from chain state via the backend.

        The lender's funding is resolved from ``funding_outrefs`` (allowing spent, for a
        captured replay) or from the unspent UTxOs at ``lender_address``; it must be
        non-empty. The config NFT and the pool policy script are resolved unless pinned
        by out-ref. The inline ``PoolDatum`` is synthesized from ``terms``.
        """
        from charli3_dendrite.lending.fluidtokens.transactions.datum_synth import (
            synth_pool_datum,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_funding,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_script_ref,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )

        if funding_outrefs:
            funding = [
                resolve_utxo_by_outref(backend, h, i, allow_spent=True)
                for h, i in funding_outrefs
            ]
        else:
            funding = resolve_funding(backend, lender_address)
        if not funding:
            raise ValueError("no funding UTxOs resolved for pool create")
        resolved_input_ref = input_ref or funding[0].out_ref
        if resolved_input_ref is None:
            raise ValueError("could not determine input_ref for pool create")

        if config_outref:
            config = resolve_utxo_by_outref(backend, *config_outref, allow_spent=True)
        else:
            config = resolve_config_utxo(backend)

        if pool_policy_ref_outref:
            pool_policy_script_ref = resolve_utxo_by_outref(
                backend,
                *pool_policy_ref_outref,
                allow_spent=True,
            )
        else:
            pool_policy_script_ref = resolve_script_ref(backend, POOL_POLICY)

        return cls(
            funding=funding,
            config=config,
            pool_policy_script_ref=pool_policy_script_ref,
            pool_address=pool_address or POOL_ADDRESS,
            pool_datum=synth_pool_datum(terms).to_cbor().hex(),
            pool_lovelace=pool_lovelace,
            liquidity=liquidity,
            input_ref=resolved_input_ref,
            pool_policy=POOL_POLICY,
        )


@dataclass
class CancelPoolSnapshot(PoolActionSnapshot):
    """Resolved building blocks for cancelling a lender pool.

    The pool UTxO is spent (empty redeemer via the pool spend script), the pool NFT is
    burned, and the pool-policy reward (``Cancel``) drives the logic -- authorized by
    the lender (its ``lenderAuth`` verification-key hash must be a required signer). The
    liquidity returns to the lender. The config NFT + the pool spend / pool policy
    scripts are reference inputs; the burn redeemer's ``input_ref`` is any spent input
    (replayed from the capture for byte-exactness).
    """

    pool: Utxo
    funding: list[Utxo]
    config: Utxo
    pool_spend_script_ref: Utxo
    pool_policy_script_ref: Utxo
    pool_id: bytes
    lender_pkh: bytes
    mint_input_ref: tuple[str, int]
    pool_policy: str

    @classmethod
    def from_capture(cls, fix: dict) -> CancelPoolSnapshot:
        """Rebuild a `CancelPoolSnapshot` from a captured real pool cancel."""
        inputs = [_as_utxo(u) for u in fix["inputs"]]
        ref_inputs = [_as_utxo(u) for u in fix["ref_inputs"]]

        pool = next(
            u
            for u in inputs
            if u.datum and _parses_pool(u.datum) and u.holds_policy(POOL_POLICY)
        )
        pool_id = next(bytes.fromhex(n) for p, n, _ in pool.assets if p == POOL_POLICY)
        funding = [u for u in inputs if u.out_ref != pool.out_ref]
        config = next(
            u for u in ref_inputs if u.holds_policy(PROTOCOL_CONFIG_NFT_POLICY)
        )
        if pool.datum is None:
            raise ValueError("cancel-pool input is missing its datum")
        datum = PoolDatum.from_cbor(bytes.fromhex(pool.datum))
        lender_pkh = bytes(datum.lender_auth.data.value[0])
        return cls(
            pool=pool,
            funding=funding,
            config=config,
            pool_spend_script_ref=_script_ref_by_hash(ref_inputs, POOL_SPEND_SKH),
            pool_policy_script_ref=_script_ref_by_hash(ref_inputs, POOL_POLICY),
            pool_id=pool_id,
            lender_pkh=lender_pkh,
            mint_input_ref=_mint_input_ref(fix, POOL_POLICY),
            pool_policy=POOL_POLICY,
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        pool_utxo: tuple[str, int],
        lender_address: str | None = None,
        allow_spent_pool: bool = False,
        funding_outrefs: list[tuple[str, int]] | None = None,
        config_outref: tuple[str, int] | None = None,
        pool_spend_ref_outref: tuple[str, int] | None = None,
        pool_policy_ref_outref: tuple[str, int] | None = None,
    ) -> CancelPoolSnapshot:
        """Resolve a `CancelPoolSnapshot` live from chain state via the backend.

        The pool UTxO is resolved by ``pool_utxo`` out-ref (``allow_spent_pool`` lets a
        captured/historical pool be replayed); its ``PoolDatum`` supplies the pool id +
        the lender vkey hash. The config NFT and the pool spend / pool policy scripts
        are resolved unless pinned by out-ref. Funding is optional for a burn-only
        cancel:
        the burn redeemer's ``input_ref`` only needs to reference a spent input, and the
        pool UTxO itself is spent, so it falls back to the pool out-ref.
        """
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_funding,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_script_ref,
        )
        from charli3_dendrite.lending.fluidtokens.transactions.resolve import (
            resolve_utxo_by_outref,
        )

        pool = resolve_utxo_by_outref(
            backend,
            *pool_utxo,
            allow_spent=allow_spent_pool,
        )
        if pool.datum is None:
            raise ValueError("resolved pool UTxO is missing its datum")
        pool_id = next(bytes.fromhex(n) for p, n, _ in pool.assets if p == POOL_POLICY)
        datum = PoolDatum.from_cbor(bytes.fromhex(pool.datum))
        lender_pkh = bytes(datum.lender_auth.data.value[0])

        if config_outref:
            config = resolve_utxo_by_outref(backend, *config_outref, allow_spent=True)
        else:
            config = resolve_config_utxo(backend)

        if pool_spend_ref_outref:
            pool_spend_script_ref = resolve_utxo_by_outref(
                backend,
                *pool_spend_ref_outref,
                allow_spent=True,
            )
        else:
            pool_spend_script_ref = resolve_script_ref(backend, POOL_SPEND_SKH)

        if pool_policy_ref_outref:
            pool_policy_script_ref = resolve_utxo_by_outref(
                backend,
                *pool_policy_ref_outref,
                allow_spent=True,
            )
        else:
            pool_policy_script_ref = resolve_script_ref(backend, POOL_POLICY)

        if funding_outrefs:
            funding = [
                resolve_utxo_by_outref(backend, h, i, allow_spent=True)
                for h, i in funding_outrefs
            ]
        elif lender_address:
            funding = resolve_funding(backend, lender_address)
        else:
            funding = []
        mint_input_ref = funding[0].out_ref if funding else pool_utxo
        if mint_input_ref is None:
            raise ValueError("could not determine mint input_ref for pool cancel")

        return cls(
            pool=pool,
            funding=funding,
            config=config,
            pool_spend_script_ref=pool_spend_script_ref,
            pool_policy_script_ref=pool_policy_script_ref,
            pool_id=pool_id,
            lender_pkh=lender_pkh,
            mint_input_ref=mint_input_ref,
            pool_policy=POOL_POLICY,
        )
