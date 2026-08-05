"""割安判定の純粋ロジック（DB 非依存＝テスト容易）。

Lv1（MVP）の判定:
  - 事例の㎡単価の中央値を「相場」とし、対象物件の㎡単価との乖離（%）を主表示
  - 同じ標本の四分位で 3段バンド（Q1未満=割安 / Q1〜Q3=適正 / Q3超=割高）を補助表示
  - 事例が MIN_CASES 未満なら抽出条件を段階拡張し、信頼度を下げる。0〜2件は判定不能

判定精度改善（hantei-seido plan）で足したもの:
  - 比べる軸と絞る軸の一致: 戸建は主指標が延床単価なので、抽出条件にも延床を入れる
    （種別別の EXPANSION_STAGES。延床は ±50% を下限として撤廃しない）
  - IQR トリム: 「古家付き土地」相当の外れ値で分布の裾が伸び、何を入れても「適正」になる
    のを防ぐ。n>=6 のときだけ適用し、閾値判定もトリム後の件数で行う
  - ばらつきガード: トリム後でも Q3/Q1 が大きい標本は信頼度を1段下げて正直に返す
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field

from app.constants import (
    BAND_CHEAP,
    BAND_EXPENSIVE,
    BAND_FAIR,
    CONFIDENCE_A,
    CONFIDENCE_B,
    CONFIDENCE_C,
    CONFIDENCE_NONE,
    CONFIDENCE_NOTES,
    MIN_CASES,
    MIN_CASES_JUDGEABLE,
    RING_LABELS,
    TYPE_CONDO,
    TYPE_HOUSE,
    TYPE_LAND,
)

# --- 外れ値トリムとばらつきガードの定数（env 化しない＝判定の芯は動かさない） ---
# IQR フェンスの係数。標準的な箱ひげ図の外れ値定義
IQR_FACTOR = 1.5
# これ未満の標本ではトリムしない（四分位フェンス自体が不安定なため）
MIN_CASES_FOR_TRIM = 6
# Q3/Q1 がこれを超える標本は「バンドが機能していない」とみなし信頼度を1段下げる
# （実測: 壊れていたケース 2.2倍 / まともなケース 1.4倍 → 中間を初期値に）
DISPERSION_RATIO_THRESHOLD = 1.8
DISPERSION_NOTE = "事例のばらつきが大きく（Q3/Q1 > 1.8）バンドが広いため信頼度を1段下げています"


@dataclass(frozen=True)
class Criteria:
    """抽出条件（1ステージぶん）。"""

    ring: int  # この値までのリングを対象にする（0=同一地区 … 3=対象市全域）
    year_tolerance: int | None  # 築年±N年。None=無制限
    area_tolerance: float  # 面積±N%（0.3 = ±30%）。区分では専有面積に効く
    floor_tolerance: float | None = None  # 延床±N%。None=条件なし（土地で使用）
    # `area_tolerance` が何の面積かの表示名。区分は Area 自体が専有面積なので言葉を変える
    # （デフォルト付き＝既存の呼び出しと挙動に影響しない）
    area_label: str = "土地面積"

    def relaxations(self, base: "Criteria") -> list[str]:
        """初期条件からどこを緩めたかの説明。"""
        notes: list[str] = []
        if self.ring > base.ring:
            notes.append(f"抽出範囲を{RING_LABELS.get(self.ring, self.ring)}まで拡大")
        if self.year_tolerance != base.year_tolerance:
            if self.year_tolerance is None:
                notes.append("築年の条件を撤廃")
            else:
                notes.append(f"築年を±{self.year_tolerance}年に緩和")
        if self.area_tolerance != base.area_tolerance:
            notes.append(f"{self.area_label}を±{int(self.area_tolerance * 100)}%に緩和")
        if self.floor_tolerance != base.floor_tolerance:
            if self.floor_tolerance is None:
                notes.append("延床の条件を撤廃")
            else:
                notes.append(f"延床を±{int(self.floor_tolerance * 100)}%に緩和")
        return notes


# 段階拡張の順序（土地: 現行どおり延床条件なし）:
#   Ring 0-1 → 2 → 3 → 築年 ±5 → ±10 → 無制限 → 土地面積 ±30 → ±50%
EXPANSION_STAGES_LAND: tuple[Criteria, ...] = (
    Criteria(ring=1, year_tolerance=5, area_tolerance=0.3),
    Criteria(ring=2, year_tolerance=5, area_tolerance=0.3),
    Criteria(ring=3, year_tolerance=5, area_tolerance=0.3),
    Criteria(ring=3, year_tolerance=10, area_tolerance=0.3),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.3),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.5),
)

# 戸建: 主指標が延床単価なので延床でも絞る。緩和順は「リング→築年→延床→築年撤廃→土地」。
# 延床は ±50% が下限で撤廃しない（撤廃すると「延床無条件」という直した壊れ方に戻るため）。
EXPANSION_STAGES_HOUSE: tuple[Criteria, ...] = (
    Criteria(ring=1, year_tolerance=5, area_tolerance=0.3, floor_tolerance=0.3),
    Criteria(ring=2, year_tolerance=5, area_tolerance=0.3, floor_tolerance=0.3),
    Criteria(ring=3, year_tolerance=5, area_tolerance=0.3, floor_tolerance=0.3),
    Criteria(ring=3, year_tolerance=10, area_tolerance=0.3, floor_tolerance=0.3),
    Criteria(ring=3, year_tolerance=10, area_tolerance=0.3, floor_tolerance=0.5),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.3, floor_tolerance=0.5),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.5, floor_tolerance=0.5),
)

# 区分マンション: 主指標が専有面積単価で、絞る軸（Area）と比べる軸が最初から一致している。
# `TotalFloorArea` は実データで全件空なので**延床の段を持たない**（持つと必ず0件になる）。
# 緩和順は「リング→築年→築年撤廃→専有面積」で、戸建から延床の2段を抜いた形。
CONDO_AREA_LABEL = "専有面積"
EXPANSION_STAGES_CONDO: tuple[Criteria, ...] = (
    Criteria(ring=1, year_tolerance=5, area_tolerance=0.3, area_label=CONDO_AREA_LABEL),
    Criteria(ring=2, year_tolerance=5, area_tolerance=0.3, area_label=CONDO_AREA_LABEL),
    Criteria(ring=3, year_tolerance=5, area_tolerance=0.3, area_label=CONDO_AREA_LABEL),
    Criteria(ring=3, year_tolerance=10, area_tolerance=0.3, area_label=CONDO_AREA_LABEL),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.3, area_label=CONDO_AREA_LABEL),
    Criteria(ring=3, year_tolerance=None, area_tolerance=0.5, area_label=CONDO_AREA_LABEL),
)

# 後方互換のエイリアス（土地＝従来の単一 STAGES）
EXPANSION_STAGES: tuple[Criteria, ...] = EXPANSION_STAGES_LAND

_STAGES_BY_TYPE: dict[str, tuple[Criteria, ...]] = {
    TYPE_HOUSE: EXPANSION_STAGES_HOUSE,
    TYPE_CONDO: EXPANSION_STAGES_CONDO,
}


def stages_for(land_type: str) -> tuple[Criteria, ...]:
    """種別に応じた段階拡張の並び。"""
    return _STAGES_BY_TYPE.get(land_type, EXPANSION_STAGES_LAND)


def _base_stage(criteria: Criteria) -> Criteria:
    """緩和内容を説明するときの基準（初期条件）。"""
    if criteria.floor_tolerance is not None:
        return EXPANSION_STAGES_HOUSE[0]
    if criteria.area_label == CONDO_AREA_LABEL:
        return EXPANSION_STAGES_CONDO[0]
    return EXPANSION_STAGES_LAND[0]


@dataclass(frozen=True)
class Case:
    """判定に使う1事例（DB 行でもテスト用のダミーでもよい）。"""

    unit_price: float
    district_name: str = ""
    land_type: str = ""
    trade_price: int = 0
    area: float = 0.0
    total_floor_area: float | None = None
    building_year: int | None = None
    period_year: int = 0
    period_quarter: int = 0
    price_category: str = ""
    ring: int = 0
    land_unit_price: float | None = None  # 従指標: 土地面積あたりの単価


@dataclass
class Valuation:
    """判定結果。件数・最終リング・緩和内容を必ず添える（誠実な出力）。"""

    judgeable: bool
    target_unit_price: float
    median_unit_price: float | None
    q1: float | None
    q3: float | None
    ratio_percent: float | None  # 中央値比（+ = 割高、− = 割安）
    band: str | None
    case_count: int  # トリム後の「実際に使った」件数
    criteria: Criteria
    ring_label: str
    confidence: str
    confidence_note: str
    relaxations: list[str] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)  # トリム後の事例
    message: str = ""
    trimmed_count: int = 0  # IQR トリムで除外した件数
    dispersion_flag: bool = False  # ばらつき大（Q3/Q1 > 閾値）
    median_land_unit_price: float | None = None  # 従指標（参考表示のみ・判定には使わない）
    ring1_source: str = ""  # Ring1 の由来（手動設定 / 自動推定 / なし）。呼び出し側が設定


def quartiles(values: Sequence[float]) -> tuple[float, float]:
    """Q1 / Q3（標本が少なくても落ちない実装）。"""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0], ordered[0]
    # statistics.quantiles は n>=2 を要求する
    q1, _median, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return q1, q3


def iqr_fence(values: Sequence[float]) -> tuple[float, float] | None:
    """[Q1−1.5×IQR, Q3+1.5×IQR]。標本が MIN_CASES_FOR_TRIM 未満なら None（トリムしない）。"""
    if len(values) < MIN_CASES_FOR_TRIM:
        return None
    q1, q3 = quartiles(values)
    iqr = q3 - q1
    return q1 - IQR_FACTOR * iqr, q3 + IQR_FACTOR * iqr


def trim_cases(cases: Sequence[Case]) -> tuple[list[Case], list[Case]]:
    """㎡単価の IQR フェンス外を外れ値として除く。戻り値は (残した事例, 除いた事例)。

    「古家付き土地」相当の極端な単価が分布の裾を伸ばし、Q1〜Q3 バンドが広がって
    何を入れても「適正」になるのを防ぐ第2防壁（第1防壁は延床フィルタ）。
    """
    fence = iqr_fence([c.unit_price for c in cases])
    if fence is None:
        return list(cases), []
    low, high = fence
    kept = [c for c in cases if low <= c.unit_price <= high]
    removed = [c for c in cases if not (low <= c.unit_price <= high)]
    if not kept:  # 理論上起きないが、全滅したらトリムしない方が安全
        return list(cases), []
    return kept, removed


def band_for(target: float, q1: float, q3: float) -> str:
    if target < q1:
        return BAND_CHEAP
    if target <= q3:
        return BAND_FAIR
    return BAND_EXPENSIVE


def _confidence(case_count: int, stage_index: int) -> str:
    if case_count < MIN_CASES_JUDGEABLE:
        return CONFIDENCE_NONE
    if case_count < MIN_CASES:
        return CONFIDENCE_C
    return CONFIDENCE_A if stage_index == 0 else CONFIDENCE_B


def _demote(confidence: str) -> str:
    """ばらつきガードによる1段降格（A→B→C。C はそのまま）。"""
    return {CONFIDENCE_A: CONFIDENCE_B, CONFIDENCE_B: CONFIDENCE_C}.get(confidence, confidence)


def _median_land_unit_price(cases: Sequence[Case]) -> float | None:
    values = [c.land_unit_price for c in cases if c.land_unit_price and c.land_unit_price > 0]
    return statistics.median(values) if values else None


def evaluate(
    cases: Sequence[Case],
    target_unit_price: float,
    criteria: Criteria = EXPANSION_STAGES_LAND[0],
    stage_index: int = 0,
    base: Criteria | None = None,
) -> Valuation:
    """事例リストと対象物件の㎡単価から判定結果を作る（純粋関数）。"""
    if target_unit_price <= 0:
        raise ValueError("対象物件の㎡単価は正の数である必要があります")

    valid = [c for c in cases if c.unit_price and c.unit_price > 0]
    kept, removed = trim_cases(valid)
    unit_prices = [c.unit_price for c in kept]
    count = len(unit_prices)
    trimmed_count = len(removed)
    confidence = _confidence(count, stage_index)
    base = base if base is not None else _base_stage(criteria)
    ring_label = RING_LABELS.get(criteria.ring, str(criteria.ring))

    if confidence == CONFIDENCE_NONE:
        return Valuation(
            judgeable=False,
            target_unit_price=target_unit_price,
            median_unit_price=None,
            q1=None,
            q3=None,
            ratio_percent=None,
            band=None,
            case_count=count,
            criteria=criteria,
            ring_label=ring_label,
            confidence=CONFIDENCE_NONE,
            confidence_note=CONFIDENCE_NOTES[CONFIDENCE_NONE],
            relaxations=criteria.relaxations(base),
            cases=list(kept),
            message=(
                f"条件を最大まで広げても事例が{count}件しかないため判定できません"
                "（地区・種別・面積の条件を見直すか、取込対象の市区町村を増やしてください）"
            ),
            trimmed_count=trimmed_count,
        )

    median = statistics.median(unit_prices)
    q1, q3 = quartiles(unit_prices)
    ratio = (target_unit_price - median) / median * 100

    dispersion = q1 > 0 and (q3 / q1) > DISPERSION_RATIO_THRESHOLD
    if dispersion:
        confidence = _demote(confidence)
    note = CONFIDENCE_NOTES[confidence] + (f"／{DISPERSION_NOTE}" if dispersion else "")

    return Valuation(
        judgeable=True,
        target_unit_price=target_unit_price,
        median_unit_price=median,
        q1=q1,
        q3=q3,
        ratio_percent=ratio,
        band=band_for(target_unit_price, q1, q3),
        case_count=count,
        criteria=criteria,
        ring_label=ring_label,
        confidence=confidence,
        confidence_note=note,
        relaxations=criteria.relaxations(base),
        cases=list(kept),
        trimmed_count=trimmed_count,
        dispersion_flag=dispersion,
        median_land_unit_price=_median_land_unit_price(kept),
    )


def expansion_driver(
    target_unit_price: float,
    stages: Sequence[Criteria] = EXPANSION_STAGES_LAND,
) -> Generator[Criteria, Sequence[Case], Valuation]:
    """段階拡張の駆動ロジック（同期/非同期のどちらからも使える単一実装）。

    `next()` で「次に試す抽出条件」を受け取り、`send(cases)` でその条件の事例を返す。
    MIN_CASES に達した時点、または段階を使い切った時点で StopIteration.value に Valuation が入る。
    件数の判定は**トリム後**の件数で行う（「使える事例5件」を保証する）。
    """
    last: Valuation | None = None
    for index, criteria in enumerate(stages):
        cases = yield criteria
        result = evaluate(
            list(cases),
            target_unit_price,
            criteria=criteria,
            stage_index=index,
            base=stages[0],
        )
        if result.case_count >= MIN_CASES:
            return result
        last = result

    assert last is not None  # stages は必ず1つ以上
    return last


def evaluate_with_expansion(
    fetch: Callable[[Criteria], Sequence[Case]],
    target_unit_price: float,
    stages: Sequence[Criteria] = EXPANSION_STAGES_LAND,
) -> Valuation:
    """事例が MIN_CASES 未満なら段階拡張しながら判定する（同期版）。

    `fetch` に条件→事例リストの関数を渡す（DB でもダミーでもよい）ので、この関数自体は純粋。
    最後まで MIN_CASES に届かなければ、最も広い条件の結果を信頼度を下げて返す。
    """
    driver = expansion_driver(target_unit_price, stages)
    criteria = next(driver)
    while True:
        try:
            criteria = driver.send(fetch(criteria))
        except StopIteration as stop:
            return stop.value


def unit_price_for_target(
    land_type: str,
    listing_price: float,
    area: float,
    total_floor_area: float | None,
) -> float:
    """対象物件の主指標㎡単価。

    土地=掲載価格÷土地面積 / 区分=掲載価格÷専有面積 / 戸建=掲載価格÷延床。
    区分は XIT001 の `Area` 自体が専有面積で `TotalFloorArea` が空なので、土地と同じ式になる
    （実データ確認済み。docs/design.md「区分マンション対応（A-1）」）。
    """
    if listing_price <= 0:
        raise ValueError("掲載価格は正の数で入力してください")
    if land_type == TYPE_LAND:
        if not area or area <= 0:
            raise ValueError("土地は土地面積（㎡）が必要です")
        return listing_price / area
    if land_type == TYPE_CONDO:
        if not area or area <= 0:
            raise ValueError("中古マンションは専有面積（㎡）が必要です")
        return listing_price / area
    if not total_floor_area or total_floor_area <= 0:
        raise ValueError("中古戸建は延床面積（㎡）が必要です（主指標が延床単価のため）")
    return listing_price / total_floor_area
