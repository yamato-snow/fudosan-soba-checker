"""税引後・出口（B-2）。耐用年数・償却・累進・損益通算・デッドクロス・譲渡を式で押さえる。"""

from __future__ import annotations

import itertools

import pytest

from app.services.cashflow import (
    Acquisition,
    CapexPlan,
    CashflowInput,
    DepreciationInput,
    ExitPlan,
    Loan,
    OperatingCost,
    RentPlan,
    TaxProfile,
    depreciation_schedule,
    exit_result,
    income_tax,
    sensitivity_by_building_ratio,
    simulate,
    tax_delta,
    useful_life,
)
from app.services.tax_tables import (
    RESIDENT_TAX_RATE,
    SPECIAL_RECONSTRUCTION_RATE,
    TAX_TABLE_VERSION,
    normalize_structure,
    statutory_useful_life,
)


def data(**kwargs) -> CashflowInput:
    base = {
        "acquisition": Acquisition(
            price=15_000_000, acquisition_costs=1_000_000, building_ratio=0.4
        ),
        "rent": RentPlan(monthly_rent=90_000, vacancy_rate=0.05),
        "operating": OperatingCost(
            monthly_admin_fee=8_000, monthly_repair_reserve=7_000, annual_property_tax=80_000
        ),
        "depreciation": DepreciationInput(structure="ＲＣ", building_age=20),
        "loan": Loan(principal=12_000_000, annual_rate=0.02, years=25),
        "tax": TaxProfile(other_income=5_000_000),
    }
    return CashflowInput(**{**base, **kwargs})


# --- 構造の語彙（A-1 で観測した実データは全角） -------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ＲＣ", 47),  # 全角（XIT001 の実データ語彙）
        ("ＳＲＣ", 47),
        ("RC", 47),
        ("SRC", 47),
        ("鉄筋コンクリート造", 47),
        ("木造", 22),
        ("軽量鉄骨造", 27),
        ("鉄骨造", 34),
        ("ＲＣ造", 47),
    ],
)
def test_structure_vocabulary_resolves_zenkaku(raw, expected):
    assert statutory_useful_life(raw) == expected


def test_unknown_structure_is_not_guessed():
    assert normalize_structure("ブロック造") is None
    assert statutory_useful_life("") is None


# --- 耐用年数（中古簡便法） ---------------------------------------------


@pytest.mark.parametrize(
    ("structure", "age", "expected"),
    [
        ("木造", 25, 4),  # 全部経過: 22 × 0.2 = 4.4 → 切捨て 4
        ("木造", 10, 14),  # 一部経過: (22−10) + 10×0.2 = 14
        ("ＲＣ", 20, 31),  # (47−20) + 20×0.2 = 31
        ("ＲＣ", 0, 47),  # 新築は法定どおり
        ("木造", 100, 4),  # 経過しすぎても 22×0.2
        ("軽量鉄骨造", 30, 5),  # 27 × 0.2 = 5.4 → 5
    ],
)
def test_useful_life_matches_the_official_examples(structure, age, expected):
    assert useful_life(structure, age) == expected


def test_useful_life_has_a_floor_of_two_years():
    assert useful_life("軽量鉄骨(骨格材3mm以下)", 100) == 3  # 19 × 0.2 = 3.8 → 3
    assert useful_life(None, 0, override=2) == 2
    with pytest.raises(ValueError):
        useful_life(None, 0, override=1)


def test_useful_life_refuses_to_guess_an_unknown_structure():
    with pytest.raises(ValueError):
        useful_life("ブロック造", 10)


# --- 減価償却 -----------------------------------------------------------


def test_depreciation_sums_to_building_price_minus_one_yen():
    schedule = depreciation_schedule(10_000_000, 10, 30)
    assert sum(schedule) == 10_000_000 - 1  # 備忘価額1円を残す
    assert all(v > 0 for v in schedule[:10])
    assert all(v == 0 for v in schedule[10:])  # 耐用年数超過年は0


def test_depreciation_handles_a_life_longer_than_the_simulation():
    schedule = depreciation_schedule(10_000_000, 47, 30)
    assert len(schedule) == 30
    assert sum(schedule) < 10_000_000


def test_no_building_price_means_no_depreciation():
    assert depreciation_schedule(0, 10, 5) == [0] * 5


# --- 所得税（超過累進＋復興特別） ---------------------------------------


@pytest.mark.parametrize("boundary", [1_950_000, 3_300_000, 6_950_000, 9_000_000, 18_000_000, 40_000_000])
def test_progressive_boundaries_are_continuous_within_one_yen(boundary):
    """境界の上下1円で税額が飛ばない（速算控除が正しく効いている）。"""
    below = income_tax(boundary - 1)
    at = income_tax(boundary)
    above = income_tax(boundary + 1)
    assert below <= at <= above
    assert above - below <= 2  # 1円ぶんの差＋丸め


def test_special_reconstruction_tax_is_on_the_tax_amount_not_the_income():
    """復興特別所得税は**所得税額に対して** 2.1%。課税所得に掛けると桁違いに増える。"""
    taxable = 5_000_000
    base = taxable * 0.20 - 427_500
    assert income_tax(taxable) == round(base * (1 + SPECIAL_RECONSTRUCTION_RATE))


def test_no_income_means_no_tax():
    assert income_tax(0) == 0
    assert income_tax(-1_000_000) == 0


# --- 損益通算（差分方式） -----------------------------------------------


def test_tax_delta_is_negative_when_the_property_runs_a_loss():
    """赤字なら差分が負＝還付方向。特別扱いの分岐を書かずに損益通算がモデル化される。"""
    profile = TaxProfile(other_income=5_000_000)
    income_delta, resident_delta = tax_delta(profile, -1_000_000)
    assert income_delta < 0
    assert resident_delta == round(-1_000_000 * RESIDENT_TAX_RATE)


def test_tax_delta_is_positive_when_the_property_is_profitable():
    profile = TaxProfile(other_income=5_000_000)
    income_delta, resident_delta = tax_delta(profile, 1_000_000)
    assert income_delta > 0
    assert resident_delta == 100_000


def test_tax_delta_never_refunds_more_than_the_other_income_carries():
    """他所得を食い尽くすほどの赤字でも、住民税の還付は他所得ぶんで止まる。"""
    profile = TaxProfile(other_income=1_000_000)
    _income_delta, resident_delta = tax_delta(profile, -5_000_000)
    assert resident_delta == -100_000


# --- デッドクロス -------------------------------------------------------


def test_dead_cross_starts_the_year_after_depreciation_ends():
    result = simulate(data(depreciation=DepreciationInput(structure="木造", building_age=25)))
    assert result.useful_life == 4
    # 償却が乗る4年は元金 < 償却、5年目から元金 > 償却
    assert all(not r.dead_cross for r in result.years[:4])
    assert result.years[4].dead_cross
    assert result.dead_cross_year == 5
    # 返済が続く25年目までは毎年デッドクロス。完済後（26年目〜）は元金返済が無いので該当しない
    for row in result.years[4:25]:
        assert row.dead_cross and row.loan_principal > row.depreciation
    assert all(not r.dead_cross and r.loan_principal == 0 for r in result.years[25:])


def test_dead_cross_row_flag_and_summary_agree():
    result = simulate(data())
    flagged = [r.year for r in result.years if r.dead_cross]
    assert result.dead_cross_year == (flagged[0] if flagged else None)


# --- 出口 ---------------------------------------------------------------


def test_five_years_exactly_is_short_term():
    plan = ExitPlan(year=5, sale_price=16_000_000, sale_cost_rate=0.03)
    result = exit_result(
        plan, acquisition_total=16_000_000, accumulated_depreciation=2_000_000, loan_balance=0
    )
    assert result.is_long_term is False
    assert result.transfer_tax == round(result.capital_gain * 0.3963)


def test_six_years_is_long_term():
    plan = ExitPlan(year=6, sale_price=16_000_000, sale_cost_rate=0.03)
    result = exit_result(
        plan, acquisition_total=16_000_000, accumulated_depreciation=2_000_000, loan_balance=0
    )
    assert result.is_long_term is True
    assert result.transfer_tax == round(result.capital_gain * 0.20315)


def test_a_loss_on_sale_means_no_transfer_tax():
    plan = ExitPlan(year=10, sale_price=5_000_000)
    result = exit_result(
        plan, acquisition_total=16_000_000, accumulated_depreciation=0, loan_balance=0
    )
    assert result.capital_gain < 0
    assert result.transfer_tax == 0


def test_book_value_is_acquisition_minus_accumulated_depreciation():
    plan = ExitPlan(year=10, sale_price=16_000_000, sale_cost_rate=0.0)
    result = exit_result(
        plan, acquisition_total=16_000_000, accumulated_depreciation=6_000_000, loan_balance=3_000_000
    )
    assert result.book_value == 10_000_000
    assert result.capital_gain == 6_000_000
    assert result.net_sale_proceeds == 16_000_000 - result.transfer_tax - 3_000_000


def test_total_profit_identity():
    """総利益＝累計ATCF＋売却手取り−自己資金（保有年数までで数える）。"""
    result = simulate(data(exit=ExitPlan(year=10, sale_price=14_000_000)))
    held = result.years[:10]
    assert result.total_profit == sum(r.atcf for r in held) + result.exit.net_sale_proceeds - result.equity


def test_exit_year_outside_the_simulation_is_rejected():
    with pytest.raises(ValueError):
        simulate(data(years=10, exit=ExitPlan(year=11, sale_price=14_000_000)))


# --- 建物割合 -----------------------------------------------------------


@pytest.mark.parametrize("ratio", [None, 0.0, 1.0, -0.1, 1.5])
def test_building_ratio_must_be_supplied_and_between_zero_and_one(ratio):
    """既定値で計算が走らない＝触らないまま「もっともらしい数字」が出ない。"""
    with pytest.raises(ValueError):
        simulate(data(acquisition=Acquisition(price=15_000_000, building_ratio=ratio)))


def test_pretax_mode_does_not_require_a_building_ratio():
    result = simulate(data(acquisition=Acquisition(price=15_000_000), tax=None))
    assert result.years[0].depreciation == 0


def test_sensitivity_gives_three_points_clamped_into_range():
    points = sensitivity_by_building_ratio(data(exit=ExitPlan(year=10, sale_price=14_000_000)))
    assert [round(r, 2) for r, _ in points] == [0.30, 0.40, 0.50]
    # 建物割合が高いほど償却が増え、総利益は増える方向に動く
    profits = [p for _r, p in points]
    assert profits[0] < profits[2]


def test_sensitivity_clamps_at_the_edges():
    points = sensitivity_by_building_ratio(
        data(acquisition=Acquisition(price=15_000_000, building_ratio=0.05))
    )
    assert all(0 < r < 1 for r, _ in points)


def test_sensitivity_is_empty_without_a_ratio():
    assert sensitivity_by_building_ratio(data(acquisition=Acquisition(price=15_000_000))) == []


# --- None の全組み合わせ ------------------------------------------------


@pytest.mark.parametrize(
    ("with_loan", "with_tax", "with_exit"),
    list(itertools.product([True, False], repeat=3)),
)
def test_every_none_combination_runs(with_loan, with_tax, with_exit):
    result = simulate(
        data(
            loan=Loan(principal=12_000_000, annual_rate=0.02, years=25) if with_loan else None,
            tax=TaxProfile(other_income=5_000_000) if with_tax else None,
            exit=ExitPlan(year=10, sale_price=14_000_000) if with_exit else None,
        )
    )
    assert len(result.years) == 30
    assert (result.exit is not None) is with_exit
    assert (result.total_profit is not None) is with_exit
    if not with_loan:
        assert result.first_year.dscr is None
    if not with_tax:
        assert all(r.income_tax == 0 for r in result.years)


# --- 土地取得借入金利子の警告（条件付き） -------------------------------


def test_loss_year_is_detectable_for_the_land_interest_warning():
    """借入あり＋赤字年あり のときだけ警告を出すための判定材料。"""
    loss = simulate(data(rent=RentPlan(monthly_rent=40_000, vacancy_rate=0.2)))
    assert loss.has_loss_year is True
    profitable = simulate(
        data(rent=RentPlan(monthly_rent=200_000), operating=OperatingCost(), loan=None)
    )
    assert profitable.has_loss_year is False


def test_capex_treated_as_expense_reduces_the_real_estate_income():
    plain = simulate(data())
    with_capex = simulate(data(capex=CapexPlan(annual_amount=100_000, treat_as_expense=True)))
    assert with_capex.years[0].real_estate_income < plain.years[0].real_estate_income
    assert with_capex.years[0].btcf < plain.years[0].btcf


def test_tax_table_version_is_pinned():
    assert TAX_TABLE_VERSION == "2026-01"
