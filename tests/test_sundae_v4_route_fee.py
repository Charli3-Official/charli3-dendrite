"""SundaeSwap V4 multi-vault orders: the route fee budget and an explicit order budget.

A V4 basic swap names only what it offers and the least it must receive; the
scooper routes it across vaults when its fee budget pays for the route. The
fixture is a recorded mainnet order the scooper filled across two vaults.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pycardano import Address

from charli3_dendrite.backend import set_backend
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import ScriptReference
from charli3_dendrite.dexs.amm.sundae_v4 import ROUTE_EXTRA_HOP_FEE
from charli3_dendrite.dexs.amm.sundae_v4 import ROUTE_EXTRA_POOL_FEE
from charli3_dendrite.dexs.amm.sundae_v4 import FeeSettings
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4ConstantSumPool
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Deployment
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4OrderDatum
from charli3_dendrite.dexs.amm.sundae_v4 import SundaeV4Vault
from tests.sundae_v4_vault_factory import build_vault_utxo
from tests.test_sundae_v4_backend_redeemers import _Minimal

ORDER = json.loads(
    (Path(__file__).parent / "sundae_v4_route_order_fixture.json").read_text(),
)

_BASE_FEE = 1_280_000
_USDCX = "1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378"
_SUSDR = "7d9e4a0ee1a3f5d5ff8159ea91a83310cf2795ee7a87170c7aea05ae7355534472"


class _FeeBackend(_Minimal):
    """Serves a fee-settings datum fixed at ``_BASE_FEE``."""

    def get_datum_from_address(
        self,
        address,  # noqa: ANN001
        asset=None,  # noqa: ANN001
    ) -> ScriptReference:
        """Serve ``FeeSettings(base_fee=_BASE_FEE)`` unconditionally."""
        return ScriptReference(
            tx_hash=None,
            tx_index=None,
            address=None,
            assets=None,
            datum_hash=None,
            datum_cbor=FeeSettings(base_fee=_BASE_FEE).to_cbor_hex(),
            script=None,
        )


@pytest.fixture(autouse=True)
def _mainnet_fee() -> Iterator[None]:
    """Mainnet deployment with the live base fee pinned at ``_BASE_FEE``."""
    SundaeV4Vault.select_network("mainnet")
    SundaeV4Vault.clear_base_fee_cache()
    set_backend(_FeeBackend())
    try:
        yield
    finally:
        SundaeV4Vault.select_network("mainnet")
        SundaeV4Vault.clear_base_fee_cache()


def test_route_fee_budget_charges_each_extra_vault_and_hop() -> None:
    assert ROUTE_EXTRA_POOL_FEE == 1_000_000
    assert ROUTE_EXTRA_HOP_FEE == 500_000
    assert SundaeV4Vault.route_fee_budget(1, 1) == _BASE_FEE
    assert SundaeV4Vault.route_fee_budget(2, 2) == 2_780_000
    assert SundaeV4Vault.route_fee_budget(3, 3) == 4_280_000
    # Two vaults on one hop (a split) pay the extra pool but no extra hop.
    assert SundaeV4Vault.route_fee_budget(2, 1) == _BASE_FEE + 1_000_000


def test_route_fee_budget_takes_an_explicit_base_fee() -> None:
    assert SundaeV4Vault.route_fee_budget(2, 2, base_fee=1_000_000) == 2_500_000


@pytest.mark.parametrize(("pools", "hops"), [(0, 0), (1, 0), (0, 1), (1, 2)])
def test_route_fee_budget_rejects_an_impossible_route(pools: int, hops: int) -> None:
    with pytest.raises(ValueError, match="route"):
        SundaeV4Vault.route_fee_budget(pools, hops)


def test_the_recorded_two_vault_order_carries_the_route_fee() -> None:
    recorded = SundaeV4OrderDatum.from_cbor(ORDER["datum"])
    budget = SundaeV4Vault.route_fee_budget(2, 2)
    assert recorded.service_budget == recorded.max_per_execution == budget
    assert ORDER["value"]["lovelace"] == budget + 2_000_000


def _pool() -> SundaeV4ConstantSumPool:
    """A mainnet-bound constant-sum pool; the order builders need only the deployment."""
    values, config = build_vault_utxo(
        [("aa" * 28 + "01", 1_000), ("bb" * 28 + "02", 1_000)],
        prices=[1, 1],
        total_lp=2_000,
        network="mainnet",
    )
    vault = SundaeV4Vault.model_validate(values)
    cs = SundaeV4Deployment.for_network("mainnet").validator("constant_sum.withdraw")
    vault.supply_module_config(cs, config)
    return vault.pools()[0]


def test_a_two_vault_order_is_byte_exact_with_the_recorded_one() -> None:
    recorded = SundaeV4OrderDatum.from_cbor(ORDER["datum"])
    output, datum = _pool().swap_utxo(
        address_source=recorded.address_source(),
        in_assets=Assets(**{_USDCX: 5_000_000}),
        out_assets=Assets(**{_SUSDR: 4_838_802}),
        fee_budget=SundaeV4Vault.route_fee_budget(2, 2),
    )
    assert datum.to_cbor_hex() == ORDER["datum"]
    assert output.datum == datum
    assert output.amount.coin == ORDER["value"]["lovelace"]
    policy, name = _USDCX[:56], _USDCX[56:]
    held = {
        (p.payload.hex(), n.payload.hex()): q
        for p, names in output.amount.multi_asset.items()
        for n, q in names.items()
    }
    assert held == {(policy, name): 5_000_000}
    # The recorded order also carries its owner's stake credential; the order
    # validator is the payment part.
    assert output.address.payment_part == Address.decode(ORDER["address"]).payment_part


def test_an_unset_budget_builds_the_base_fee_order() -> None:
    pool = _pool()
    recorded = SundaeV4OrderDatum.from_cbor(ORDER["datum"])
    output, datum = pool.swap_utxo(
        address_source=recorded.address_source(),
        in_assets=Assets(**{_USDCX: 5_000_000}),
        out_assets=Assets(**{_SUSDR: 4_838_802}),
    )
    assert datum.service_budget == datum.max_per_execution == _BASE_FEE
    assert output.amount.coin == _BASE_FEE + 2_000_000
    assert output.address == pool.stake_address


def test_a_budget_below_the_base_fee_is_refused() -> None:
    with pytest.raises(ValueError, match="base fee"):
        _pool().swap_utxo(
            address_source=SundaeV4OrderDatum.from_cbor(
                ORDER["datum"]
            ).address_source(),
            in_assets=Assets(**{_USDCX: 5_000_000}),
            out_assets=Assets(**{_SUSDR: 4_838_802}),
            fee_budget=_BASE_FEE - 1,
        )


def test_a_budgeted_order_still_offers_and_asks_one_asset() -> None:
    with pytest.raises(ValueError, match="one asset"):
        _pool().swap_utxo(
            address_source=SundaeV4OrderDatum.from_cbor(
                ORDER["datum"]
            ).address_source(),
            in_assets=Assets(**{_USDCX: 5_000_000, "lovelace": 1}),
            out_assets=Assets(**{_SUSDR: 4_838_802}),
            fee_budget=2_780_000,
        )
