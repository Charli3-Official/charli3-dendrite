"""Liqwid build errors and the exact loan arithmetic of ``terms.py``.

Integer tables come from settled mainnet transactions: the liquidation rows in
``fixtures/liquidation_rows.json.gz`` and worked loan modifications, each named by
its transaction hash.
"""

from __future__ import annotations

import ast
import gzip
import json
import pickle
import random
import subprocess
import sys
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from charli3_dendrite.lending.liqwid import math as lqm
from charli3_dendrite.lending.liqwid.datums import ActionDatum
from charli3_dendrite.lending.liqwid.datums import LoanDatum
from charli3_dendrite.lending.liqwid.datums import MarketParamsDatum
from charli3_dendrite.lending.liqwid.transactions import errors
from charli3_dendrite.lending.liqwid.transactions import terms
from charli3_dendrite.lending.liqwid.transactions.terms import ActionDeltas
from charli3_dendrite.lending.liqwid.transactions.terms import RepayResult
from charli3_dendrite.lending.liqwid.transactions.terms import Settlement
from charli3_dendrite.lending.transactions.errors import LendingBuildError

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "08abc365d15eb5a450ff8ed2a6207bfa3cb5a0df4ec1ddd0e6636026"


@cache
def _rows() -> list[dict[str, Any]]:
    raw = gzip.decompress((FIXTURES / "liquidation_rows.json.gz").read_bytes())
    return json.loads(raw)


def _row(tx_prefix: str) -> dict[str, Any]:
    (row,) = [row for row in _rows() if row["tx"].startswith(tx_prefix)]
    return row


def _params(min_interest_rate: tuple[int, int] = (1, 1250)) -> MarketParamsDatum:
    return MarketParamsDatum(
        length=36,
        initial_qtoken_rate=(1, 50),
        income_ratio=(10, 9, 0, 0),
        legacy_loan_validator=None,
        interest_period_ms=2_000_000,
        rate_model=(),
        max_ltv=(3, 4),
        max_collaterals=5,
        min_interest_rate=min_interest_rate,
        close_factor=(1, 2),
        liquidation_threshold=(1, 1),
        action_validator=None,
        batch_validator=None,
        borrow_cap=None,
        supply_cap=None,
        withdraw_zero=None,
        loan_validator=None,
        raw=(),
    )


def _loan(principal: int, interest: int, min_interest: int, index: int) -> LoanDatum:
    return LoanDatum(OWNER, principal, interest, min_interest, index)


# --- errors ----------------------------------------------------------------------

ERROR_CASES: list[tuple[type[errors.LiqwidBuildError], dict[str, Any]]] = [
    (errors.ActionUtxoSpentError, {"out_ref": "ab" * 32 + "#1"}),
    (
        errors.MarketAwaitingBatchError,
        {"market": "Ada", "deadline_ms": 1_792_611_257_000},
    ),
    (errors.ForeignReferenceInputError, {"out_ref": "cd" * 32 + "#0"}),
    (
        errors.UnhealthyLoanError,
        {"required": Fraction(681_740_000), "available": Fraction(681_739_999, 1)},
    ),
    (errors.MinValueError, {"amount": 80_999_999, "minimum": 81_000_000}),
    (errors.CapExceededError, {"cap": "borrow"}),
    (errors.CapExceededError, {"cap": "supply"}),
    (errors.CollateralCountError, {"count": 1, "limit": 0}),
    (errors.InsufficientLiquidityError, {"needed": 10, "available": 9}),
    (errors.UnsupportedCollateralError, {"unit": "ef" * 28}),
    (errors.UnsupportedMarketError, {"market": "POL", "reason": "MarketParams list34"}),
    (errors.UnsupportedLoanError, {"out_ref": "01" * 32 + "#5", "reason": "legacy"}),
    (errors.MissingFeedError, {"asset": "lovelace"}),
    (errors.RepayAmountError, {"repay": 11, "debt": 10, "payoff": 12}),
]


@pytest.mark.parametrize(("cls", "kwargs"), ERROR_CASES)
def test_each_error_keeps_its_arguments_and_a_one_line_message(cls, kwargs):
    error = cls(**kwargs)
    assert isinstance(error, errors.LiqwidBuildError)
    assert isinstance(error, LendingBuildError)
    assert isinstance(error, ValueError)
    for name, value in kwargs.items():
        assert getattr(error, name) == value
    message = str(error)
    assert message
    assert "\n" not in message
    # positional construction matches the documented argument order
    assert str(cls(*kwargs.values())) == message
    copied = pickle.loads(pickle.dumps(error))
    assert type(copied) is cls
    assert str(copied) == message
    for name, value in kwargs.items():
        assert getattr(copied, name) == value


def test_every_error_is_exported():
    names = {cls.__name__ for cls, _ in ERROR_CASES} | {"LiqwidBuildError"}
    assert names == set(errors.__all__)


# --- settlement ------------------------------------------------------------------

SETTLE_ROWS = [
    # tx, (p0, i0, m0, x0), s5, debt, interest, index
    (
        # Ada borrow-more 2a2930b0ef53cb7f4b5f9705cd28889e576559c0fc798224393666e9ecbc6063
        "2a2930b0",
        (578_250_654, 0, 489_346, 19_761_242_199_725_666),
        19_763_163_649_327_205,
        578_306_880,
        56_226,
        19_763_163_677_196_094,
    ),
    (
        # USDM borrow-more efac5202df8509aa07b7a91f9d709dc34295eeaaa561588f887dfbc396506cc1
        "efac5202",
        (867_698_782_924, 75_273_426_323, 482_960_000, 26_059_106_366_117_823),
        26_619_183_062_951_314,
        963_239_088_424,
        95_540_305_500,
        26_619_183_062_974_498,
    ),
    (
        # Ada loan 79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902#40
        "79e2bda3#40",
        (383_321_869, 0, 0, 19_359_639_391_292_492),
        19_764_188_185_007_556,
        391_331_957,
        8_010_088,
        19_764_188_225_349_542,
    ),
    (
        # STRIKE-ADA repay ae44af0bb2dcf08ac0b399abe55f26ba11d8ae35ef847d4d06db3d20e3b9454e
        "ae44af0b",
        (3_000_000_000, 0, 2_400_000, 16_838_849),
        16_927_635,
        3_015_818_065,
        15_818_065,
        16_927_635,
    ),
    (
        # STRIKE-USDCx repay e64a99c5d432eff960c829458edd86470f0c25236e10519aaf6ec574ce1738d9
        "e64a99c5",
        (232_022_777, 35_072, 185_619, 16_779_827),
        16_784_815,
        232_126_831,
        104_054,
        16_784_815,
    ),
    (
        # LP-S close 6e7a7e5b4f5a42a1b2ba4383ed0332cbdf9b99a2e4e0cfbd3e4865d1ca778832
        "6e7a7e5b",
        (9_102_322_242, 90_211_096, 0, 17_107_781),
        17_153_887,
        9_217_307_501,
        114_985_259,
        17_153_887,
    ),
]


@pytest.mark.parametrize(
    ("label", "loan", "s5", "debt", "interest", "index"),
    SETTLE_ROWS,
    ids=[row[0] for row in SETTLE_ROWS],
)
def test_settle_reproduces_the_worked_rows(label, loan, s5, debt, interest, index):
    settlement = terms.settle(_loan(*loan), s5)
    assert settlement == Settlement(debt, interest, index, clamped=False)


# Ada partial repay 1478680fb708a213a7c52cda8eefa81221a3e66c74a6f6b5abdac65403930e16:
# owed 5,106,691,516 (interest 611,618) indexes down to 5,106,691,513 on a loan
# rebased above the batched index. The loan and market indexes reproduce that
# rounding.
CLAMP_OWED = 5_106_691_516
CLAMP_INTEREST = 611_618
CLAMP_LOAN_INDEX = 19_763_163_700_000_000
CLAMP_MARKET_INDEX = 19_763_163_688_389_842


def test_settle_clamps_at_the_owed_amount_and_keeps_the_index():
    loan = _loan(CLAMP_OWED - CLAMP_INTEREST, CLAMP_INTEREST, 0, CLAMP_LOAN_INDEX)
    settlement = terms.settle(loan, CLAMP_MARKET_INDEX)
    assert settlement == Settlement(
        debt=CLAMP_OWED,
        interest=CLAMP_INTEREST,
        index=CLAMP_LOAN_INDEX,
        clamped=True,
    )
    # at the loan's own index nothing rounds, so nothing is clamped
    assert terms.settle(loan, CLAMP_LOAN_INDEX).clamped is False


def test_settle_a_zero_owed_loan_divides_by_nothing():
    index = 16_777_216
    assert terms.settle(_loan(0, 0, 0, index), 19_000_000) == Settlement(
        0,
        0,
        index,
        clamped=False,
    )
    assert terms.settle(_loan(0, 0, 82, index), 19_000_000) == Settlement(
        0,
        0,
        index,
        clamped=False,
    )


# --- action deltas ---------------------------------------------------------------


def test_action_deltas_apply_field_wise_and_keep_the_nonce():
    nonce = ["nonce", 7]
    action = ActionDatum(10, 20, 30, 40, 50, nonce)
    deltas = ActionDeltas(
        supply=1, qtoken=-2, principal=3, interest=-4, min_interest_paid=5
    )
    applied = deltas.apply_to(action)
    assert applied == ActionDatum(11, 18, 33, 36, 55, nonce)
    assert applied.nonce is nonce
    assert ActionDeltas().apply_to(action) == action


def test_action_deltas_between_two_records_undo_apply_to():
    before = ActionDatum(10, 20, 30, 40, 50, ["nonce", 7])
    after = ActionDatum(11, 18, 33, 36, 55, ["nonce", 8])
    deltas = ActionDeltas.between(before, after)
    assert deltas == ActionDeltas(
        supply=1, qtoken=-2, principal=3, interest=-4, min_interest_paid=5
    )
    assert deltas.apply_to(before) == ActionDatum(11, 18, 33, 36, 55, ["nonce", 7])


def test_held_collateral_drops_zero_quantities():
    assert terms.held_collateral({"a": 3, "b": 0}) == {"a": 3}


def test_held_collateral_refuses_a_negative_quantity():
    with pytest.raises(ValueError, match="negative collateral quantity -1 of c"):
        terms.held_collateral({"a": 3, "c": -1})


def test_action_deltas_underlying_delta_follows_the_value_rule():
    # supply - principal - interest + min_interest_paid
    deltas = ActionDeltas(
        supply=100, qtoken=7, principal=30, interest=-5, min_interest_paid=2
    )
    assert deltas.underlying_delta() == 100 - 30 + 5 + 2


def test_close_underlying_delta_of_the_ada_close():
    # Ada close 9362b410197dc6b00912733972342fbb5ee016df70949b768ed92d9e7c49df20 repays
    # the open 574f8f14505eb51cd34fc96f769d81da4c57d96a53fb789871b65e10c8b4297d before
    # the next batch, so the loan is still at the open's index.
    s5 = 19_763_675_910_528_091
    loan = terms.open_loan(OWNER, 50_811_360_333, s5, _params())
    assert loan == LoanDatum(OWNER, 50_811_360_333, 0, 40_649_089, s5)
    deltas = terms.close_deltas(loan, s5)
    assert deltas == ActionDeltas(
        principal=-50_811_360_333, min_interest_paid=40_649_089
    )
    assert deltas.underlying_delta() == 50_852_009_422
    assert terms.close_payment(loan, s5) == 50_852_009_422


# --- supply and redeem -----------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "s9", "minted"),
    [
        # the Liqwid API supply quote for 100 ADA
        (100_000_000, (9_233_708_323_970, 435_888_389_624_643), 4_720_621_166),
        # STRIKE-ADA supply 2bf96ad7da7270197799bf740e7f42960988c6d542962b8f62a44a64548be95a
        (500_000_000, (37_181_401_450, 1_848_386_065_803), 24_856_325_928),
        # STRIKE-USDCx supply 248faba9605a580a16f1f95f42f82fb377e75c1f38bdd2247b2c1328bd201d4b
        (30_000_000, (30_100_370, 501_339_797), 499_668_074),
    ],
)
def test_supply_mint(amount, s9, minted):
    assert terms.supply_mint(amount, s9) == minted


@pytest.mark.parametrize(
    ("qtokens", "s9", "payout"),
    [
        # STRIKE-ADA redeem 1c965d8ad4693fa2b196268e6f811f4f035df9e8fdb644cd6cca291a6a838cd1
        (24_856_325_878, (37_181_401_450, 1_848_386_065_803), 499_999_998),
        # STRIKE-USDCx redeem 9b740ed06f6af7e0261045009187c991080c8181aa48da6c0e681ae7b89461f4
        (999_336_148, (30_100_370, 501_339_797), 59_999_999),
    ],
)
def test_redeem_payout_is_the_floor(qtokens, s9, payout):
    assert terms.redeem_payout(qtokens, s9) == payout


def test_redeem_burn_for_is_the_smallest_burn_that_pays_the_amount():
    rng = random.Random(0)
    for _ in range(200):
        den = rng.randint(1, 10**16)
        num = rng.randint(1, 10**16)
        amount = rng.randint(0, 10**13)
        s9 = (num, den)
        burn = terms.redeem_burn_for(amount, s9)
        assert terms.redeem_payout(burn, s9) >= amount
        if burn > 0:
            assert terms.redeem_payout(burn - 1, s9) < amount


@pytest.mark.parametrize(
    "call",
    [
        lambda: terms.supply_mint(-1, (1, 50)),
        lambda: terms.supply_mint(1, (0, 50)),
        lambda: terms.redeem_payout(-1, (1, 50)),
        lambda: terms.redeem_payout(1, (0, 50)),
        lambda: terms.redeem_burn_for(-1, (1, 50)),
        lambda: terms.redeem_burn_for(1, (0, 50)),
    ],
)
def test_supply_and_redeem_reject_negative_amounts_and_non_positive_rates(call):
    with pytest.raises(ValueError):
        call()


# --- open and borrow more --------------------------------------------------------


def test_open_loan_records_the_minimum_interest_and_the_index():
    loan = terms.open_loan(OWNER, 30_199_061, 16_784_815, _params())
    # STRIKE-USDCx open 3fced096c5d395a00673b07da01c83c152a67da1d83ed35e53d222e485740f86
    assert loan == LoanDatum(OWNER, 30_199_061, 0, 24_160, 16_784_815)
    with pytest.raises(ValueError):
        terms.open_loan(OWNER, 0, 16_784_815, _params())


@pytest.mark.parametrize(
    ("loan", "s5", "amount", "out"),
    [
        (
            # 2a2930b0ef53cb7f4b5f9705cd28889e576559c0fc798224393666e9ecbc6063
            (578_250_654, 0, 489_346, 19_761_242_199_725_666),
            19_763_163_649_327_205,
            103_000_000,
            (681_250_654, 56_226, 571_746, 19_763_163_677_196_094),
        ),
        (
            # efac5202df8509aa07b7a91f9d709dc34295eeaaa561588f887dfbc396506cc1
            (867_698_782_924, 75_273_426_323, 482_960_000, 26_059_106_366_117_823),
            26_619_183_062_951_314,
            27_000_000_000,
            (894_698_782_924, 95_540_305_500, 504_560_000, 26_619_183_062_974_498),
        ),
        (
            # 81 ADA more on 79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902#40
            (383_321_869, 0, 0, 19_359_639_391_292_492),
            19_764_188_185_007_556,
            81_000_000,
            (464_321_869, 8_010_088, 64_800, 19_764_188_225_349_542),
        ),
    ],
)
def test_increase_settles_then_adds_principal(loan, s5, amount, out):
    new_loan, deltas = terms.increase(_loan(*loan), s5, amount, _params())
    assert new_loan == _loan(*out)
    assert deltas == ActionDeltas(principal=amount)
    assert deltas.underlying_delta() == -amount


def test_increase_rejects_a_non_positive_amount():
    with pytest.raises(ValueError):
        terms.increase(_loan(100, 0, 1, 7), 7, 0, _params())


# --- repay -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("loan", "s5", "repay", "out", "principal_delta", "interest_delta"),
    [
        (
            # STRIKE-ADA ae44af0bb2dcf08ac0b399abe55f26ba11d8ae35ef847d4d06db3d20e3b9454e
            (3_000_000_000, 0, 2_400_000, 16_838_849),
            16_927_635,
            1_507_909_032,
            (1_507_909_033, 0, 0, 16_927_635),
            -1_492_090_967,
            -15_818_065,
        ),
        (
            # STRIKE-USDCx e64a99c5d432eff960c829458edd86470f0c25236e10519aaf6ec574ce1738d9
            (232_022_777, 35_072, 185_619, 16_779_827),
            16_784_815,
            125_444_468,
            (106_682_363, 0, 81_565, 16_784_815),
            -125_340_414,
            -104_054,
        ),
        (
            # 100 ADA on 79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902#40
            (383_321_869, 0, 0, 19_359_639_391_292_492),
            19_764_188_185_007_556,
            100_000_000,
            (291_331_957, 0, 0, 19_764_188_225_349_542),
            -91_989_912,
            -8_010_088,
        ),
    ],
)
def test_apply_repay_pays_interest_first(
    loan,
    s5,
    repay,
    out,
    principal_delta,
    interest_delta,
):
    result = terms.apply_repay(_loan(*loan), s5, repay)
    assert result == RepayResult(
        loan=_loan(*out),
        deltas=ActionDeltas(principal=principal_delta, interest=interest_delta),
    )
    assert result.deltas.underlying_delta() == repay


# USDM partial repay 89f443ffb50e53eaa3cdd4b376f5e37fafd4211b3ae8e9ca402feaea700ded8d and
# its loan-input variants: R = 42,130,664. The loan index is not part of the example;
# the settlement carries one through unchanged.
INDEX = 26_000_000_000_000_000
REPAY_89F = 42_130_664


@pytest.mark.parametrize(
    ("loan", "interest", "out"),
    [
        # natural: iS 1,056,667
        ((125_000_000, 113_547, 100_000), 1_056_667, (83_926_003, 0, 0)),
        # variant B: m0 5,000,000
        ((125_000_000, 113_547, 5_000_000), 1_056_667, (83_926_003, 0, 3_943_333)),
        # variant D: R < iS < m0, so m1 = m0 - iS
        (
            (125_000_000, 60_000_000, 80_000_000),
            61_394_551,
            (125_000_000, 19_263_887, 18_605_449),
        ),
    ],
)
def test_repay_from_settlement_on_the_usdm_variants(loan, interest, out):
    p0, i0, m0 = loan
    settlement = Settlement(p0 + interest, interest, INDEX, clamped=False)
    result = terms.repay_from_settlement(
        _loan(p0, i0, m0, INDEX - 1), settlement, REPAY_89F
    )
    assert result.loan == _loan(*out, INDEX)
    assert result.deltas == ActionDeltas(
        principal=out[0] - p0,
        interest=out[1] - interest,
    )


def test_interest_only_repay_leaves_the_principal():
    # USDM 6d7eff5a541cc531f9ae79580bab9f49d36f67b69cc4d835d58c3600c353c9cd:
    # R 162,694,540 < iS 267,232,659. The example records no principal; any
    # principal splits the same way.
    principal = 10_000_000_000
    i_s = 267_232_659
    settlement = Settlement(principal + i_s, i_s, INDEX, clamped=False)
    result = terms.repay_from_settlement(
        _loan(principal, 200_000_000, 0, INDEX - 1),
        settlement,
        162_694_540,
    )
    assert result.loan == _loan(principal, 104_538_119, 0, INDEX)
    assert result.deltas == ActionDeltas(principal=0, interest=-162_694_540)


def test_repay_bounds():
    loan = _loan(1_000, 10, 50, INDEX)
    settlement = terms.settle(loan, INDEX)
    assert settlement.debt == 1_010
    with pytest.raises(ValueError):
        terms.repay_from_settlement(loan, settlement, -1)
    # a zero repay only rewrites the settled loan
    assert terms.repay_from_settlement(loan, settlement, 0).loan == _loan(
        1_000, 10, 40, INDEX
    )
    # at most the settled debt; the payoff (1,050) closes instead
    assert terms.repay_from_settlement(loan, settlement, 1_010).loan == _loan(
        0, 0, 40, INDEX
    )
    with pytest.raises(errors.RepayAmountError) as raised:
        terms.apply_repay(loan, INDEX, 1_011)
    assert (raised.value.repay, raised.value.debt, raised.value.payoff) == (
        1_011,
        1_010,
        1_050,
    )
    with pytest.raises(errors.RepayAmountError):
        terms.apply_repay(loan, INDEX, 1_050)


def test_liquidation_repay_of_the_settled_debt_leaves_the_minimum_interest():
    # NIGHT liquidation 3e4347ff93478d3420e951a037d92e5fb1bbd15795555303d96ff7341c00385e
    row = _row("3e4347ff")
    loan = LoanDatum(OWNER, row["p0"], row["i0"], row["m0"], row["s5"])
    settlement = Settlement(row["Ds"], row["Ds"] - row["p0"], row["s5"], clamped=False)
    assert row["m0"] > settlement.interest
    result = terms.repay_from_settlement(loan, settlement, row["Ds"])
    assert result.loan == LoanDatum(OWNER, 0, 0, 4_874_559, row["s5"])
    with pytest.raises(errors.RepayAmountError) as raised:
        terms.repay_from_settlement(loan, settlement, row["Ds"] + 1)
    assert raised.value.payoff == row["p0"] + row["m0"]


def test_liquidation_rows_follow_the_repay_split():
    rows = _rows()
    assert len(rows) == 458
    assert all(row["exact"] for row in rows)
    for row in rows:
        loan = LoanDatum(OWNER, row["p0"], row["i0"], row["m0"], row["s5"])
        settlement = Settlement(
            row["Ds"], row["Ds"] - row["p0"], row["s5"], clamped=False
        )
        result = terms.repay_from_settlement(loan, settlement, row["R"])
        assert (result.loan.principal, result.loan.interest) == (
            row["p1"],
            row["i1"],
        ), row["tx"]


def test_liquidation_rows_seize_at_most_the_legal_maximum():
    single = [row for row in _rows() if len(row["collaterals"]) == 1]
    assert len(single) == 446
    exact = 0
    for row in single:
        (coll,) = row["collaterals"]
        legal = lqm.seized_qtokens(
            row["R"],
            borrow_price=Fraction(*row["borrow_price"]),
            discount=Fraction(*coll["disc"]),
            rate=Fraction(*coll["s9"]),
            price=Fraction(*coll["price"]),
        )
        assert legal >= coll["seized"], row["tx"]
        exact += legal == coll["seized"]
    assert exact >= 431


# --- close, rebase, health debt --------------------------------------------------


def test_close_of_the_lp_loan_pays_the_settled_debt():
    # LP-S close 6e7a7e5b4f5a42a1b2ba4383ed0332cbdf9b99a2e4e0cfbd3e4865d1ca778832: the
    # payment is the settled debt whenever the minimum interest is at most iS.
    for m0 in (0, 114_985_259):
        loan = _loan(9_102_322_242, 90_211_096, m0, 17_107_781)
        assert terms.close_payment(loan, 17_153_887) == 9_217_307_501
        assert terms.close_deltas(loan, 17_153_887) == ActionDeltas(
            principal=-9_102_322_242,
            interest=-114_985_259,
        )


def test_close_of_the_79e2bda3_loan_pays_the_settled_debt():
    # Ada loan 79e2bda3acd86e9188c5a008fb825437e9d91a04d871b38961f05d907cc00902#40
    # settled at the market index: no minimum interest, so the close pays Ds.
    loan = _loan(383_321_869, 0, 0, 19_359_639_391_292_492)
    s5 = 19_764_188_185_007_556
    assert terms.close_payment(loan, s5) == 391_331_957
    deltas = terms.close_deltas(loan, s5)
    assert deltas == ActionDeltas(principal=-383_321_869, interest=-8_010_088)
    assert deltas.underlying_delta() == 391_331_957


@pytest.mark.parametrize(
    ("principal", "interest", "min_interest", "pay", "top_up"),
    [
        # STRIKE-ADA close 23f99dd5dbb4f50c57b99aef460cdf494d1627b8f9091ab481e2e9b8e79f763b
        (601_207_729, 159_330, 480_967, 601_688_696, 321_637),
        # STRIKE-USDCx close ab69b4412e05aa1b7c4a3eb6d475950f62a59d8be7a2a09814e2f8b931d09d92
        (30_199_061, 1_677, 24_160, 30_223_221, 22_483),
    ],
)
def test_close_tops_up_the_minimum_interest(
    principal, interest, min_interest, pay, top_up
):
    # the loans as settled: interest already iS, index at s5
    s5 = 16_920_447
    loan = _loan(principal, interest, min_interest, s5)
    assert terms.close_payment(loan, s5) == pay
    deltas = terms.close_deltas(loan, s5)
    assert deltas == ActionDeltas(
        principal=-principal,
        interest=-interest,
        min_interest_paid=top_up,
    )
    assert deltas.underlying_delta() == pay


def test_close_of_zero_owed_loans():
    index = 16_777_216
    zero = _loan(0, 0, 0, index)
    assert terms.close_payment(zero, 19_000_000) == 0
    assert terms.close_deltas(zero, 19_000_000) == ActionDeltas()
    minimum_only = _loan(0, 0, 82, index)
    assert terms.close_payment(minimum_only, 19_000_000) == 82
    assert terms.close_deltas(minimum_only, 19_000_000) == ActionDeltas(
        principal=0,
        interest=0,
        min_interest_paid=82,
    )


def test_rebase_for_collateral():
    # 2a2930b0ef53cb7f4b5f9705cd28889e576559c0fc798224393666e9ecbc6063: x0 < s5 re-bases
    loan = _loan(578_250_654, 0, 489_346, 19_761_242_199_725_666)
    assert terms.rebase_for_collateral(loan, 19_763_163_649_327_205) == _loan(
        578_250_654,
        56_226,
        489_346,
        19_763_163_677_196_094,
    )
    # x0 >= s5 keeps the loan as it is, clamp case included
    clamp = _loan(CLAMP_OWED - CLAMP_INTEREST, CLAMP_INTEREST, 0, CLAMP_LOAN_INDEX)
    assert terms.rebase_for_collateral(clamp, CLAMP_MARKET_INDEX) is clamp
    assert terms.rebase_for_collateral(loan, loan.index) is loan


@pytest.mark.parametrize(
    ("args", "debt"),
    [
        # borrow-more 2a2930b0: p0 + m0 + A
        ((578_250_654, 56_226, 489_346, 103_000_000, 0), 681_740_000),
        # borrow-more efac5202: p0 + iS + A
        (
            (867_698_782_924, 95_540_305_500, 482_960_000, 27_000_000_000, 0),
            990_239_088_424,
        ),
        # repay 89f443ff, variants A to D: p0 + max(iS, m0) - R
        ((125_000_000, 1_056_667, 100_000, 0, 42_130_664), 83_926_003),
        ((125_000_000, 1_056_667, 5_000_000, 0, 42_130_664), 87_869_336),
        ((125_000_000, 61_394_551, 0, 0, 42_130_664), 144_263_887),
        ((125_000_000, 61_394_551, 80_000_000, 0, 42_130_664), 162_869_336),
        # open: D = A
        ((0, 0, 0, 50_811_360_333, 0), 50_811_360_333),
    ],
)
def test_health_debt(args, debt):
    assert terms.health_debt(*args) == debt


# --- import isolation ------------------------------------------------------------


TERMS_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "enum",
    "fractions",
    "typing",
    "charli3_dendrite.lending.liqwid",
    "charli3_dendrite.lending.liqwid.datums",
    "charli3_dendrite.lending.liqwid.transactions.errors",
    "charli3_dendrite.lending.math",
}


def _imported_modules(source: str) -> set[str]:
    """Every module ``source`` imports, at any depth of the syntax tree."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    return imported


def test_imported_modules_sees_nested_imports():
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from pycardano import Address\n"
        "try:\n"
        "    import requests\n"
        "except ImportError:\n"
        "    pass\n"
        "def build():\n"
        "    import charli3_dendrite.backend\n"
        "class Builder:\n"
        "    def run(self):\n"
        "        from . import oracles\n"
    )
    assert _imported_modules(source) == {
        "typing",
        "pycardano",
        "requests",
        "charli3_dendrite.backend",
        ".",
    }


def test_terms_imports_only_records_arithmetic_and_errors():
    imported = _imported_modules(Path(terms.__file__).read_text())
    assert imported <= TERMS_IMPORTS, sorted(imported - TERMS_IMPORTS)


def test_importing_terms_loads_no_oracles_deployment_or_other_protocol():
    code = (
        "import sys; "
        "import charli3_dendrite.lending.liqwid.transactions.terms; "
        "bad = sorted(m for m in sys.modules if m in ("
        "'charli3_dendrite.lending.liqwid.oracles', "
        "'charli3_dendrite.lending.liqwid.deployment', "
        "'charli3_dendrite.lending.liqwid.market', "
        "'charli3_dendrite.lending.liqwid.indexing') "
        "or m.startswith(('charli3_dendrite.lending.fluidtokens', "
        "'charli3_dendrite.lending.danogo'))); "
        "assert not bad, bad"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
