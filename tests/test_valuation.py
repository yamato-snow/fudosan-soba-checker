"""判定ロジック（純粋関数）の単体テスト。"""

from __future__ import annotations

import pytest

from app.constants import (
    BAND_CHEAP,
    BAND_EXPENSIVE,
    BAND_FAIR,
    CONFIDENCE_A,
    CONFIDENCE_B,
    CONFIDENCE_C,
    CONFIDENCE_NONE,
    TYPE_CONDO,
    TYPE_HOUSE,
    TYPE_LAND,
)
from app.services.valuation import (
    EXPANSION_STAGES,
    EXPANSION_STAGES_CONDO,
    EXPANSION_STAGES_HOUSE,
    EXPANSION_STAGES_LAND,
    Case,
    Criteria,
    evaluate,
    evaluate_with_expansion,
    stages_for,
    trim_cases,
    unit_price_for_target,
)


def cases(*unit_prices: float) -> list[Case]:
    return [Case(unit_price=p) for p in unit_prices]


def test_median_ratio_and_band_cheap():
    v = evaluate(cases(100_000, 110_000, 120_000, 130_000, 140_000), target_unit_price=102_000)
    assert v.judgeable
    assert v.median_unit_price == 120_000
    assert v.ratio_percent == pytest.approx(-15.0)  # 相場中央値比 −15%
    assert v.band == BAND_CHEAP  # Q1(110,000) 未満
    assert v.case_count == 5
    assert v.confidence == CONFIDENCE_A


def test_band_fair_and_expensive():
    sample = cases(100_000, 110_000, 120_000, 130_000, 140_000)
    assert evaluate(sample, 120_000).band == BAND_FAIR
    assert evaluate(sample, 135_000).band == BAND_EXPENSIVE  # Q3(130,000) 超


def test_confidence_a_requires_initial_stage():
    """同じ5件でも、拡張後に得た標本は A にしない。"""
    initial = evaluate(cases(*[100_000] * 5), 100_000, criteria=EXPANSION_STAGES[0], stage_index=0)
    expanded = evaluate(cases(*[100_000] * 5), 100_000, criteria=EXPANSION_STAGES[2], stage_index=2)
    assert initial.confidence == CONFIDENCE_A
    assert expanded.confidence == CONFIDENCE_B
    assert expanded.ring_label == "対象市全域"


def test_confidence_c_for_three_or_four_cases():
    v = evaluate(cases(100_000, 120_000, 140_000), 130_000, EXPANSION_STAGES[-1], stage_index=5)
    assert v.judgeable
    assert v.confidence == CONFIDENCE_C
    assert v.case_count == 3


def test_zero_cases_is_unjudgeable():
    v = evaluate([], 120_000)
    assert not v.judgeable
    assert v.confidence == CONFIDENCE_NONE
    assert v.median_unit_price is None
    assert "判定できません" in v.message


def test_two_cases_is_unjudgeable():
    v = evaluate(cases(100_000, 120_000), 110_000, EXPANSION_STAGES[-1], stage_index=5)
    assert not v.judgeable
    assert v.confidence == CONFIDENCE_NONE
    assert v.case_count == 2


def test_expansion_stops_at_first_stage_with_enough_cases():
    fetched: list[Criteria] = []

    def fetch(criteria: Criteria):
        fetched.append(criteria)
        # Ring0-1 は2件しかなく、Ring2 まで広げると6件になる
        if criteria.ring <= 1:
            return cases(100_000, 110_000)
        return cases(90_000, 100_000, 110_000, 120_000, 130_000, 140_000)

    v = evaluate_with_expansion(fetch, target_unit_price=100_000)

    assert [c.ring for c in fetched] == [1, 2]  # 3段目以降は引かない
    assert v.case_count == 6
    assert v.confidence == CONFIDENCE_B
    assert v.ring_label == "基準市全域"
    assert "抽出範囲を基準市全域まで拡大" in v.relaxations


def test_expansion_relaxes_year_then_area_and_reports_it():
    def fetch(criteria: Criteria):
        # 築年条件が外れるまで足りない
        if criteria.year_tolerance is not None:
            return cases(100_000)
        return cases(80_000, 90_000, 100_000, 110_000, 120_000)

    v = evaluate_with_expansion(fetch, target_unit_price=85_000)

    assert v.case_count == 5
    assert v.confidence == CONFIDENCE_B
    assert "築年の条件を撤廃" in v.relaxations
    assert v.band == BAND_CHEAP


def test_expansion_exhausted_returns_unjudgeable_with_last_criteria():
    def fetch(criteria: Criteria):
        return []

    v = evaluate_with_expansion(fetch, target_unit_price=100_000)

    assert not v.judgeable
    assert v.case_count == 0
    assert v.criteria == EXPANSION_STAGES[-1]  # 最後まで広げたことが結果に残る
    assert v.ring_label == "対象市全域"


def test_expansion_exhausted_with_four_cases_is_confidence_c():
    def fetch(criteria: Criteria):
        if criteria == EXPANSION_STAGES[-1]:
            return cases(100_000, 110_000, 120_000, 130_000)
        return cases(100_000)

    v = evaluate_with_expansion(fetch, target_unit_price=100_000)
    assert v.judgeable
    assert v.confidence == CONFIDENCE_C
    assert v.case_count == 4


def test_unit_price_for_target():
    assert unit_price_for_target(TYPE_LAND, 25_000_000, 200, None) == 125_000
    assert unit_price_for_target(TYPE_HOUSE, 25_000_000, 200, 100) == 250_000  # 戸建は延床基準
    # 区分は Area が専有面積。延床が無くても（あっても無視して）専有面積で割る
    assert unit_price_for_target(TYPE_CONDO, 15_000_000, 75, None) == 200_000
    assert unit_price_for_target(TYPE_CONDO, 15_000_000, 75, 80) == 200_000


def test_unit_price_for_target_requires_total_floor_area_for_house():
    with pytest.raises(ValueError):
        unit_price_for_target(TYPE_HOUSE, 25_000_000, 200, None)
    with pytest.raises(ValueError):
        unit_price_for_target(TYPE_LAND, 25_000_000, 0, None)


def test_unit_price_for_target_requires_area_for_condo():
    """区分の分岐を落とすと戸建側に落ちて「延床が必要」と誤ったエラーになる回帰テスト。"""
    with pytest.raises(ValueError, match="専有面積"):
        unit_price_for_target(TYPE_CONDO, 15_000_000, 0, None)


def test_evaluate_rejects_non_positive_target():
    with pytest.raises(ValueError):
        evaluate(cases(100_000), 0)


def test_cases_without_unit_price_are_excluded():
    sample = [Case(unit_price=100_000), Case(unit_price=0), Case(unit_price=120_000)]
    v = evaluate(sample, 110_000, EXPANSION_STAGES[-1], stage_index=5)
    assert v.case_count == 2  # unit_price なしは母集団に入らない


# --- 種別別の段階拡張（軸ズレ修正） ---


def test_stages_are_separated_by_land_type():
    assert stages_for(TYPE_LAND) is EXPANSION_STAGES_LAND
    assert stages_for(TYPE_HOUSE) is EXPANSION_STAGES_HOUSE
    assert stages_for(TYPE_CONDO) is EXPANSION_STAGES_CONDO
    # 未知の種別は土地の並びに落とす（例外を投げて画面を落とさない）
    assert stages_for("農地") is EXPANSION_STAGES_LAND
    # 土地は現行どおり延床条件なし（6段）
    assert len(EXPANSION_STAGES_LAND) == 6
    assert all(c.floor_tolerance is None for c in EXPANSION_STAGES_LAND)


def test_house_stages_relax_floor_to_50_percent_and_never_drop_it():
    tolerances = [c.floor_tolerance for c in EXPANSION_STAGES_HOUSE]
    assert tolerances == [0.3, 0.3, 0.3, 0.3, 0.5, 0.5, 0.5]
    # 延床は撤廃しない（None が出てこない）＝「延床無条件」の壊れ方に戻らない
    assert None not in tolerances
    # 緩和順: リング → 築年 → 延床 → 築年撤廃 → 土地面積
    assert [c.ring for c in EXPANSION_STAGES_HOUSE] == [1, 2, 3, 3, 3, 3, 3]
    assert [c.year_tolerance for c in EXPANSION_STAGES_HOUSE] == [5, 5, 5, 10, 10, None, None]
    assert [c.area_tolerance for c in EXPANSION_STAGES_HOUSE] == [0.3] * 6 + [0.5]


def test_house_relaxations_report_floor_widening():
    base = EXPANSION_STAGES_HOUSE[0]
    notes = EXPANSION_STAGES_HOUSE[4].relaxations(base)
    assert "延床を±50%に緩和" in notes
    assert "築年を±10年に緩和" in notes
    # 初期条件そのものは「緩和した」と言わない
    assert base.relaxations(base) == []


def test_condo_stages_have_no_floor_step_and_relax_year_then_area():
    """区分は TotalFloorArea が実データで全件空なので、延床の段を持ってはいけない。"""
    assert all(c.floor_tolerance is None for c in EXPANSION_STAGES_CONDO)
    # 緩和順: リング → 築年 → 築年撤廃 → 専有面積（戸建から延床の2段を抜いた6段）
    assert [c.ring for c in EXPANSION_STAGES_CONDO] == [1, 2, 3, 3, 3, 3]
    assert [c.year_tolerance for c in EXPANSION_STAGES_CONDO] == [5, 5, 5, 10, None, None]
    assert [c.area_tolerance for c in EXPANSION_STAGES_CONDO] == [0.3] * 5 + [0.5]


def test_condo_relaxations_say_exclusive_area_not_land_area():
    """区分で「土地面積を緩和」と出ると、実際に緩んだ軸（専有面積）と食い違う。"""
    notes = EXPANSION_STAGES_CONDO[-1].relaxations(EXPANSION_STAGES_CONDO[0])
    assert "専有面積を±50%に緩和" in notes
    assert not any("土地面積" in n for n in notes)
    # 土地・戸建の文言は従来どおり
    assert "土地面積を±50%に緩和" in EXPANSION_STAGES_LAND[-1].relaxations(EXPANSION_STAGES_LAND[0])


def test_condo_expansion_reports_area_relaxation_end_to_end():
    def fetch(criteria: Criteria):
        if criteria.area_tolerance == 0.3:
            return cases(200_000)
        return cases(180_000, 190_000, 200_000, 210_000, 220_000)

    v = evaluate_with_expansion(fetch, 195_000, stages=EXPANSION_STAGES_CONDO)

    assert v.case_count == 5
    assert "専有面積を±50%に緩和" in v.relaxations
    assert v.confidence == CONFIDENCE_B


def test_house_expansion_reports_floor_relaxation_end_to_end():
    def fetch(criteria: Criteria):
        if criteria.floor_tolerance == 0.3:
            return cases(200_000)
        return cases(180_000, 190_000, 200_000, 210_000, 220_000)

    v = evaluate_with_expansion(fetch, 195_000, stages=EXPANSION_STAGES_HOUSE)

    assert v.case_count == 5
    assert v.criteria.floor_tolerance == 0.5
    assert "延床を±50%に緩和" in v.relaxations


# --- IQR トリム（外れ値耐性） ---


def test_trim_is_skipped_below_six_cases():
    sample = cases(10_000, 100_000, 105_000, 110_000, 900_000)  # n=5
    kept, removed = trim_cases(sample)
    assert removed == []  # 四分位フェンス自体が不安定なので触らない
    assert len(kept) == 5


def test_trim_excludes_outliers_and_recomputes_median_and_quartiles():
    sample = cases(100_000, 110_000, 120_000, 130_000, 140_000, 150_000, 900_000)
    v = evaluate(sample, 125_000, EXPANSION_STAGES_LAND[0], stage_index=0)

    assert v.trimmed_count == 1  # 900,000 は Q3+1.5×IQR の外
    assert v.case_count == 6  # 件数は「実際に使った」件数
    assert v.median_unit_price == 125_000  # 外れ値込みなら 130,000 になっていた
    assert v.q1 == pytest.approx(112_500)
    assert v.q3 == pytest.approx(137_500)
    assert len(v.cases) == 6  # 表示する事例もトリム後


def test_threshold_uses_trimmed_count_so_expansion_continues():
    """生6件でも使える事例が4件なら MIN_CASES 未達として拡張を続ける。"""
    fetched: list[Criteria] = []

    def fetch(criteria: Criteria):
        fetched.append(criteria)
        if criteria.ring <= 1:
            # 上下の外れ値を除くと 4件しか残らない
            return cases(10_000, 100_000, 105_000, 110_000, 115_000, 1_000_000)
        return cases(100_000, 105_000, 110_000, 115_000, 120_000)

    v = evaluate_with_expansion(fetch, 108_000)

    assert [c.ring for c in fetched] == [1, 2]  # 1段目で止まらない
    assert v.case_count == 5
    assert v.trimmed_count == 0


def test_trimmed_count_is_reported_even_when_unjudgeable():
    def fetch(criteria: Criteria):
        if criteria == EXPANSION_STAGES_LAND[-1]:
            return cases(10_000, 100_000, 105_000, 110_000, 115_000, 1_000_000)
        return []

    v = evaluate_with_expansion(fetch, 108_000)

    assert v.case_count == 4
    assert v.trimmed_count == 2
    assert v.confidence == CONFIDENCE_C  # 4件は参考値


# --- ばらつきガード ---


def test_dispersion_guard_demotes_confidence_one_step():
    # Q1=120,000 / Q3=300,000 → 2.5倍（> 1.8）
    v = evaluate(cases(100_000, 120_000, 200_000, 300_000, 400_000), 200_000)

    assert v.dispersion_flag
    assert v.confidence == CONFIDENCE_B  # 初期条件なので本来は A
    assert "ばらつき" in v.confidence_note


def test_dispersion_guard_does_not_fire_on_tight_sample():
    v = evaluate(cases(100_000, 110_000, 120_000, 130_000, 140_000), 120_000)
    assert not v.dispersion_flag
    assert v.confidence == CONFIDENCE_A


def test_dispersion_guard_keeps_confidence_c_at_c():
    v = evaluate(
        cases(100_000, 200_000, 400_000), 200_000, EXPANSION_STAGES_LAND[-1], stage_index=5
    )
    assert v.dispersion_flag
    assert v.confidence == CONFIDENCE_C  # C より下は無い（判定不能とは別物）


# --- 従指標（土地面積単価） ---


def test_median_land_unit_price_is_reported_as_reference():
    sample = [
        Case(unit_price=200_000, land_unit_price=100_000),
        Case(unit_price=210_000, land_unit_price=110_000),
        Case(unit_price=220_000, land_unit_price=120_000),
        Case(unit_price=230_000, land_unit_price=130_000),
        Case(unit_price=240_000, land_unit_price=140_000),
    ]
    v = evaluate(sample, 220_000)
    assert v.median_land_unit_price == 120_000
    assert v.median_unit_price == 220_000  # 主指標は延床単価のまま（判定には従指標を使わない）
