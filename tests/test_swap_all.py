"""Swap-all orders: an order that swaps whatever amount of its input token it
holds when executed.

An order funded by another order's output has no exact input amount when its
datum is built. Minswap V2 expresses "swap everything held" with
``SAOAll(deducted_amount)``. A SundaeSwap V3 stableswap order offers more than it
holds, and the pool swaps the smaller of the offer and the held input. Every
other order datum fixes the input amount, so asking it for a swap-all order
raises ``ValueError`` rather than silently building a fixed-amount datum.

Pure-unit: pools are built with ``model_construct`` (no backend).
"""

from __future__ import annotations

import inspect

import pytest
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.amm_base import AbstractPoolState
from charli3_dendrite.dexs.amm.minswap import MinswapV2CPPState
from charli3_dendrite.dexs.amm.minswap import MinswapV2OrderDatum
from charli3_dendrite.dexs.amm.minswap import SAOAll
from charli3_dendrite.dexs.amm.minswap import SAOSpecificAmount
from charli3_dendrite.dexs.amm.minswap import SwapExactInV2
from charli3_dendrite.dexs.amm.sundae import SundaeSwapV3CPPState
from charli3_dendrite.dexs.amm.sundae import SundaeSwapV3StableSwap
from charli3_dendrite.dexs.amm.sundae import SundaeV3OrderDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverInlineDatum
from charli3_dendrite.dexs.amm.sundae import SundaeV3ReceiverInlineDatumHash
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

_SRC = Address(
    VerificationKeyHash(b"\x11" * 28),
    VerificationKeyHash(b"\x22" * 28),
    network=Network.MAINNET,
)
_POLICY = "a" * 56
_NAME = "deadbeef"
_TOKEN = _POLICY + _NAME
_BATCHER = Assets(lovelace=1_500_000)
_DEPOSIT = Assets(lovelace=2_000_000)

_SUNDAE_STABLE_POLICY = "4de79a0c17180030bff4c36825cb6e99caa007bc632f789561a26d56"
_SUNDAE_CPP_POLICY = "e0302560ced2fdcbfcb2602697df970cd0d6a38f94b32703f51c312b"
_SUNDAE_BATCHER = Assets(lovelace=1_000_000)

# Default (fixed-amount) datums, pinned so swap-all support leaves them
# byte-identical.
_MINSWAP_V2_TOKEN_IN_CBOR = (
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799fd8799f581c22222222222222222222222222222222222222222222222222222222"
    "ffffffffd87980d8799fd8799f581c111111111111111111111111111111111111111111111111"
    "11111111ffd8799fd8799fd8799f581c222222222222222222222222222222222222222222222222"
    "22222222ffffffffd87980d8799f581cf5808c2c990d86da54bfc97d89cee6efa20cd846161635"
    "9478d96b4c5820d6d855d98b81c80b714fb810799fa673d5a4d8e5aafbe08d188559dfd928c32a"
    "ffd8799fd87980d8799f1a004c4b40ff1a002dc6c0d87980ff1a0016e360d87a80ff"
)
_MINSWAP_V2_ADA_IN_CBOR = (
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799fd8799f581c22222222222222222222222222222222222222222222222222222222"
    "ffffffffd87980d8799fd8799f581c111111111111111111111111111111111111111111111111"
    "11111111ffd8799fd8799fd8799f581c222222222222222222222222222222222222222222222222"
    "22222222ffffffffd87980d8799f581cf5808c2c990d86da54bfc97d89cee6efa20cd846161635"
    "9478d96b4c5820d6d855d98b81c80b714fb810799fa673d5a4d8e5aafbe08d188559dfd928c32a"
    "ffd8799fd87a80d8799f1a00989680ff1a003d0900d87980ff1a0016e360d87a80ff"
)
_MINSWAP_V2_POOL_CBOR = (
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799fd8799fd8799f581c22222222222222222222222222222222222222222222222222222222"
    "ffffffffd87980d8799fd8799f581c111111111111111111111111111111111111111111111111"
    "11111111ffd8799fd8799fd8799f581c222222222222222222222222222222222222222222222222"
    "22222222ffffffffd87980d8799f581cf5808c2c990d86da54bfc97d89cee6efa20cd846161635"
    "9478d96b4c5820d6d855d98b81c80b714fb810799fa673d5a4d8e5aafbe08d188559dfd928c32a"
    "ffd8799fd87980d8799f1a004c4b40ff1a002dc6c0d87980ff1a001e8480d87a80ff"
)
_SUNDAE_V3_CBOR = (
    "d8799fd8799f581c11111111111111111111111111111111111111111111111111111111ff"
    "d8799f581c22222222222222222222222222222222222222222222222222222222ff1a000f4240"
    "d8799fd8799fd8799f581c11111111111111111111111111111111111111111111111111111111"
    "ffd8799fd8799fd8799f581c2222222222222222222222222222222222222222222222222222"
    "2222ffffffffd87980ffd87a9f83581caaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "aaaaaaaaaa44deadbeef1a004c4b408340401a002dc6c0ff49d866821ad543084a80ff"
)


def _token(quantity: int) -> Assets:
    return Assets(root={_TOKEN: quantity})


def _ada(quantity: int) -> Assets:
    return Assets(lovelace=quantity)


def _minswap_v2_datum(
    in_assets: Assets,
    out_assets: Assets,
    **kwargs: bool,
) -> MinswapV2OrderDatum:
    return MinswapV2OrderDatum.create_datum(
        address_source=_SRC,
        in_assets=in_assets,
        out_assets=out_assets,
        batcher_fee=_BATCHER,
        deposit=_DEPOSIT,
        **kwargs,
    )


def _minswap_v2_pool() -> MinswapV2CPPState:
    return MinswapV2CPPState.model_construct(plutus_v2=True)


@pytest.fixture()
def _sundae_v3_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the SundaeSwap V3 batcher fee normally read from the settings datum."""
    for cls in (SundaeSwapV3CPPState, SundaeSwapV3StableSwap):
        monkeypatch.setattr(cls, "_batcher_fee", _SUNDAE_BATCHER, raising=False)


def _sundae_v3_pool(
    cls: type[SundaeSwapV3CPPState | SundaeSwapV3StableSwap],
    policy: str,
) -> SundaeSwapV3CPPState | SundaeSwapV3StableSwap:
    return cls.model_construct(
        pool_nft=Assets(root={policy + "000de140" + "11" * 28: 1}),
        plutus_v2=True,
    )


# ── Minswap V2: SwapExactInV2 ──
def test_swap_exact_in_v2_without_deducted_amount_is_a_specific_amount() -> None:
    step = SwapExactInV2.from_assets(in_asset=_token(5), out_asset=_ada(3))
    assert step.swap_amount_option == SAOSpecificAmount(swap_amount=5)


def test_swap_exact_in_v2_deducted_amount_swaps_all() -> None:
    step = SwapExactInV2.from_assets(
        in_asset=_token(5),
        out_asset=_ada(3),
        deducted_amount=7,
    )
    assert step.swap_amount_option == SAOAll(deducted_amount=7)


# ── Minswap V2: order datum ──
def test_minswap_v2_swap_all_token_input_deducts_nothing() -> None:
    datum = _minswap_v2_datum(_token(5_000_000), _ada(3_000_000), swap_all=True)
    assert datum.step.swap_amount_option == SAOAll(deducted_amount=0)


def test_minswap_v2_swap_all_ada_input_deducts_batcher_fee_and_deposit() -> None:
    datum = _minswap_v2_datum(_ada(10_000_000), _token(4_000_000), swap_all=True)
    assert datum.step.swap_amount_option == SAOAll(
        deducted_amount=_BATCHER.quantity() + _DEPOSIT.quantity(),
    )


@pytest.mark.parametrize(
    ("in_assets", "out_assets"),
    [
        pytest.param(_token(5_000_000), _ada(3_000_000), id="token_input"),
        pytest.param(_ada(10_000_000), _token(4_000_000), id="ada_input"),
    ],
)
def test_minswap_v2_swap_all_changes_only_the_swap_amount_option(
    in_assets: Assets,
    out_assets: Assets,
) -> None:
    fixed = _minswap_v2_datum(in_assets, out_assets)
    swap_all = _minswap_v2_datum(in_assets, out_assets, swap_all=True)
    swap_all.step.swap_amount_option = fixed.step.swap_amount_option
    assert swap_all.to_cbor_hex() == fixed.to_cbor_hex()


@pytest.mark.parametrize(
    ("in_assets", "out_assets", "cbor"),
    [
        pytest.param(
            _token(5_000_000),
            _ada(3_000_000),
            _MINSWAP_V2_TOKEN_IN_CBOR,
            id="token_input",
        ),
        pytest.param(
            _ada(10_000_000),
            _token(4_000_000),
            _MINSWAP_V2_ADA_IN_CBOR,
            id="ada_input",
        ),
    ],
)
def test_minswap_v2_default_datum_is_unchanged(
    in_assets: Assets,
    out_assets: Assets,
    cbor: str,
) -> None:
    default = _minswap_v2_datum(in_assets, out_assets)
    explicit = _minswap_v2_datum(in_assets, out_assets, swap_all=False)
    assert isinstance(default.step.swap_amount_option, SAOSpecificAmount)
    assert default.to_cbor_hex() == cbor
    assert explicit.to_cbor_hex() == cbor


# ── Minswap V2: pool threading ──
def test_minswap_v2_pool_default_datum_is_unchanged() -> None:
    _, default = _minswap_v2_pool().swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
    )
    _, explicit = _minswap_v2_pool().swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=False,
    )
    assert default.to_cbor_hex() == _MINSWAP_V2_POOL_CBOR
    assert explicit.to_cbor_hex() == _MINSWAP_V2_POOL_CBOR


def test_minswap_v2_swap_utxo_threads_swap_all_for_token_input() -> None:
    pool = _minswap_v2_pool()
    fixed_output, _ = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
    )
    output, datum = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=True,
    )
    assert datum.step.swap_amount_option == SAOAll(deducted_amount=0)
    assert output.datum == datum
    # The order value still carries the expected input plus fee and deposit.
    assert output.amount == fixed_output.amount


def test_minswap_v2_swap_utxo_threads_swap_all_for_ada_input() -> None:
    pool = _minswap_v2_pool()
    fixed_output, _ = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_ada(10_000_000),
        out_assets=_token(4_000_000),
    )
    output, datum = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_ada(10_000_000),
        out_assets=_token(4_000_000),
        swap_all=True,
    )
    fee_and_deposit = pool.batcher_fee().quantity() + pool.deposit().quantity()
    assert datum.step.swap_amount_option == SAOAll(deducted_amount=fee_and_deposit)
    assert output.amount == fixed_output.amount


def test_swap_all_inner_datum_forwards_through_minswap_v2() -> None:
    """A swap-all next hop is hashed into a Minswap V2 forward's receiver datum."""
    pool = _minswap_v2_pool()
    inner = pool.swap_datum(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=True,
    )
    _, outer = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_ada(10_000_000),
        out_assets=_token(5_000_000),
        address_target=pool.stake_address,
        datum_target=inner,
    )
    assert outer.receiver_datum_hash == SundaeV3ReceiverInlineDatumHash(
        datum_hash=inner.hash().payload,
    )
    # The inline datum attached to the forwarded output decodes back to the
    # same swap-all order and hashes to the forwarded hash.
    reparsed = MinswapV2OrderDatum.from_cbor(inner.to_cbor_hex())
    assert reparsed.step.swap_amount_option == SAOAll(deducted_amount=0)
    assert reparsed.hash() == inner.hash()


@pytest.mark.usefixtures("_sundae_v3_fee")
def test_swap_all_inner_datum_forwards_through_sundae_v3() -> None:
    """A swap-all next hop is embedded inline in a SundaeSwap V3 forward."""
    minswap = _minswap_v2_pool()
    inner = minswap.swap_datum(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=True,
    )
    sundae = _sundae_v3_pool(SundaeSwapV3StableSwap, _SUNDAE_STABLE_POLICY)
    _, outer = sundae.swap_utxo(
        address_source=_SRC,
        in_assets=_ada(10_000_000),
        out_assets=_token(5_000_000),
        address_target=minswap.stake_address,
        datum_target=inner,
    )
    assert outer.destination.datum == SundaeV3ReceiverInlineDatum(datum=inner)
    reparsed = SundaeV3OrderDatum.from_cbor(outer.to_cbor_hex())
    assert reparsed.hash() == outer.hash()


# ── SundaeSwap V3 ──
@pytest.mark.usefixtures("_sundae_v3_fee")
def test_sundae_v3_stable_swap_all_rejects_ada_input() -> None:
    """An ADA offer shares its value with the fee and deposit, so swapping all
    of it would leave the output without ADA."""
    pool = _sundae_v3_pool(SundaeSwapV3StableSwap, _SUNDAE_STABLE_POLICY)
    with pytest.raises(ValueError, match="SundaeSwapV3StableSwap.*ADA input"):
        pool.swap_datum(
            address_source=_SRC,
            in_assets=_ada(10_000_000),
            out_assets=_token(4_000_000),
            swap_all=True,
        )
    with pytest.raises(ValueError, match="SundaeSwapV3StableSwap.*ADA input"):
        pool.swap_utxo(
            address_source=_SRC,
            in_assets=_ada(10_000_000),
            out_assets=_token(4_000_000),
            swap_all=True,
        )


@pytest.mark.usefixtures("_sundae_v3_fee")
def test_sundae_v3_stable_swap_all_offers_twice_the_input() -> None:
    pool = _sundae_v3_pool(SundaeSwapV3StableSwap, _SUNDAE_STABLE_POLICY)
    fixed_output, fixed = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
    )
    output, datum = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=True,
    )
    assert datum.swap.in_value == [
        bytes.fromhex(_POLICY),
        bytes.fromhex(_NAME),
        2 * 5_000_000,
    ]
    # Only the offer changes; the order value is built from the real input.
    datum.swap.in_value = fixed.swap.in_value
    assert datum.to_cbor_hex() == fixed.to_cbor_hex()
    assert output.amount == fixed_output.amount


@pytest.mark.usefixtures("_sundae_v3_fee")
@pytest.mark.parametrize(
    ("cls", "policy"),
    [
        pytest.param(SundaeSwapV3StableSwap, _SUNDAE_STABLE_POLICY, id="stable"),
        pytest.param(SundaeSwapV3CPPState, _SUNDAE_CPP_POLICY, id="cpp"),
    ],
)
def test_sundae_v3_default_datum_is_unchanged(
    cls: type[SundaeSwapV3CPPState | SundaeSwapV3StableSwap],
    policy: str,
) -> None:
    pool = _sundae_v3_pool(cls, policy)
    _, default = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
    )
    _, explicit = pool.swap_utxo(
        address_source=_SRC,
        in_assets=_token(5_000_000),
        out_assets=_ada(3_000_000),
        swap_all=False,
    )
    assert default.to_cbor_hex() == _SUNDAE_V3_CBOR
    assert explicit.to_cbor_hex() == _SUNDAE_V3_CBOR


@pytest.mark.usefixtures("_sundae_v3_fee")
def test_sundae_v3_cpp_rejects_swap_all() -> None:
    pool = _sundae_v3_pool(SundaeSwapV3CPPState, _SUNDAE_CPP_POLICY)
    with pytest.raises(ValueError, match="SundaeSwapV3CPPState"):
        pool.swap_datum(
            address_source=_SRC,
            in_assets=_token(5_000_000),
            out_assets=_ada(3_000_000),
            swap_all=True,
        )
    with pytest.raises(ValueError, match="SundaeSwapV3CPPState"):
        pool.swap_utxo(
            address_source=_SRC,
            in_assets=_token(5_000_000),
            out_assets=_ada(3_000_000),
            swap_all=True,
        )


# ── Every other AMM pool ──
_SWAP_ALL_POOLS = {MinswapV2CPPState, SundaeSwapV3StableSwap}


def _concrete_pool_classes() -> list[type[AbstractPoolState]]:
    found: set[type[AbstractPoolState]] = set()
    walk: list[type[AbstractPoolState]] = [AbstractPoolState]
    while walk:
        for sub in walk.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                walk.append(sub)
    return sorted(
        (
            cls
            for cls in found
            if not inspect.isabstract(cls)
            and cls.__module__.startswith("charli3_dendrite.")
            and cls not in _SWAP_ALL_POOLS
        ),
        key=lambda cls: cls.__name__,
    )


_REJECTING_POOLS = _concrete_pool_classes()


@pytest.mark.parametrize("cls", _REJECTING_POOLS, ids=lambda cls: cls.__name__)
def test_pool_without_swap_all_support_rejects_swap_datum(
    cls: type[AbstractPoolState],
) -> None:
    pool = cls.model_construct(plutus_v2=True)
    with pytest.raises(ValueError, match=cls.__name__):
        pool.swap_datum(
            address_source=_SRC,
            in_assets=_token(5_000_000),
            out_assets=_ada(3_000_000),
            swap_all=True,
        )


@pytest.mark.parametrize(
    "cls",
    [cls for cls in _REJECTING_POOLS if cls.swap_utxo is AbstractPoolState.swap_utxo],
    ids=lambda cls: cls.__name__,
)
def test_pool_without_swap_all_support_rejects_swap_utxo(
    cls: type[AbstractPoolState],
) -> None:
    pool = cls.model_construct(plutus_v2=True)
    with pytest.raises(ValueError, match=cls.__name__):
        pool.swap_utxo(
            address_source=_SRC,
            in_assets=_token(5_000_000),
            out_assets=_ada(3_000_000),
            swap_all=True,
        )
