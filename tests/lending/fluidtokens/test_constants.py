"""FluidTokens deployment constants + config-datum resolution.

Offline tests cover the static constants, the config-datum parser, and
``resolve_addresses`` (both the live-config path via a stubbed resolver and the
constants fallback). A gated live test (``FLUID_E2E=1``) doubles as a redeploy-watch:
it asserts the on-chain config datum still matches the checked-in constants.
"""

import os

import cbor2
import pytest
from pycardano import Address

from charli3_dendrite.lending.fluidtokens import constants as C
from charli3_dendrite.lending.fluidtokens.constants import FluidConfig
from charli3_dendrite.lending.fluidtokens.constants import LOAN_ADDRESS
from charli3_dendrite.lending.fluidtokens.constants import POOL_ADDRESS
from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_NAME
from charli3_dendrite.lending.fluidtokens.constants import PROTOCOL_CONFIG_NFT_POLICY
from charli3_dendrite.lending.fluidtokens.constants import REQUEST_ADDRESS
from charli3_dendrite.lending.fluidtokens.constants import resolve_addresses


def test_config_nft_constants():
    assert PROTOCOL_CONFIG_NFT_POLICY == (
        "219832152b2c489358f4c02a1818d312a851b1f55774ae881e33a907"
    )
    assert PROTOCOL_CONFIG_NFT_NAME == "706172616d6574657273"  # "parameters"


def test_entity_addresses_are_mainnet_script_addresses():
    for addr in (POOL_ADDRESS, LOAN_ADDRESS, REQUEST_ADDRESS):
        assert addr.startswith("addr1")


def _synth_config_cbor(cfg: FluidConfig, *, reserved: int = 6) -> str:
    """Build a config-datum cbor (Constr(0, [...])) that ``FluidConfig.parse`` reads.

    Fields 0-1 are arbitrary governance credentials; 2-15 are ``cfg``'s hashes in
    on-chain order; a reserved tail (empty bytes) is appended to mirror the live datum.
    """
    fields = [
        bytes.fromhex("ab" * 28),  # [0] governance key hash
        cbor2.CBORTag(122, [bytes.fromhex("cd" * 28)]),  # [1] Constr(1, [hash])
        bytes.fromhex(cfg.pool_policy),
        bytes.fromhex(cfg.request_policy),
        bytes.fromhex(cfg.borrower_bond_policy),
        bytes.fromhex(cfg.lender_bond_policy),
        bytes.fromhex(cfg.loan_policy),
        bytes.fromhex(cfg.repayment_policy),
        bytes.fromhex(cfg.pool_spend_skh),
        bytes.fromhex(cfg.request_spend_skh),
        bytes.fromhex(cfg.loan_spend_skh),
        bytes.fromhex(cfg.loan_claim_action_skh),
        bytes.fromhex(cfg.loan_repay_action_skh),
        bytes.fromhex(cfg.loan_change_collateral_action_skh),
        bytes.fromhex(cfg.loan_recast_action_skh),
        bytes.fromhex(cfg.asset_manager_spend_skh),
    ]
    fields.extend(b"" for _ in range(reserved))
    return cbor2.dumps(cbor2.CBORTag(121, fields)).hex()


def test_parse_round_trips_defaults():
    # The synthetic datum built from the checked-in constants must parse back to them,
    # which locks the field indices against the real on-chain layout.
    cfg = FluidConfig.defaults()
    assert FluidConfig.parse(_synth_config_cbor(cfg)) == cfg


def test_parse_reads_redeployed_hashes():
    redeployed = FluidConfig(
        pool_policy="11" * 28,
        request_policy="22" * 28,
        borrower_bond_policy="33" * 28,
        lender_bond_policy="44" * 28,
        loan_policy="55" * 28,
        repayment_policy="66" * 28,
        pool_spend_skh="77" * 28,
        request_spend_skh="88" * 28,
        loan_spend_skh="99" * 28,
        loan_claim_action_skh="aa" * 28,
        loan_repay_action_skh="bb" * 28,
        loan_change_collateral_action_skh="cc" * 28,
        loan_recast_action_skh="dd" * 28,
        asset_manager_spend_skh="ee" * 28,
    )
    assert FluidConfig.parse(_synth_config_cbor(redeployed)) == redeployed


def test_parse_rejects_non_constr():
    with pytest.raises(ValueError, match="Constr"):
        FluidConfig.parse(cbor2.dumps(123).hex())


def test_parse_rejects_too_few_fields():
    short = cbor2.dumps(cbor2.CBORTag(121, [b"\x00"] * 4)).hex()
    with pytest.raises(ValueError, match="fields"):
        FluidConfig.parse(short)


class _NoDbBackend:
    """A backend without ``db_query`` -> config resolution must fall back."""


def test_resolve_addresses_falls_back_to_constants():
    addrs = resolve_addresses(_NoDbBackend())

    assert addrs["pool_policy"] == C.POOL_POLICY
    assert addrs["loan_policy"] == C.LOAN_POLICY
    assert addrs["request_policy"] == C.REQUEST_POLICY
    # Payment-credential identities derived from the spend script hashes.
    assert Address.decode(addrs["pool"]).payment_part.payload.hex() == C.POOL_SPEND_SKH
    assert Address.decode(addrs["loan"]).payment_part.payload.hex() == C.LOAN_SPEND_SKH
    assert (
        Address.decode(addrs["request"]).payment_part.payload.hex()
        == C.REQUEST_SPEND_SKH
    )


def test_resolve_addresses_prefers_live_config(monkeypatch):
    redeployed = FluidConfig.parse(
        _synth_config_cbor(
            FluidConfig(
                pool_policy="11" * 28,
                request_policy="22" * 28,
                borrower_bond_policy="33" * 28,
                lender_bond_policy="44" * 28,
                loan_policy="55" * 28,
                repayment_policy="66" * 28,
                pool_spend_skh="77" * 28,
                request_spend_skh="88" * 28,
                loan_spend_skh="99" * 28,
                loan_claim_action_skh="aa" * 28,
                loan_repay_action_skh="bb" * 28,
                loan_change_collateral_action_skh="cc" * 28,
                loan_recast_action_skh="dd" * 28,
                asset_manager_spend_skh="ee" * 28,
            ),
        ),
    )

    class _Utxo:
        datum = _synth_config_cbor(redeployed)

    import charli3_dendrite.lending.fluidtokens.transactions.resolve as resolve_mod

    monkeypatch.setattr(
        resolve_mod,
        "resolve_config_utxo",
        lambda _backend, **_kw: _Utxo(),
    )

    addrs = resolve_addresses(object())

    assert addrs["pool_policy"] == "11" * 28
    assert addrs["loan_policy"] == "55" * 28
    assert Address.decode(addrs["pool"]).payment_part.payload.hex() == "77" * 28


@pytest.mark.skipif(
    os.environ.get("FLUID_E2E") != "1",
    reason="requires live dbsync (FLUID_E2E=1)",
)
def test_live_config_matches_checked_in_constants():
    """Redeploy watch: the on-chain config datum must still match the constants.

    A failure here means FluidTokens redeployed (new script hashes / policies) and the
    static constants need refreshing; the resolver already self-heals for discovery.
    """
    from dotenv import load_dotenv

    load_dotenv()
    from charli3_dendrite.backend.dbsync import DbsyncBackend

    from charli3_dendrite.lending.fluidtokens.constants import resolve_config

    assert resolve_config(DbsyncBackend()) == FluidConfig.defaults()
