"""Protocol-agnostic, eval-only build/evaluate plumbing for lending transactions.

This is the shared seam the per-protocol lending builders reuse: a network-free
chain context (`EvalContext`) for `min_lovelace` + the validity slot, manual
(no-balancing) transaction assembly (`assemble_unsigned`), slot/out-ref helpers,
an Ogmios ``additionalUtxo`` builder, and the Ogmios ``evaluateTransaction``
client (`evaluate_tx_cbor`). Nothing here is protocol-specific and nothing signs
or submits.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from typing import Any

import requests
from pycardano import Address
from pycardano import ExecutionUnits
from pycardano import Network
from pycardano import PlutusData
from pycardano import ProtocolParameters
from pycardano import RawCBOR
from pycardano import Transaction
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionWitnessSet
from pycardano import UTxO
from pycardano.backend.base import ChainContext
from pycardano.backend.base import GenesisParameters

from charli3_dendrite.utility import MAINNET_SLOT_ANCHOR
from charli3_dendrite.utility import MAINNET_SLOT_ANCHOR_MS

if TYPE_CHECKING:
    from charli3_dendrite.backend.backend_base import AbstractBackend

# Lovelace placed on outputs whose value Ogmios evaluation does not balance: a
# deliberate over-estimate of each output's min-UTxO (callers additionally floor
# to the protocol min via `min_lovelace`); it is not a balanced on-chain amount.
OUTPUT_MIN_ADA = 5_000_000

# Ogmios computes the real execution units; the redeemers only need a syntactically
# valid placeholder so the transaction serializes.
PLACEHOLDER_FEE = 2_000_000

# Mainnet protocol parameters needed only for `min_lovelace` (the loan output floor)
# and the script-data hash; the no-Kupo evaluation path never balances or selects
# inputs, so the remaining fields are nominal mainnet values.
_COINS_PER_UTXO_BYTE = 4310

# Funding ADA attached to the synthesized borrower input. Ogmios evaluation never
# checks value balance, so this only needs to comfortably exceed every output's
# min-UTxO; it is not the borrower UTxO's real lovelace.
_BORROWER_LOVELACE = 600_000_000_000

_DEFAULT_OGMIOS_PORT = "1337"
_EVAL_TIMEOUT_S = 60


class EvalContext(ChainContext):
    """Network-free chain context for no-Kupo manual assembly + Ogmios evaluation.

    The forward builder needs a context only for `min_lovelace` (the loan output's
    ADA floor) and the validity slot; it never queries UTxOs, balances, or submits
    (Ogmios `evaluateTransaction` resolves the live inputs itself). `last_block_slot`
    is supplied from the backend's current tip so the validity window is current.
    """

    def __init__(self, *, last_block_slot: int) -> None:
        """Pin the validity-window slot from the backend's current tip."""
        self._last_block_slot = last_block_slot

    @property
    def protocol_param(self) -> ProtocolParameters:
        """Nominal mainnet protocol parameters (only `min_lovelace` reads them)."""
        return ProtocolParameters(
            min_fee_constant=155381,
            min_fee_coefficient=44,
            max_block_size=98304,
            max_tx_size=16384,
            max_block_header_size=1100,
            key_deposit=2000000,
            pool_deposit=500000000,
            pool_influence=0.3,
            monetary_expansion=0.003,
            treasury_expansion=0.2,
            decentralization_param=0,
            extra_entropy="",
            protocol_major_version=10,
            protocol_minor_version=0,
            min_utxo=1000000,
            min_pool_cost=170000000,
            price_mem=0.0577,
            price_step=0.0000721,
            max_tx_ex_mem=14000000,
            max_tx_ex_steps=10000000000,
            max_block_ex_mem=62000000,
            max_block_ex_steps=20000000000,
            max_val_size=5000,
            collateral_percent=150,
            max_collateral_inputs=3,
            coins_per_utxo_word=34482,
            coins_per_utxo_byte=_COINS_PER_UTXO_BYTE,
            cost_models={},
        )

    @property
    def genesis_param(self) -> GenesisParameters | None:
        """No genesis parameters: the eval-only path never needs them."""
        return None

    @property
    def network(self) -> Network:
        """Mainnet (the only network this builder targets)."""
        return Network.MAINNET

    @property
    def epoch(self) -> int:
        """Nominal epoch (unused by the eval-only path)."""
        return 0

    @property
    def last_block_slot(self) -> int:
        """Current tip slot supplied at construction (drives the validity window)."""
        return self._last_block_slot

    def utxos(self, address: str | Address) -> list[UTxO]:
        """No UTxO queries: manual assembly does no input selection."""
        return []

    def submit_tx_cbor(self, cbor: bytes | str) -> str:
        """Unsupported: this context evaluates only and never submits."""
        raise NotImplementedError("evaluation-only context never submits")

    def evaluate_tx_cbor(self, cbor: bytes | str) -> dict[str, ExecutionUnits]:
        """Unsupported: use the module-level `evaluate_tx_cbor` against Ogmios."""
        raise NotImplementedError("use evaluate_tx_cbor against Ogmios")


def current_slot(backend: AbstractBackend) -> int:
    """Current mainnet absolute slot from the backend's latest block time."""
    blocks = backend.last_block(last_n_blocks=1)
    block_time = blocks.root[0].block_time  # POSIX seconds of the tip block
    return MAINNET_SLOT_ANCHOR + (block_time - MAINNET_SLOT_ANCHOR_MS // 1000)


def parse_out_ref(out_ref: str) -> TransactionInput:
    """Parse a ``tx_hash#index`` string into a `TransactionInput`."""
    tx_hash, _, idx = out_ref.partition("#")
    return TransactionInput(
        transaction_id=TransactionId(bytes.fromhex(tx_hash)),
        index=int(idx),
    )


def additional_utxo_for_input(
    out_ref: str,
    actor_address: str,
    collateral: dict[str, int],
) -> dict[str, Any]:
    """Ogmios ``additionalUtxo`` entry resolving the borrower (funding) input.

    The borrower input funds the loan and is a plain (datum-less) wallet UTxO that is
    not part of the live ledger snapshot Ogmios evaluates against (it may already be
    spent), so it must be supplied explicitly. Its value carries the locked collateral
    plus ample ADA; Plutus evaluation resolves but does not balance it.
    """
    tx_hash, _, idx = out_ref.partition("#")
    value: dict[str, Any] = {"ada": {"lovelace": _BORROWER_LOVELACE}}
    for unit, qty in collateral.items():
        if unit == "lovelace":
            value["ada"]["lovelace"] += qty
            continue
        value.setdefault(unit[:56], {})[unit[56:]] = qty
    return {
        "transaction": {"id": tx_hash},
        "index": int(idx),
        "address": actor_address,
        "value": value,
    }


def assemble_unsigned(tx_builder: TransactionBuilder) -> str:
    """Assemble an unsigned, eval-only `Transaction` from `tx_builder` -> CBOR hex.

    Manual, eval-only assembly: there is no chain context to balance against (no
    Kupo), and Ogmios `evaluateTransaction` only resolves inputs + runs scripts, so
    we drive the builder's redeemer indexing and body construction directly rather
    than calling `TransactionBuilder.build()` (which requires a balancing context).
    NOTE: this couples to pycardano private internals (`_set_redeemer_index`,
    `_redeemer_list`, `_build_tx_body`); revisit on a pycardano upgrade.

    Order inputs canonically (tx id, index) so the SPEND redeemer index matches the
    position the Plutus script context exposes, then let the builder index + collect
    the redeemers and assemble the body/witness set manually (no balancing).
    """
    tx_builder.inputs.sort(
        key=lambda u: (bytes(u.input.transaction_id), u.input.index),
    )
    tx_builder._set_redeemer_index()  # - reuse the builder's indexing

    # The oracle redeemer is a plain dataclass (custom `to_cbor`), not a pycardano
    # `PlutusData`, so wrap its exact bytes as `RawCBOR` for serialization; `CreateLoan`
    # is already `PlutusData`.
    for r in tx_builder._redeemer_list:  # - finalize redeemer payloads
        if not isinstance(r.data, PlutusData):
            r.data = RawCBOR(r.data.to_cbor())

    body = tx_builder._build_tx_body()  # - manual assembly (no chain ctx)
    witness = TransactionWitnessSet(redeemer=tx_builder.redeemers())
    tx = Transaction(body, witness)
    return tx.to_cbor_hex()


def ogmios_http_url() -> str:
    """Ogmios HTTP JSON-RPC endpoint from ``OGMIOS_HOST`` / ``OGMIOS_PORT``."""
    host = os.environ["OGMIOS_HOST"]
    port = os.environ.get("OGMIOS_PORT", _DEFAULT_OGMIOS_PORT)
    return f"http://{host}:{port}"


def evaluate_tx_cbor(
    tx_cbor: str,
    additional_utxo: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return Ogmios per-redeemer execution budgets, or raise on validation failure."""
    payload = {
        "jsonrpc": "2.0",
        "method": "evaluateTransaction",
        "params": {
            "transaction": {"cbor": tx_cbor},
            "additionalUtxo": additional_utxo,
        },
    }
    resp = requests.post(ogmios_http_url(), json=payload, timeout=_EVAL_TIMEOUT_S)
    body = resp.json()
    if "error" in body:
        raise AssertionError(f"ogmios evaluate error: {body['error']}")
    return body["result"]
