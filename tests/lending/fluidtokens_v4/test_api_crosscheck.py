"""Gated cross-check of decoded V4 pools and loans against FluidTokens' Provider API.

Skipped unless ``FLUID_E2E=1`` (live dbsync) and ``FLUIDTOKENS_API_KEY`` are set. Read
only: nothing is built, signed or submitted. The API returns an empty ``utxoAddress``
for V4 UTxOs, so records are matched on ``txHash`` / ``txIndex``. ``isPermissioned`` is
not compared: the API derives it from the hex of the condition hash, which is never the
literal ``NONE``.
"""

import os
from datetime import datetime

import pytest
from dotenv import load_dotenv
from pycardano import Address
from pycardano import Network
from pycardano import VerificationKeyHash

from charli3_dendrite.lending.units import constr

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1" or not os.environ.get("FLUIDTOKENS_API_KEY"),
    reason="requires live dbsync (FLUID_E2E=1) and FLUIDTOKENS_API_KEY",
)

_LIQUIDATION = 2
_PERPETUAL = 2
_TRUE = 1
_SOME = 0


@pytest.fixture(scope="module")
def snap():
    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend
    from charli3_dendrite.lending.fluidtokens_v4.loader import snapshot

    return snapshot(DbsyncBackend())


@pytest.fixture(scope="module")
def api():
    from charli3_dendrite.lending.fluidtokens.oracles.fluid_api import (
        FluidTokensProviderClient,
    )

    return FluidTokensProviderClient()


def _wallet(key_hash_hex, staking_part):
    return Address(
        payment_part=VerificationKeyHash(bytes.fromhex(key_hash_hex)),
        staking_part=staking_part,
        network=Network.MAINNET,
    ).encode()


def _by_out_ref(items):
    return {f"{item['txHash']}#{item['txIndex']}": item for item in items}


def _asset(unit):
    return ("", "") if unit == "lovelace" else (unit[:56], unit[56:])


def test_pools_match_the_api(snap, api):
    managers = {m.pool_id: m for m in snap.pool_managers}
    lenders = {}
    for pool in snap.pools:
        owner = managers[pool.pool_id].owner_auth
        if owner.kind != "signature":
            continue
        staking = Address.decode(pool.address).staking_part
        lenders.setdefault(_wallet(owner.hash_hex, staking), []).append(pool)
    compared = 0
    for lender, pools in lenders.items():
        api_pools = _by_out_ref(
            api._get(f"/providers/liquidity/pools/lender/{lender}")["pools"],
        )
        for pool in pools:
            item = api_pools.get(pool.out_ref)
            if item is None:
                continue
            datum = pool.pool_datum
            terms = datum.common_data
            policy, name = _asset(terms.principal_asset.unit())
            assert item["principalAsset"]["policyId"] == policy
            assert item["principalAsset"]["assetName"] == name
            assert item["permissionedConditionScriptHash"] == (
                datum.permissioned_condition_script_hash.hex()
            )
            assert item["interestRate"] == terms.interest_rate
            assert item["installmentPeriodHours"] == terms.installment_period
            assert item["totalInstallments"] == terms.total_installments
            assert item["initialGracePeriodHours"] == terms.initial_grace_period
            assert item["repaymentTimeWindowHours"] == terms.repayment_time_window
            assert item["penaltyFeeForLateRepayment"] == (
                terms.penalty_fee_for_late_repayment
            )
            assert item["repaymentReceipts"] == (
                constr(terms.repayment_receipts)[0] == _TRUE
            )
            assert item["dynamicCollateralPrice"] == (
                constr(datum.dynamic_collateral_price)[0] == _TRUE
            )
            _check_modes(item, terms.liquidation_mode, terms.repayment_mode)
            options = list(
                zip(
                    datum.collateral_options,
                    datum.min_collateral,
                    datum.min_collateral_divider,
                ),
            )
            assert len(item["collateralOptions"]) == len(options)
            for api_option, (option, minimum, divider) in zip(
                item["collateralOptions"],
                options,
            ):
                name_alt, name_fields = constr(option.maybe_asset_name)
                assert api_option["policyId"] == option.policy_id.hex()
                assert api_option["assetName"] == (
                    name_fields[0].hex() if name_alt == _SOME else ""
                )
                assert api_option["oraclePolicyId"] == (
                    option.oracle_token_asset.policy_id.hex()
                )
                assert api_option["oracleAssetName"] == (
                    option.oracle_token_asset.asset_name.hex()
                )
                assert api_option["minCollateralAmount"] == minimum
                assert api_option["minCollateralDivider"] == divider
            compared += 1
    assert compared > 0


def test_loans_match_the_api(snap, api):
    loans = {loan.out_ref: loan for loan in snap.loans}
    lenders = set()
    for manager in snap.lender_managers:
        auth = manager.lender_auth
        stake_alt, stake_fields = constr(manager.datum.lender_stake_credential)
        if auth.kind != "signature" or stake_alt != _SOME:
            continue
        inline_alt, inline_fields = constr(stake_fields[0])
        credential_alt, credential_fields = constr(inline_fields[0])
        if inline_alt != 0 or credential_alt != 0:
            continue
        staking = VerificationKeyHash(credential_fields[0])
        lenders.add(_wallet(auth.hash_hex, staking))
    compared = 0
    for lender in sorted(lenders):
        response = api._get(f"/providers/liquidity/loans/lender/{lender}")
        for item in response["loans"]:
            loan = loans.get(f"{item['txHash']}#{item['txIndex']}")
            if loan is None:
                continue
            datum = loan.loan_datum
            assert item["originId"] == datum.origin_id.hex()
            assert item["loanNftAssetName"] == loan.loan_id
            assert item["principalAmount"] == datum.principal_amount
            assert item["lendDateTimestamp"] == datum.lend_date
            assert item["baseInterestRate"] == datum.interest_rate
            assert item["totalInstallments"] == datum.total_installments
            assert item["repaidInstallments"] == datum.repaid_installments
            assert item["doneRecasts"] == datum.done_recasts
            collateral = loan.collateral_unit
            policy, name = _asset(collateral)
            assert item["collateral"]["policyId"] == policy
            assert item["collateral"]["assetName"] == name
            assert item["collateralAmount"] == loan.assets[collateral]
            _check_modes(item, datum.liquidation_mode, datum.repayment_mode)
            updated = datetime.fromisoformat(
                item["lastUpdatedAt"].replace("Z", "+00:00"),
            )
            loan.set_time(int(updated.timestamp() * 1000))
            assert item["remainingDebtRaw"] == loan.current_debt()
            compared += 1
    assert compared > 0


def _check_modes(item, liquidation_mode, repayment_mode):
    liquidation_alt, liquidation_fields = constr(liquidation_mode)
    if liquidation_alt == _LIQUIDATION:
        l_tv, divider, penalty = liquidation_fields[:3]
        assert item["liquidationMode"]["type"] == "PartialClaim"
        assert item["liquidationMode"]["liquidationPenalty"] == penalty
        if "liquidationLtvPercent" in item:
            assert item["liquidationLtvPercent"] == pytest.approx(100 * l_tv / divider)
    if constr(repayment_mode)[0] == _PERPETUAL:
        assert item["repaymentMode"]["type"] == "Perpetual"
