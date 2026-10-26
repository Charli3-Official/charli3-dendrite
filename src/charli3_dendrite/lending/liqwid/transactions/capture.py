"""A captured Liqwid V2 transaction read back into the pieces a snapshot holds.

A capture is the record of a transaction already on chain: its spent inputs,
outputs and reference inputs as :class:`~charli3_dendrite.lending.transactions.utxos.
Utxo` dictionaries (``address, lovelace, assets, datum, ref_script, ref_script_type,
out_ref``), its mints as ``[policy, name, quantity]``, its required signers and its
validity interval in slots (``invalid_before``, ``invalid_hereafter``).
:class:`CapturedTx` decodes it once and answers what a builder snapshot's
``from_capture`` needs: the market records and oracle feeds among the reference
inputs, the reference scripts by hash, the market whose qTokens it mints or burns,
the action UTxOs and loans it spends and writes, the style of the datums it writes,
and the wallet inputs that funded it.

The records and feeds are assembled by the same functions live resolution uses
(:func:`~charli3_dendrite.lending.liqwid.transactions.resolve.market_records`,
:func:`~charli3_dendrite.lending.liqwid.transactions.resolve.health_inputs`), over
the reference inputs instead of a backend read.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from charli3_dendrite.lending.liqwid.datums import datum_style
from charli3_dendrite.lending.liqwid.deployment import LiqwidDeployment
from charli3_dendrite.lending.liqwid.deployment import resolve_deployment
from charli3_dendrite.lending.liqwid.deployment import staking_roles
from charli3_dendrite.lending.liqwid.indexing import LiqwidKind
from charli3_dendrite.lending.liqwid.indexing import action_selector
from charli3_dendrite.lending.liqwid.indexing import parse_utxo
from charli3_dendrite.lending.liqwid.oracles import parse_feed
from charli3_dendrite.lending.liqwid.transactions.resolve import ActionUtxo
from charli3_dendrite.lending.liqwid.transactions.resolve import LiqwidScriptRefs
from charli3_dendrite.lending.liqwid.transactions.resolve import LoanRecord
from charli3_dendrite.lending.liqwid.transactions.resolve import action_record
from charli3_dendrite.lending.liqwid.transactions.resolve import health_inputs
from charli3_dendrite.lending.liqwid.transactions.resolve import loan_record
from charli3_dendrite.lending.liqwid.transactions.resolve import loan_script_refs
from charli3_dendrite.lending.liqwid.transactions.resolve import market_records
from charli3_dendrite.lending.transactions.utxos import Utxo
from charli3_dendrite.lending.transactions.utxos import script_ref_by_hash
from charli3_dendrite.lending.transactions.utxos import utxo_from_dict
from charli3_dendrite.lending.transactions.utxos import utxo_to_info
from charli3_dendrite.utility import slot_to_posix_ms

if TYPE_CHECKING:
    from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
    from charli3_dendrite.lending.liqwid.deployment import MarketComponents
    from charli3_dendrite.lending.liqwid.indexing import LiqwidRecord
    from charli3_dendrite.lending.liqwid.oracles import LiqwidFeed
    from charli3_dendrite.lending.liqwid.transactions.resolve import HealthInputs
    from charli3_dendrite.lending.liqwid.transactions.resolve import LoanScriptRefs
    from charli3_dendrite.lending.liqwid.transactions.resolve import MarketRecords

__all__ = ["CapturedTx", "sole"]

_T = TypeVar("_T")

# The script each LiqwidScriptRefs field carries, as a refusal names it.
_SCRIPT_NAMES = {
    "action": "action validator",
    "qtoken_policy": "qToken policy",
    "loan_validator": "loan validator",
    "loan_policy": "loan-token policy",
    "borrow_staking": "borrow staking script",
    "liquidation_staking": "liquidation staking script",
}


def sole(items: Sequence[_T], what: str) -> _T:
    """The one member of ``items``.

    Raises:
        ValueError: ``items`` does not hold exactly one ``what``.
    """
    if len(items) != 1:
        msg = f"expected one {what} in the capture, found {len(items)}"
        raise ValueError(msg)
    return items[0]


@dataclass(frozen=True)
class CapturedTx:
    """A captured transaction, decoded against a Liqwid deployment.

    ``records`` are the routed Liqwid records among the reference inputs and
    ``feeds`` the genuine oracle feeds among them; ``mints`` are the minted (and,
    negative, burnt) ``(policy, name, quantity)`` and ``required_signers`` the key
    hashes (hex) the transaction requires; ``tx_hash`` names the transaction in
    a refusal.
    """

    inputs: tuple[Utxo, ...]
    outputs: tuple[Utxo, ...]
    references: tuple[Utxo, ...]
    invalid_before: int | None
    invalid_hereafter: int | None
    deployment: LiqwidDeployment
    records: tuple[LiqwidRecord, ...]
    feeds: tuple[LiqwidFeed, ...]
    mints: tuple[tuple[str, str, int], ...] = ()
    required_signers: tuple[str, ...] = ()
    tx_hash: str = ""

    @classmethod
    def of(
        cls,
        fix: Mapping[str, Any],
        *,
        deployment: LiqwidDeployment | None = None,
    ) -> CapturedTx:
        """Decode the capture ``fix`` against ``deployment`` (the bundled one)."""
        active = resolve_deployment(deployment)
        references = tuple(utxo_from_dict(u) for u in fix["ref_inputs"])
        infos = [utxo_to_info(u) for u in references]
        return cls(
            inputs=tuple(utxo_from_dict(u) for u in fix["inputs"]),
            outputs=tuple(utxo_from_dict(u) for u in fix["outputs"]),
            references=references,
            invalid_before=fix["invalid_before"],
            invalid_hereafter=fix["invalid_hereafter"],
            deployment=active,
            records=tuple(
                record
                for info in infos
                if (record := parse_utxo(info, active)) is not None
            ),
            feeds=tuple(
                feed for info in infos if (feed := parse_feed(info, active)) is not None
            ),
            mints=tuple((p, n, int(q)) for p, n, q in fix.get("mints", ())),
            required_signers=tuple(fix.get("required_signers", ())),
            tx_hash=fix.get("tx_hash", ""),
        )

    @property
    def valid_to_ms(self) -> int | None:
        """The upper validity bound in POSIX milliseconds, None when unbounded."""
        if self.invalid_hereafter is None:
            return None
        return slot_to_posix_ms(self.invalid_hereafter)

    def upper_bound_ms(self) -> int:
        """The upper validity bound, which every action-spending flow carries.

        Raises:
            ValueError: The capture has no upper bound.
        """
        upper = self.valid_to_ms
        if upper is None:
            msg = "the captured transaction has no upper validity bound"
            raise ValueError(msg)
        return upper

    @property
    def valid_from_ms(self) -> int | None:
        """The lower validity bound in POSIX milliseconds, None when unbounded."""
        if self.invalid_before is None:
            return None
        return slot_to_posix_ms(self.invalid_before)

    def market_records(self, market: MarketComponents) -> MarketRecords:
        """``market``'s records among the reference inputs (:func:`market_records`)."""
        return market_records(self.records, market)

    def health(self, units: Iterable[str], *, borrow: MarketRecords) -> HealthInputs:
        """The health inputs of ``units`` among the reference inputs."""
        return health_inputs(
            units,
            borrow=borrow,
            records=self.records,
            feeds=self.feeds,
            deployment=self.deployment,
        )

    def script(self, script_hash: str) -> Utxo | None:
        """The reference input carrying ``script_hash``, None when none does."""
        try:
            return script_ref_by_hash(list(self.references), script_hash)
        except ValueError:
            return None

    def script_refs(
        self,
        market: MarketComponents,
        params: MarketParamsDatum,
        *,
        require: Collection[str] = ("action",),
    ) -> LiqwidScriptRefs:
        """The market's scripts the transaction references; the others are None.

        ``require`` names the fields of
        :class:`~charli3_dendrite.lending.liqwid.transactions.resolve.LiqwidScriptRefs`
        the transaction must reference (the action validator by default).

        Raises:
            ValueError: No reference input carries a required script.
        """
        borrow = liquidation = None
        if params.withdraw_zero is not None:
            roles = staking_roles(market, params)
            borrow, liquidation = roles.borrow, roles.liquidation
        refs = LiqwidScriptRefs(
            action=self._optional_script(params.action_validator),
            qtoken_policy=self.script(market.qtoken_policy),
            loan_validator=self._optional_script(params.loan_validator),
            loan_policy=self._optional_script(market.new_borrow_policy),
            borrow_staking=self._optional_script(borrow),
            liquidation_staking=self._optional_script(liquidation),
        )
        for field in require:
            if getattr(refs, field) is None:
                msg = (
                    f"the capture references no {_SCRIPT_NAMES[field]} of {market.name}"
                )
                raise ValueError(msg)
        return refs

    def loan_script_refs(
        self,
        market: MarketComponents,
        params: MarketParamsDatum,
    ) -> LoanScriptRefs:
        """The market's owner loan scripts among the reference inputs.

        Raises:
            UnsupportedMarketError: The market is unfit for loan flows.
            ValueError: No reference input carries one of the scripts.
        """
        return loan_script_refs(self.references, market, params)

    def qtoken_market(self) -> MarketComponents:
        """The one market whose qTokens the transaction mints or burns.

        Raises:
            ValueError: It mints or burns the qTokens of no market, or of several.
        """
        markets = {
            found
            for policy, _, _ in self.mints
            if (found := self.deployment.market_of_qtoken(policy)) is not None
        }
        if len(markets) != 1:
            msg = f"capture {self.tx_hash} does not mint or burn one market's qTokens"
            raise ValueError(msg)
        (market,) = markets
        return market

    def _optional_script(self, script_hash: str | None) -> Utxo | None:
        return self.script(script_hash) if script_hash else None

    def action_inputs(self, market: MarketComponents) -> list[ActionUtxo]:
        """The spent action UTxOs of ``market``, in input order (`_actions`).

        Raises:
            UnsupportedMarketError: As `_actions`.
        """
        return self._actions(self.inputs, market)

    def action_outputs(self, market: MarketComponents) -> list[ActionUtxo]:
        """The re-created action UTxOs of ``market``, in output order (`_actions`).

        Raises:
            UnsupportedMarketError: As `_actions`.
        """
        return self._actions(self.outputs, market)

    def _actions(
        self,
        utxos: Iterable[Utxo],
        market: MarketComponents,
    ) -> list[ActionUtxo]:
        """The members of ``utxos`` that are action UTxOs of ``market``.

        A UTxO counts as live resolution counts it (:func:`action_record`): at the
        action validator the market's referenced MarketParams name, holding the
        market's action token and an action record.

        Raises:
            UnsupportedMarketError: The capture references no MarketParams or
                MarketState of ``market``, or they name no action validator.
        """
        params = self.market_records(market).params_datum
        selector = action_selector(market, params)
        return [
            ActionUtxo(utxo=u, datum=record.datum)
            for u in utxos
            if (record := action_record(utxo_to_info(u), market, selector)) is not None
        ]

    def loan_inputs(self) -> list[LoanRecord]:
        """The current-generation loans spent, in input order."""
        return self._loans(self.inputs)

    def loan_outputs(self) -> list[LoanRecord]:
        """The current-generation loans written, in output order."""
        return self._loans(self.outputs)

    def _loans(self, utxos: Iterable[Utxo]) -> list[LoanRecord]:
        return [
            loan_record(u, deployment=self.deployment)
            for u in utxos
            if (record := parse_utxo(utxo_to_info(u), self.deployment)) is not None
            and record.kind is LiqwidKind.LOAN
        ]

    def indefinite(self, *written: Utxo | None) -> bool:
        """True when a datum of ``written`` (outputs; None skipped) is indefinite.

        The captured array style, which a replay pins.
        """
        return any(
            u is not None and u.datum and datum_style(u.datum) == "indefinite"
            for u in written
        )

    def funding(self, *spent: Utxo) -> list[Utxo]:
        """The inputs other than ``spent`` (the protocol UTxOs), in input order."""
        protocol = {u.out_ref for u in spent}
        return [u for u in self.inputs if u.out_ref not in protocol]

    def wallet_address(self, *protocol: Utxo) -> str:
        """The address of the first input, else output, outside ``protocol``.

        A transaction funded from the borrowed amount alone spends no wallet input;
        its first other output is the wallet's.

        Raises:
            ValueError: Every input and output is a ``protocol`` UTxO.
        """
        known = {u.out_ref for u in protocol}
        for u in (*self.inputs, *self.outputs):
            if u.out_ref not in known:
                return u.address
        msg = "the captured transaction has no wallet input or output"
        raise ValueError(msg)
