"""FluidTokens V4 repayment claim: a lender collects the repayments of their loans.

A repayment lands at the asset manager, owned by the loan's lender bond, and the asset
manager releases it to any transaction that spends that bond. Lender bonds sit at the
lender manager, which lets them move only under an action named by its dispatch; the
``WithdrawBonds`` action requires every spent bond's lender to sign. A claim spends
the lender's bonds and the repayments they own, returns each bond to the lender
manager unchanged (so its automation keeps serving later repayments), and leaves the
repayments, with any repayment receipts they carry, to the caller's change.

The asset manager checks each repayment against every input of the transaction, so a
claim's execution budget grows with repayments times inputs: a claim spends at most
:data:`MAX_REPAYMENTS_PER_CLAIM` repayments, and more take several claims.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycardano import Address
from pycardano import Redeemer
from pycardano import ScriptHash
from pycardano import TransactionBuilder
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.fluidtokens.transactions._common import ref_index
from charli3_dendrite.lending.fluidtokens.transactions._common import reward_address
from charli3_dendrite.lending.fluidtokens.transactions.utxos import Utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import ogmios_entry
from charli3_dendrite.lending.fluidtokens.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.fluidtokens.transactions.utxos import to_pycardano_utxo
from charli3_dendrite.lending.fluidtokens.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.fluidtokens_v4 import constants as c
from charli3_dendrite.lending.fluidtokens_v4.datums import AssetManagerDatumWithToken
from charli3_dendrite.lending.fluidtokens_v4.datums import ConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerConfigDatum
from charli3_dendrite.lending.fluidtokens_v4.datums import LenderManagerDatum
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import (
    add_zero_withdrawals,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import ledger_order
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import min_ada
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import out_ref_of
from charli3_dendrite.lending.fluidtokens_v4.transactions.common import signing_key
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    AssetManagerWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LenderManagerActionWithdrawBonds,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LenderManagerWithdrawRedeemer,
)
from charli3_dendrite.lending.fluidtokens_v4.transactions.redeemers import (
    LoanSpendRedeemer,
)
from charli3_dendrite.lending.transactions.snapshot import PoolActionSnapshot

if TYPE_CHECKING:
    from pycardano import TransactionOutput

    from charli3_dendrite.backend.backend_base import AbstractBackend

# The most repayments one claim spends: this leaves room for the lender-manager and
# wallet inputs within the transaction's execution budget.
MAX_REPAYMENTS_PER_CLAIM = 10


def _payment_script(utxo: Utxo) -> str | None:
    """The script hash of ``utxo``'s payment credential, or None for a key address."""
    payment = Address.decode(utxo.address).payment_part
    return payment.payload.hex() if isinstance(payment, ScriptHash) else None


@dataclass
class ClaimPosition:
    """A UTxO holding lender bonds at the lender manager and the repayments it owns."""

    bond: Utxo
    repayments: list[Utxo]

    @property
    def out_ref(self) -> tuple[str, int]:
        """The bond UTxO's out-ref."""
        return out_ref_of(self.bond)

    @property
    def lender_datum(self) -> LenderManagerDatum:
        """The bond UTxO's lender-manager datum."""
        if self.bond.datum is None:
            raise ValueError("lender-bond UTxO is missing its datum")
        return LenderManagerDatum.from_cbor(self.bond.datum)

    @property
    def bond_names(self) -> list[bytes]:
        """The lender bonds the UTxO holds; each is named after its loan."""
        return [
            bytes.fromhex(n)
            for p, n, q in self.bond.assets
            if p == c.LENDER_BOND_POLICY
        ]

    @property
    def owner_pkh(self) -> bytes:
        """The key hash that must sign to move the bonds."""
        return signing_key(self.lender_datum.lender_auth, "lender bond")

    def check(self) -> None:
        """Raise unless the bonds sit at the lender manager and own every repayment."""
        if _payment_script(self.bond) != c.LENDER_MANAGER_SPEND_SKH:
            raise ValueError(f"UTxO {self.out_ref} is not at the lender manager")
        if not self.bond_names:
            raise ValueError(f"UTxO {self.out_ref} holds no lender bond")
        if not self.repayments:
            raise ValueError(f"lender bond UTxO {self.out_ref} owns no repayment")
        names = set(self.bond_names)
        for repayment in self.repayments:
            owner = AssetManagerDatumWithToken.from_cbor(repayment.datum or "")
            if (
                bytes(owner.owner_asset.policy_id).hex() != c.LENDER_BOND_POLICY
                or bytes(owner.owner_asset.asset_name) not in names
            ):
                raise ValueError(
                    f"lender bond UTxO {self.out_ref} does not own repayment "
                    f"{out_ref_of(repayment)}",
                )


@dataclass
class ClaimSnapshot(PoolActionSnapshot):
    """Everything a claim of one lender's repayments needs, resolved from chain."""

    positions: Sequence[ClaimPosition]
    funding: list[Utxo]
    config: Utxo
    lender_manager_config: Utxo
    lender_manager_spend_script_ref: Utxo
    lender_manager_withdraw_script_ref: Utxo
    withdraw_bonds_script_ref: Utxo
    asset_manager_spend_script_ref: Utxo
    asset_manager_withdraw_script_ref: Utxo

    @property
    def ordered_positions(self) -> list[ClaimPosition]:
        """The positions in the order the ledger sorts their bond inputs."""
        return sorted(
            self.positions,
            key=lambda p: (bytes.fromhex(p.out_ref[0]), p.out_ref[1]),
        )

    @property
    def withdraw_bonds_script_hash(self) -> str:
        """The lender manager's ``WithdrawBonds`` action, named by its config."""
        if self.lender_manager_config.datum is None:
            raise ValueError("lender-manager config UTxO is missing its datum")
        return LenderManagerConfigDatum.from_cbor(
            self.lender_manager_config.datum,
        ).withdraw_bonds_action_script_hash.hex()

    @classmethod
    def from_capture(cls, fix: dict) -> ClaimSnapshot:
        """Rebuild the snapshot of a captured mainnet claim (for byte-exact replay)."""
        inputs = [utxo_from_dict(u) for u in fix["inputs"]]
        refs = [utxo_from_dict(u) for u in fix["ref_inputs"]]
        config = next(
            u for u in refs if u.holds(c.CONFIG_NFT_POLICY, c.CONFIG_NFT_NAME)
        )
        lender_manager_config = next(
            u
            for u in refs
            if u.holds(
                c.LENDER_MANAGER_CONFIG_NFT_POLICY,
                c.LENDER_MANAGER_CONFIG_NFT_NAME,
            )
        )
        scripts = ConfigDatum.from_cbor(config.datum or "")
        am_spend = scripts.asset_manager_spend_script_hash.hex()
        repayments = [u for u in inputs if _payment_script(u) == am_spend]
        positions = []
        for bond in ledger_order(
            u for u in inputs if _payment_script(u) == c.LENDER_MANAGER_SPEND_SKH
        ):
            names = {n for p, n, _ in bond.assets if p == c.LENDER_BOND_POLICY}
            positions.append(
                ClaimPosition(
                    bond=bond,
                    repayments=[
                        u
                        for u in repayments
                        if bytes(
                            AssetManagerDatumWithToken.from_cbor(
                                u.datum or "",
                            ).owner_asset.asset_name,
                        ).hex()
                        in names
                    ],
                ),
            )
        used = {out_ref_of(u) for p in positions for u in (p.bond, *p.repayments)}
        withdraw_bonds = LenderManagerConfigDatum.from_cbor(
            lender_manager_config.datum or "",
        ).withdraw_bonds_action_script_hash.hex()
        return cls(
            positions=positions,
            funding=[u for u in inputs if u.out_ref not in used],
            config=config,
            lender_manager_config=lender_manager_config,
            lender_manager_spend_script_ref=script_ref_by_hash(
                refs,
                c.LENDER_MANAGER_SPEND_SKH,
            ),
            lender_manager_withdraw_script_ref=script_ref_by_hash(
                refs,
                c.LENDER_MANAGER_WITHDRAW_SKH,
            ),
            withdraw_bonds_script_ref=script_ref_by_hash(refs, withdraw_bonds),
            asset_manager_spend_script_ref=script_ref_by_hash(refs, am_spend),
            asset_manager_withdraw_script_ref=script_ref_by_hash(
                refs,
                scripts.repayment_policy_id.hex(),
            ),
        )

    @classmethod
    def from_backend(
        cls,
        backend: AbstractBackend,
        *,
        lender_address: str,
        bonds: Collection[bytes] | None = None,
        funding: Sequence[Utxo] | None = None,
    ) -> ClaimSnapshot:
        """Resolve a claim of the repayments owed to ``lender_address``'s bonds.

        The bonds are the lender bonds at the lender manager whose lender
        authorisation is the key of ``lender_address``; the repayments are the
        asset-manager UTxOs they own, the largest first, at most
        :data:`MAX_REPAYMENTS_PER_CLAIM` of them. ``bonds`` (loan ids, which name the
        bonds) limits the claim to those bonds. The lender's UTxOs fund the claim
        unless ``funding`` is given. Refuses a lender with nothing to claim, a named
        bond that is not the lender's at the lender manager, and a wallet with no
        UTxO to fund the claim.
        """
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            config_datum,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            lender_manager_config_datum,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_claim_positions,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_lender_manager_config_utxo,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_script,
        )
        from charli3_dendrite.lending.fluidtokens_v4.transactions.resolve import (
            resolve_wallet_funding,
        )

        payment = Address.decode(lender_address).payment_part
        if not isinstance(payment, VerificationKeyHash):
            raise NotImplementedError("only a key address can claim repayments")
        config = resolve_config_utxo(backend)
        scripts = config_datum(config)
        lender_manager_config = resolve_lender_manager_config_utxo(backend)
        positions = resolve_claim_positions(
            backend,
            scripts,
            lender_pkh=payment.payload,
            bonds=bonds,
        )
        if not positions:
            raise ValueError(f"{lender_address} has no repayments to claim")
        spent = [u for p in positions for u in (p.bond, *p.repayments)]
        resolved = (
            list(funding)
            if funding is not None
            else resolve_wallet_funding(backend, lender_address, exclude=spent)
        )
        if funding is None and not resolved:
            raise ValueError(f"no UTxOs at {lender_address} fund the claim")
        return cls(
            positions=positions,
            funding=resolved,
            config=config,
            lender_manager_config=lender_manager_config,
            lender_manager_spend_script_ref=resolve_script(
                backend,
                c.LENDER_MANAGER_SPEND_SKH,
            ),
            lender_manager_withdraw_script_ref=resolve_script(
                backend,
                c.LENDER_MANAGER_WITHDRAW_SKH,
            ),
            withdraw_bonds_script_ref=resolve_script(
                backend,
                lender_manager_config_datum(
                    lender_manager_config,
                ).withdraw_bonds_action_script_hash,
            ),
            asset_manager_spend_script_ref=resolve_script(
                backend,
                scripts.asset_manager_spend_script_hash,
            ),
            asset_manager_withdraw_script_ref=resolve_script(
                backend,
                scripts.repayment_policy_id,
            ),
        )


def require_sole_lender_manager_action(tx_builder: TransactionBuilder) -> None:
    """Raise if ``tx_builder`` already spends from or dispatches the lender manager.

    ``WithdrawBonds`` requires the lender of every spent lender-manager UTxO to sign,
    and one dispatch redeemer names one action for the whole transaction.
    """
    spends = any(
        isinstance(u.output.address.payment_part, ScriptHash)
        and u.output.address.payment_part.payload.hex() == c.LENDER_MANAGER_SPEND_SKH
        for u in tx_builder.inputs
    )
    dispatches = reward_address(c.LENDER_MANAGER_WITHDRAW_SKH) in (
        tx_builder.withdrawals or {}
    )
    if spends or dispatches:
        raise ValueError(
            "a claim must be the only lender-manager action in its transaction",
        )


def build_claim(tx_builder: TransactionBuilder, *, snapshot: ClaimSnapshot) -> None:
    """Add a claim of every repayment of ``snapshot`` to ``tx_builder``.

    Each bond UTxO returns to the lender manager unchanged; the repayments land in
    the caller's change. Funding inputs are recorded for Ogmios evaluation. The
    caller balances, signs (with every bond's lender key) and submits.

    Everything else the caller wants among the transaction's reference inputs must
    be added before this call, which fills the config indexes last. Discard the
    builder if this raises.
    """
    require_sole_lender_manager_action(tx_builder)
    positions = snapshot.ordered_positions
    _check_claim(positions)
    _add_spends(tx_builder, snapshot=snapshot, positions=positions)

    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.config))
    tx_builder.reference_inputs.add(to_pycardano_utxo(snapshot.lender_manager_config))
    dispatch = LenderManagerWithdrawRedeemer(
        config_ref_input_index=0,
        action=LenderManagerActionWithdrawBonds(),
    )
    release = AssetManagerWithdrawRedeemer(config_ref_input_index=0)
    for script_ref, redeemer in (
        (snapshot.lender_manager_withdraw_script_ref, dispatch),
        # The action reads no redeemer.
        (snapshot.withdraw_bonds_script_ref, LoanSpendRedeemer()),
        (snapshot.asset_manager_withdraw_script_ref, release),
    ):
        tx_builder.add_withdrawal_script(
            to_pycardano_utxo(script_ref),
            Redeemer(redeemer),
        )
    add_zero_withdrawals(
        tx_builder,
        [
            c.LENDER_MANAGER_WITHDRAW_SKH,
            snapshot.withdraw_bonds_script_hash,
            bytes(
                ConfigDatum.from_cbor(snapshot.config.datum or "").repayment_policy_id,
            ).hex(),
        ],
    )
    signers = list(tx_builder.required_signers or [])
    for signer in dict.fromkeys(VerificationKeyHash(p.owner_pkh) for p in positions):
        if signer not in signers:
            signers.append(signer)
    tx_builder.required_signers = signers
    for position in positions:
        tx_builder.add_output(_returned_bond(position))

    refs = ref_index(tx_builder)
    dispatch.config_ref_input_index = refs[out_ref_of(snapshot.lender_manager_config)]
    release.config_ref_input_index = refs[out_ref_of(snapshot.config)]


def _check_claim(positions: Sequence[ClaimPosition]) -> None:
    """Raise for a claim the contracts or the transaction budget would refuse."""
    if not positions:
        raise ValueError("a claim needs at least one lender bond")
    if sum(len(p.repayments) for p in positions) > MAX_REPAYMENTS_PER_CLAIM:
        raise ValueError(
            f"a claim spends at most {MAX_REPAYMENTS_PER_CLAIM} repayments; claim the "
            "rest in another transaction",
        )
    for position in positions:
        position.check()
    spent = [out_ref_of(u) for p in positions for u in (p.bond, *p.repayments)]
    if len(set(spent)) != len(spent):
        raise ValueError("a claim names each bond and repayment once")


def _add_spends(
    tx_builder: TransactionBuilder,
    *,
    snapshot: ClaimSnapshot,
    positions: Sequence[ClaimPosition],
) -> None:
    """Spend the bonds, their repayments and the funding not among them."""
    for position in positions:
        tx_builder.add_script_input(
            to_pycardano_utxo(position.bond),
            script=to_pycardano_utxo(snapshot.lender_manager_spend_script_ref),
            redeemer=Redeemer(LoanSpendRedeemer()),
        )
        for repayment in position.repayments:
            tx_builder.add_script_input(
                to_pycardano_utxo(repayment),
                script=to_pycardano_utxo(snapshot.asset_manager_spend_script_ref),
                redeemer=Redeemer(LoanSpendRedeemer()),
            )
    spent = {out_ref_of(u) for p in positions for u in (p.bond, *p.repayments)}
    for funding in snapshot.funding:
        if funding.out_ref not in spent:
            tx_builder.add_input(to_pycardano_utxo(funding))
            snapshot.add_actor_additional_utxo(ogmios_entry(funding))


def _returned_bond(position: ClaimPosition) -> TransactionOutput:
    """The bond UTxO back at the lender manager, as spent.

    ``WithdrawBonds`` checks no output, so a bond whose ADA no longer covers its
    output's minimum is topped up to it.
    """
    output = to_pycardano_utxo(position.bond).output
    output.amount.coin = max(output.amount.coin, min_ada(output))
    return output
