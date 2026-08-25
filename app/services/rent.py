"""賃料相場の判定（純粋＝DB も FastAPI も触らない）。

売買側の統計エンジン（`evaluate()` / `trim_cases()` / `quartiles()` / `band_for()` /
`_confidence()`）を**一切変えずに**流用する。賃料でやることは3つだけ:

  1. 主指標を「(賃料＋管理費) ÷ 面積」に差し替える（`RentCase.rent_unit_price`）
  2. 段階拡張の並びを賃料用に持つ（土地面積で絞らない ＝ `area_tolerance` は全段固定）
  3. 出てきた㎡単価の中央値・四分位に面積を掛けて「想定月額賃料レンジ」に戻す

賃料でも通奏低音は同じ: 推定モデルを作らない／分布をそのまま見せる／
判定できないときは判定できないと言う。鮮度の減衰重みも成約への補正係数も**作らない**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.constants import (
    MIN_CASES,
    MIN_CASES_JUDGEABLE,
    RENT_KIND_CONDO,
    RENT_KIND_HOUSE,
)
from app.services.valuation import Case, Criteria, Valuation

# 賃料では土地面積で絞らない（借り手は敷地の広さで賃料を決めない）。
# `area_tolerance` は全ステージ同じ値に固定する＝緩和説明に土地面積が出てこない。
RENT_AREA_TOLERANCE_FIXED = 0.3

# 面積の軸は `floor_tolerance` に載せる（区分=専有面積 / 戸建=延床）。
# 「比べる軸（㎡単価の分母）と絞る軸を一致させる」という判定精度改善の原則をそのまま守る。
RENT_AREA_LABEL_CONDO = "専有面積"
RENT_AREA_LABEL_HOUSE = "延床"

# 想定賃料の入力が無いときに `evaluate()` へ渡す番人（比較・バンドは表示しない）。
# `evaluate()` は target > 0 を要求するので、判定を通すためだけの値。
_NO_TARGET_UNIT_PRICE = 1.0


def _rent_stages(area_label: str) -> tuple[Criteria, ...]:
    """賃料の段階拡張: リング → 築年±5 → ±10 → 面積±50% → 築年撤廃。

    面積は ±50% が下限で**撤廃しない**（撤廃すると 20㎡ と 120㎡ が同じ母集団に入り、
    ㎡単価が桁で暴れる。売買の戸建で延床を撤廃しないのと同じ理由）。
    """
    return (
        Criteria(
            ring=1,
            year_tolerance=5,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.3,
            floor_label=area_label,
        ),
        Criteria(
            ring=2,
            year_tolerance=5,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.3,
            floor_label=area_label,
        ),
        Criteria(
            ring=3,
            year_tolerance=5,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.3,
            floor_label=area_label,
        ),
        Criteria(
            ring=3,
            year_tolerance=10,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.3,
            floor_label=area_label,
        ),
        Criteria(
            ring=3,
            year_tolerance=10,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.5,
            floor_label=area_label,
        ),
        Criteria(
            ring=3,
            year_tolerance=None,
            area_tolerance=RENT_AREA_TOLERANCE_FIXED,
            floor_tolerance=0.5,
            floor_label=area_label,
        ),
    )


EXPANSION_STAGES_RENT_CONDO: tuple[Criteria, ...] = _rent_stages(RENT_AREA_LABEL_CONDO)
EXPANSION_STAGES_RENT_HOUSE: tuple[Criteria, ...] = _rent_stages(RENT_AREA_LABEL_HOUSE)

_RENT_STAGES_BY_KIND: dict[str, tuple[Criteria, ...]] = {
    RENT_KIND_CONDO: EXPANSION_STAGES_RENT_CONDO,
    RENT_KIND_HOUSE: EXPANSION_STAGES_RENT_HOUSE,
}


def stages_for_rent(property_kind: str) -> tuple[Criteria, ...]:
    """種別に応じた段階拡張の並び（未知の種別は区分扱い）。"""
    return _RENT_STAGES_BY_KIND.get(property_kind, EXPANSION_STAGES_RENT_CONDO)


@dataclass(frozen=True)
class RentCase:
    """判定に使う賃料事例1件（DB 行でもテスト用のダミーでもよい）。"""

    rent_unit_price: float
    district_name: str = ""
    property_kind: str = ""
    monthly_rent: int = 0
    admin_fee: int = 0
    exclusive_area: float = 0.0
    layout: str = ""
    building_year: int | None = None
    structure: str = ""
    floor_no: int | None = None
    total_floors: int | None = None
    station_walk_min: int | None = None
    parking_included: bool = False
    survey_year: int = 0
    survey_month: int = 0
    status: str = ""
    source_ref: str = ""
    ring: int = 0

    @property
    def total_monthly(self) -> int:
        """主指標の分子（賃料＋管理費）。"""
        return self.monthly_rent + self.admin_fee

    def to_valuation_case(self) -> Case:
        """統計エンジンが読む `Case` へ。自身を `source` に載せて表示側で取り戻せるようにする。"""
        return Case(
            unit_price=self.rent_unit_price,
            district_name=self.district_name,
            land_type=self.property_kind,
            trade_price=self.total_monthly,
            area=self.exclusive_area,
            building_year=self.building_year,
            period_year=self.survey_year,
            period_quarter=self.survey_month,
            ring=self.ring,
            source=self,
        )


def target_unit_price_for_rent(exclusive_area: float, target_monthly: float | None) -> float:
    """想定賃料（管理費込み・月額）÷ 面積。入力が無ければ番人の値を返す。"""
    if exclusive_area <= 0:
        raise ValueError("面積（区分=専有／戸建=延床）は正の数で入力してください")
    if target_monthly is None:
        return _NO_TARGET_UNIT_PRICE
    if target_monthly <= 0:
        raise ValueError("想定賃料は正の数で入力してください（空欄なら相場レンジだけ出します）")
    return target_monthly / exclusive_area


def rent_cases_of(valuation: Valuation) -> list[RentCase]:
    """`Valuation.cases` から `RentCase` を取り戻す（賃料の語彙で表示するため）。"""
    return [c.source for c in valuation.cases if isinstance(c.source, RentCase)]


def survey_year_range(cases: Sequence[RentCase]) -> tuple[int, int] | None:
    """最終標本の調査時点の最古〜最新。

    古さは**人間が判断すべき情報**なので、減衰係数に溶かして隠さず必ず表に出す。
    （`Criteria.year_tolerance` は築年の軸であって調査時点ではない。混同すると築年フィルタが壊れる）
    """
    years = [c.survey_year for c in cases if c.survey_year]
    return (min(years), max(years)) if years else None


@dataclass
class RentAssessment:
    """賃料判定の表示用まとめ。`Valuation`（統計の結果）は無加工で抱える。"""

    valuation: Valuation
    property_kind: str
    exclusive_area: float
    has_target: bool
    target_monthly: int | None
    median_monthly: int | None
    low_monthly: int | None  # Q1 × 面積
    high_monthly: int | None  # Q3 × 面積
    survey_year_from: int | None
    survey_year_to: int | None
    shortfall: int  # 判定できるまであと何件必要か（0 なら不足していない）
    cases: list[RentCase]

    @property
    def judgeable(self) -> bool:
        return self.valuation.judgeable

    @property
    def ratio_percent(self) -> float | None:
        """想定賃料の入力が無いときは比較しない（番人の値で出た数字を絶対に見せない）。"""
        return self.valuation.ratio_percent if self.has_target else None

    @property
    def band(self) -> str | None:
        return self.valuation.band if self.has_target else None


def build_assessment(
    valuation: Valuation,
    *,
    property_kind: str,
    exclusive_area: float,
    target_monthly: float | None,
) -> RentAssessment:
    """判定結果 → 月額に戻した表示用オブジェクト。"""
    cases = rent_cases_of(valuation)
    years = survey_year_range(cases)

    def monthly(unit_price: float | None) -> int | None:
        return round(unit_price * exclusive_area) if unit_price else None

    shortfall = max(0, MIN_CASES_JUDGEABLE - valuation.case_count) if not valuation.judgeable else 0

    return RentAssessment(
        valuation=valuation,
        property_kind=property_kind,
        exclusive_area=exclusive_area,
        has_target=target_monthly is not None,
        target_monthly=round(target_monthly) if target_monthly else None,
        median_monthly=monthly(valuation.median_unit_price),
        low_monthly=monthly(valuation.q1),
        high_monthly=monthly(valuation.q3),
        survey_year_from=years[0] if years else None,
        survey_year_to=years[1] if years else None,
        shortfall=shortfall,
        cases=cases,
    )


def shortage_message(shortfall: int) -> str:
    """判定不能を黙って突き返さない。薄い母集団を埋める唯一の現実的な経路を示す。"""
    if shortfall <= 0:
        return ""
    return f"あと {shortfall} 件登録すると判定できます（判定には最低 {MIN_CASES_JUDGEABLE} 件、信頼度Aには {MIN_CASES} 件必要です）"
