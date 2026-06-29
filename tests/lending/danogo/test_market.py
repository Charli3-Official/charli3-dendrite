from charli3_dendrite.lending.danogo.market import DanogoMarket


def _market_dict():
    return {
        "supply_token": "lovelace",
        "collaterals": {"aa" + "0" * 54 + "444a4544": 8000},
        "alt_supply_tokens": {"bb" + "0" * 54: True},
        "base_rate": 400,
        "power_base": 10_470,
        "util_cap": 8500,
        "loan_fee_rate": 2000,
        "loan_origination_fee_rate": 0,
        "min_tx_amount": 1_000_000,
    }


def test_market_exposes_supply_token_and_thresholds():
    m = DanogoMarket(**_market_dict())
    assert m.supply_token == "lovelace"
    assert m.collaterals["aa" + "0" * 54 + "444a4544"] == 8000
    assert m.util_cap == 8500


def test_threshold_for_unit_defaults_zero():
    m = DanogoMarket(**_market_dict())
    assert m.threshold_for("unknownunit") == 0
