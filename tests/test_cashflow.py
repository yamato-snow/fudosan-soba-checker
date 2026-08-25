"""収支シミュレーションの純粋ロジック（B-1: 税引前）。

恒等式を assert するのがこのファイルの主目的。30年積み上げる計算は、
「なんとなく合っている表」が一番危ないので、合っていることを式で押さえる。
"""

from __future__ import annotations

import pytest

from app.services.cashflow import (
    Acquisition,
    CapexPlan,
    CashflowInput,
    Loan,
    OperatingCost,
    RentPlan,
    amortization_schedule,
    gross_income,
    monthly_payment,
    operating_expenses,
    simulate,
)


def loan(principal=10_000_000, rate=0.02, years=35) -> Loan:
    return Loan(principal=principal, annual_rate=rate, years=years)


def data(**kwargs) -> CashflowInput:
    base = {
        "acquisition": Acquisition(price=15_000_000, acquisition_costs=1_000_000),
        "rent": RentPlan(monthly_rent=90_000, vacancy_rate=0.05),
        "operating": OperatingCost(
            monthly_admin_fee=8_000,
            monthly_repair_reserve=7_000,
            pm_fee_rate=0.05,
            annual_property_tax=80_000,
            annual_insurance=15_000,
            restoration_cost=200_000,
            ad_cost=100_000,
            turnover_years=4.0,
        ),
        "loan": loan(),
    }
    return CashflowInput(**{**base, **kwargs})


# --- 返済表の恒等式 -----------------------------------------------------


def test_monthly_payment_matches_a_known_value():
    """1,000万・年2.0%・35年 → 月額約 33,126 円。"""
    assert monthly_payment(10_000_000, 0.02, 35) == pytest.approx(33_126, abs=1)


def test_monthly_payment_handles_zero_rate_without_dividing_by_zero():
    assert monthly_payment(12_000_000, 0.0, 10) == pytest.approx(100_000)


def test_monthly_payment_rejects_zero_years():
    with pytest.raises(ValueError):
        monthly_payment(10_000_000, 0.02, 0)


@pytest.mark.parametrize("rate", [0.0, 0.005, 0.02, 0.045])
@pytest.mark.parametrize("years", [1, 10, 35])
def test_amortization_identities(rate, years):
    rows = amortization_schedule(loan(rate=rate, years=years))
    assert len(rows) == years
    # ① 元金の合計が借入額と一致する
    assert sum(r.principal for r in rows) == 10_000_000
    # ② 各行で 返済額 = 利息 + 元金
    assert all(r.payment == r.interest + r.principal for r in rows)
    # ③ 返済総額 = 利息総額 + 元金総額
    assert sum(r.payment for r in rows) == sum(r.interest for r in rows) + sum(
        r.principal for r in rows
    )
    # ④ 完済時の残高が 0
    assert rows[-1].loan_balance == 0
    assert all(r.loan_balance >= 0 for r in rows)


def test_zero_rate_schedule_has_no_interest():
    rows = amortization_schedule(loan(rate=0.0, years=10))
    assert sum(r.interest for r in rows) == 0
    assert rows[0].principal == 1_000_000


def test_no_principal_means_no_schedule():
    assert amortization_schedule(loan(principal=0)) == []


def test_negative_rate_is_rejected():
    with pytest.raises(ValueError):
        amortization_schedule(loan(rate=-0.01))


# --- 借入年数 ≷ シミュレーション期間 ------------------------------------


def test_loan_shorter_than_simulation_leaves_later_years_free():
    result = simulate(data(loan=loan(years=10), years=30))
    assert result.years[9].loan_payment > 0
    assert result.years[10].loan_payment == 0
    assert result.years[10].loan_balance == 0
    assert result.years[10].btcf > result.years[9].btcf


def test_loan_longer_than_simulation_warns_about_the_remaining_balance():
    result = simulate(data(loan=loan(years=35), years=30))
    assert result.years[-1].loan_balance > 0
    assert any("残債が残ります" in w for w in result.warnings)


# --- 現金購入 -----------------------------------------------------------


def test_cash_purchase_has_no_dscr():
    result = simulate(data(loan=None))
    assert result.first_year.dscr is None  # ∞ でも 0 でもなく None（画面は「—」）
    assert result.first_year.repayment_ratio is None
    assert result.amortization == []
    assert result.equity == 16_000_000
    assert all(r.loan_payment == 0 for r in result.years)


def test_equity_is_price_plus_costs_minus_loan():
    assert data(loan=loan(principal=12_000_000)).equity == 4_000_000


# --- 収入・運営費 -------------------------------------------------------


def test_gross_income_declines_only_on_the_rent_itself():
    rent = RentPlan(monthly_rent=100_000, monthly_other_income=5_000, rent_decline_rate=0.01)
    assert gross_income(rent, 1) == pytest.approx(1_260_000)
    assert gross_income(rent, 2) == pytest.approx((100_000 * 0.99 + 5_000) * 12)


def test_pm_fee_is_a_rate_on_effective_income():
    cost = OperatingCost(pm_fee_rate=0.05)
    assert operating_expenses(cost, 1_000_000) == pytest.approx(50_000)


def test_turnover_costs_are_spread_evenly():
    """原状回復・AD は 1/turnover_years を毎年按分する（年ごとにガタつかせない）。"""
    cost = OperatingCost(restoration_cost=200_000, ad_cost=100_000, turnover_years=4.0)
    assert operating_expenses(cost, 0) == pytest.approx(75_000)
    result = simulate(data())
    assert result.years[3].operating_expenses == result.years[4].operating_expenses


def test_full_vacancy_makes_noi_negative_and_warns():
    result = simulate(data(rent=RentPlan(monthly_rent=90_000, vacancy_rate=1.0)))
    assert result.years[0].effective_income == 0
    assert result.years[0].noi < 0
    assert any("NOI がマイナス" in w for w in result.warnings)


# --- 指標 ---------------------------------------------------------------


def test_first_year_metrics_follow_the_published_formulas():
    result = simulate(data())
    row = result.years[0]
    metrics = result.first_year
    assert metrics.gross_yield == pytest.approx(row.gross_income / 15_000_000)
    assert metrics.net_yield == pytest.approx(row.noi / 16_000_000)
    assert metrics.dscr == pytest.approx(row.noi / row.loan_payment)
    assert metrics.repayment_ratio == pytest.approx(row.loan_payment / row.gross_income)


def test_btcf_is_noi_minus_payment_minus_capex():
    result = simulate(data(capex=CapexPlan(annual_amount=50_000)))
    row = result.years[0]
    assert row.btcf == round(row.noi - row.loan_payment - row.capex)
    assert row.capex == 50_000


def test_low_dscr_is_warned():
    result = simulate(data(loan=loan(principal=15_000_000, rate=0.04, years=15)))
    assert result.first_year.dscr < 1.0
    assert any("DSCR" in w for w in result.warnings)


# --- 税引前モードでは税の列がすべて0 ------------------------------------


def test_pretax_mode_leaves_every_tax_column_at_zero():
    result = simulate(data(tax=None))
    assert all(r.income_tax == 0 and r.resident_tax == 0 for r in result.years)
    assert all(r.depreciation == 0 and r.real_estate_income == 0 for r in result.years)
    assert all(r.atcf == r.btcf for r in result.years)
    assert result.dead_cross_year is None
    assert result.exit is None and result.total_profit is None


# --- 入力の検証（サービス層で ValueError → ルーターが400に変換） --------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"acquisition": Acquisition(price=0)},
        {"rent": RentPlan(monthly_rent=0)},
        {"years": 0},
        {"rent": RentPlan(monthly_rent=90_000, vacancy_rate=1.5)},
    ],
)
def test_invalid_input_raises_value_error(kwargs):
    with pytest.raises(ValueError):
        simulate(data(**kwargs))


# --- 丸めの規約 ---------------------------------------------------------


def test_every_published_number_is_an_int():
    result = simulate(data())
    for row in result.years:
        for name, value in vars(row).items():
            if name == "dead_cross":
                continue
            assert isinstance(value, int), f"{name} が int ではない: {value!r}"


def test_rounding_never_truncates_downwards():
    """`int()` 切り捨てだと30年で系統的に下振れする。`round()` であることを平均で見る。"""
    rent = RentPlan(monthly_rent=90_001)  # 12倍して端数が出る額
    result = simulate(data(rent=rent, loan=None, operating=OperatingCost()))
    assert result.years[0].gross_income == round(90_001 * 12)
