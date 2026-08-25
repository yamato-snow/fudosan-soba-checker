"""賃料判定の純粋ロジック（統計エンジンを無変更で流用できていること）。"""

from __future__ import annotations

import pytest

from app.constants import (
    BAND_CHEAP,
    BAND_EXPENSIVE,
    BAND_FAIR,
    CONFIDENCE_A,
    CONFIDENCE_B,
    CONFIDENCE_NONE,
    RENT_KIND_CONDO,
    RENT_KIND_HOUSE,
)
from app.services.rent import (
    EXPANSION_STAGES_RENT_CONDO,
    EXPANSION_STAGES_RENT_HOUSE,
    RENT_AREA_LABEL_CONDO,
    RentCase,
    build_assessment,
    rent_cases_of,
    shortage_message,
    stages_for_rent,
    survey_year_range,
    target_unit_price_for_rent,
)
from app.services.valuation import evaluate, evaluate_with_expansion

AREA = 62.5


def case(unit_price: float, *, survey_year: int = 2026, **kwargs) -> RentCase:
    base = {
        "district_name": "地区40",
        "property_kind": RENT_KIND_CONDO,
        "monthly_rent": int(unit_price * AREA) - 5_000,
        "admin_fee": 5_000,
        "exclusive_area": AREA,
        "layout": "3LDK",
        "building_year": 2005,
        "survey_year": survey_year,
        "survey_month": 8,
        "status": "募集中",
    }
    return RentCase(rent_unit_price=unit_price, **{**base, **kwargs})


def assess(cases, target_monthly=None, stages=EXPANSION_STAGES_RENT_CONDO):
    valuation = evaluate_with_expansion(
        lambda _criteria: [c.to_valuation_case() for c in cases],
        target_unit_price_for_rent(AREA, target_monthly),
        stages=stages,
    )
    return build_assessment(
        valuation,
        property_kind=RENT_KIND_CONDO,
        exclusive_area=AREA,
        target_monthly=target_monthly,
    )


# --- RentCase → Case 変換 ----------------------------------------------


def test_to_valuation_case_puts_rent_unit_price_in_unit_price():
    rent_case = case(1440.0)
    converted = rent_case.to_valuation_case()
    assert converted.unit_price == pytest.approx(1440.0)
    assert converted.trade_price == rent_case.total_monthly == 90_000
    assert converted.area == AREA
    assert converted.source is rent_case  # 表示側が賃料の語彙を取り戻せる


def test_rent_cases_of_recovers_the_original_objects():
    cases = [case(1400.0), case(1500.0), case(1600.0), case(1700.0), case(1800.0)]
    valuation = evaluate([c.to_valuation_case() for c in cases], 1500.0)
    assert rent_cases_of(valuation) == cases


def test_target_unit_price_requires_positive_area_and_rent():
    assert target_unit_price_for_rent(AREA, 90_000) == pytest.approx(1440.0)
    with pytest.raises(ValueError):
        target_unit_price_for_rent(0, 90_000)
    with pytest.raises(ValueError):
        target_unit_price_for_rent(AREA, 0)


# --- 段階拡張 -----------------------------------------------------------


def test_rent_stages_never_relax_land_area():
    """賃料では土地面積で絞らない＝緩和説明に土地面積が出てはいけない。"""
    for stages in (EXPANSION_STAGES_RENT_CONDO, EXPANSION_STAGES_RENT_HOUSE):
        tolerances = {s.area_tolerance for s in stages}
        assert len(tolerances) == 1
        for stage in stages:
            assert not any("土地面積" in note for note in stage.relaxations(stages[0]))


def test_rent_stage_relaxation_uses_floor_label():
    stages = EXPANSION_STAGES_RENT_CONDO
    notes = stages[-1].relaxations(stages[0])
    assert f"{RENT_AREA_LABEL_CONDO}を±50%に緩和" in notes
    assert "抽出範囲を対象市全域まで拡大" in notes
    assert "築年の条件を撤廃" in notes


def test_house_stages_keep_the_existing_wording():
    stages = EXPANSION_STAGES_RENT_HOUSE
    assert "延床を±50%に緩和" in stages[-1].relaxations(stages[0])


def test_stages_for_rent_picks_by_kind():
    assert stages_for_rent(RENT_KIND_CONDO) is EXPANSION_STAGES_RENT_CONDO
    assert stages_for_rent(RENT_KIND_HOUSE) is EXPANSION_STAGES_RENT_HOUSE


# --- 判定 ---------------------------------------------------------------


def test_five_cases_at_the_first_stage_give_confidence_a():
    cases = [case(u) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]
    a = assess(cases)
    assert a.judgeable
    assert a.valuation.confidence == CONFIDENCE_A
    assert a.valuation.case_count == 5
    # 想定賃料を入れていないので比較は見せない（番人の値で出た数字を絶対に出さない）
    assert a.has_target is False
    assert a.ratio_percent is None
    assert a.band is None


def test_monthly_range_is_quartiles_times_area():
    cases = [case(u) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]
    a = assess(cases)
    assert a.median_monthly == round(1440.0 * AREA)
    assert a.low_monthly == round(a.valuation.q1 * AREA)
    assert a.high_monthly == round(a.valuation.q3 * AREA)
    assert a.low_monthly < a.median_monthly < a.high_monthly


@pytest.mark.parametrize(
    ("target_monthly", "band"),
    [(70_000, BAND_CHEAP), (90_000, BAND_FAIR), (130_000, BAND_EXPENSIVE)],
)
def test_target_rent_is_placed_in_a_band(target_monthly, band):
    cases = [case(u) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]
    a = assess(cases, target_monthly=target_monthly)
    assert a.has_target is True
    assert a.band == band
    assert a.target_monthly == target_monthly


def test_zero_cases_is_not_judgeable_and_asks_for_more():
    a = assess([])
    assert a.judgeable is False
    assert a.valuation.confidence == CONFIDENCE_NONE
    assert a.shortfall == 3
    assert "あと 3 件登録すると判定できます" in shortage_message(a.shortfall)
    assert a.median_monthly is None


def test_two_cases_still_not_judgeable_but_shortfall_shrinks():
    a = assess([case(1400.0), case(1500.0)])
    assert a.judgeable is False
    assert a.shortfall == 1


def test_three_cases_are_judgeable_with_low_confidence():
    a = assess([case(1400.0), case(1440.0), case(1500.0)])
    assert a.judgeable is True
    assert a.shortfall == 0
    assert a.valuation.confidence != CONFIDENCE_A


def test_confidence_drops_to_b_when_expansion_was_needed():
    """初期段で足りず広げた場合は B。fetch は段が進むほど件数が増える形で模す。"""
    cases = [case(u) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]

    def fetch(criteria):
        return [c.to_valuation_case() for c in cases] if criteria.ring >= 2 else []

    valuation = evaluate_with_expansion(
        fetch, target_unit_price_for_rent(AREA, None), stages=EXPANSION_STAGES_RENT_CONDO
    )
    assert valuation.confidence == CONFIDENCE_B


# --- 鮮度の見せ方（係数に溶かさず表に出す） -----------------------------


def test_survey_year_range_is_reported():
    cases = [
        case(1400.0, survey_year=2023),
        case(1440.0, survey_year=2025),
        case(1500.0, survey_year=2026),
        case(1600.0, survey_year=2024),
        case(1300.0, survey_year=2026),
    ]
    a = assess(cases)
    assert (a.survey_year_from, a.survey_year_to) == (2023, 2026)
    assert survey_year_range([]) is None


def test_old_cases_are_not_demoted_or_weighted():
    """古い事例でも重みを掛けない・信頼度も下げない（恣意的な係数を統計の芯に入れない）。"""
    fresh = [case(u, survey_year=2026) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]
    old = [case(u, survey_year=2015) for u in (1300.0, 1400.0, 1440.0, 1500.0, 1600.0)]
    a_fresh, a_old = assess(fresh), assess(old)
    assert a_fresh.median_monthly == a_old.median_monthly
    assert a_fresh.valuation.confidence == a_old.valuation.confidence
