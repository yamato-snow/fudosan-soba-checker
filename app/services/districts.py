"""地区リングによる近傍抽出（座標を使わない）。

Phase 0 で XPT001 の座標が数km級のクラスタ点と判明したため、距離ではなく地区で近傍を定義する:
  Ring0 = 同一地区（対象物件の DistrictCode）
  Ring1 = 近隣地区（env NEIGHBOR_DISTRICTS = 生活圏の DistrictCode リスト。
          未設定なら「地価が似ている同一市内の地区」を自動推定する）
  Ring2 = 基準市全域（TARGET_CITIES の先頭）
  Ring3 = 対象市全域（TARGET_CITIES すべて）
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TYPE_CONDO, TYPE_HOUSE, TYPE_LAND
from app.models.transaction import Transaction
from app.services.valuation import Case, Criteria

# --- Ring1 自動推定（座標が使えない環境での「近い」の実用的な代理＝地価水準が近い） ---
# 対象地区の宅地(土地)㎡単価中央値の ±N% に収まる同一市内の地区を Ring1 とみなす
NEIGHBOR_PRICE_TOLERANCE = 0.25
# 中央値が安定する最低件数。対象地区・候補地区の双方に課す
MIN_DISTRICT_CASES_FOR_INFERENCE = 3

RING1_SOURCE_MANUAL = "手動設定"
RING1_SOURCE_AUTO = "自動推定"
RING1_SOURCE_NONE = "なし"


@dataclass(frozen=True)
class RingContext:
    """リングを解決するための入力（どの地区・どの市を基準にするか）。"""

    district_code: str
    home_city: str
    target_cities: tuple[str, ...]
    neighbor_districts: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings, district_code: str) -> "RingContext":
        return cls(
            district_code=district_code,
            home_city=settings.home_city,
            target_cities=tuple(settings.target_city_list),
            neighbor_districts=tuple(settings.neighbor_district_list),
        )


@dataclass(frozen=True)
class TargetProperty:
    """判定対象の物件条件。"""

    land_type: str
    district_code: str
    area: float
    total_floor_area: float | None = None
    building_year: int | None = None


def _ring_filter(ctx: RingContext, ring: int, model=Transaction):
    """指定リング「まで」を含む WHERE 条件。

    `model` は `district_code` / `municipality_code` を同じ体系で持つテーブルなら何でもよい
    （賃料事例 `RentListing` も同じ列名にしてある）。Ring2 の OR ロジックを2箇所に分けると
    片方だけ直す壊れ方をするので、複製せずモデルだけ差し替える。
    """
    if ring <= 0:
        return model.district_code == ctx.district_code
    if ring == 1:
        codes = {ctx.district_code, *ctx.neighbor_districts}
        return model.district_code.in_(sorted(codes))
    if ring == 2:
        # 基準市全域（Ring0/1 の地区は基準市外にもありうるので明示的に OR で足す）
        codes = {ctx.district_code, *ctx.neighbor_districts}
        return or_(
            model.municipality_code == ctx.home_city,
            model.district_code.in_(sorted(codes)),
        )
    return model.municipality_code.in_(sorted(set(ctx.target_cities)))


def build_case_query(ctx: RingContext, target: TargetProperty, criteria: Criteria) -> Select:
    """条件に合う事例を引くクエリ。"""
    stmt = select(Transaction).where(
        _ring_filter(ctx, criteria.ring),
        Transaction.land_type == target.land_type,
        Transaction.unit_price.is_not(None),
        Transaction.unit_price > 0,
    )

    # 面積で絞る（土地・戸建=土地面積で敷地の規模感を揃える / 区分=Area が専有面積）
    if target.area and target.area > 0:
        low = target.area * (1 - criteria.area_tolerance)
        high = target.area * (1 + criteria.area_tolerance)
        stmt = stmt.where(Transaction.area >= low, Transaction.area <= high)

    # 延床は戸建のみ。主指標が延床単価である以上「比べる軸」と「絞る軸」を一致させる
    # （これが無いと延床40㎡と180㎡が同じ母集団に入り、㎡単価が桁違いに暴れる）。
    # 区分は TotalFloorArea が実データで全件空なので**適用してはいけない**（必ず0件になる）。
    # 区分の「比べる軸と絞る軸の一致」は上の面積フィルタ（=専有面積）が既に担っている
    if (
        target.land_type == TYPE_HOUSE
        and target.total_floor_area
        and criteria.floor_tolerance is not None
    ):
        low_floor = target.total_floor_area * (1 - criteria.floor_tolerance)
        high_floor = target.total_floor_area * (1 + criteria.floor_tolerance)
        stmt = stmt.where(
            Transaction.total_floor_area.is_not(None),
            Transaction.total_floor_area >= low_floor,
            Transaction.total_floor_area <= high_floor,
        )

    # 築年は建物がある種別のみ（土地に築年は無い）。区分は BuildingYear が実データで全件充足
    if (
        target.land_type in (TYPE_HOUSE, TYPE_CONDO)
        and target.building_year
        and criteria.year_tolerance is not None
    ):
        low_year = target.building_year - criteria.year_tolerance
        high_year = target.building_year + criteria.year_tolerance
        stmt = stmt.where(
            Transaction.building_year.is_not(None),
            Transaction.building_year >= low_year,
            Transaction.building_year <= high_year,
        )

    return stmt.order_by(Transaction.period_year.desc(), Transaction.period_quarter.desc())


async def infer_neighbor_districts(session: AsyncSession, district_code: str) -> list[str]:
    """Ring1 の自動推定: 同一市内で宅地(土地)㎡単価の中央値が対象地区に近い地区。

    座標が使えないので「近い」の代理として地価水準を使う。取込済みデータだけで計算でき、
    集計は1クエリ（全体でも数千件規模の SQLite なのでキャッシュは持たない＝YAGNI）。
    対象地区・候補地区とも土地レコードが MIN_DISTRICT_CASES_FOR_INFERENCE 件以上ないと推定しない。
    """
    if len(district_code) < 5:
        return []
    municipality_code = district_code[:5]

    stmt = select(Transaction.district_code, Transaction.unit_price).where(
        Transaction.municipality_code == municipality_code,
        Transaction.land_type == TYPE_LAND,
        Transaction.district_code != "",
        Transaction.unit_price.is_not(None),
        Transaction.unit_price > 0,
    )
    by_district: dict[str, list[float]] = {}
    for code, unit_price in (await session.execute(stmt)).all():
        by_district.setdefault(code, []).append(float(unit_price))

    base_prices = by_district.get(district_code, [])
    if len(base_prices) < MIN_DISTRICT_CASES_FOR_INFERENCE:
        return []
    base_median = statistics.median(base_prices)
    if base_median <= 0:
        return []
    low = base_median * (1 - NEIGHBOR_PRICE_TOLERANCE)
    high = base_median * (1 + NEIGHBOR_PRICE_TOLERANCE)

    return sorted(
        code
        for code, prices in by_district.items()
        if code != district_code
        and len(prices) >= MIN_DISTRICT_CASES_FOR_INFERENCE
        and low <= statistics.median(prices) <= high
    )


async def resolve_ring1(session: AsyncSession, ctx: RingContext) -> tuple[RingContext, str]:
    """Ring1 を確定する。手動設定（env）が常に勝ち、無ければ自動推定に落ちる。

    戻り値は (Ring1 を埋めた RingContext, 由来ラベル)。由来は判定結果に表示する。
    """
    if ctx.neighbor_districts:
        return ctx, RING1_SOURCE_MANUAL
    inferred = await infer_neighbor_districts(session, ctx.district_code)
    if not inferred:
        return ctx, RING1_SOURCE_NONE
    return replace(ctx, neighbor_districts=tuple(inferred)), RING1_SOURCE_AUTO


def to_case(row: Transaction, ring: int) -> Case:
    return Case(
        unit_price=float(row.unit_price or 0),
        district_name=row.district_name,
        land_type=row.land_type,
        trade_price=row.trade_price,
        area=row.area,
        total_floor_area=row.total_floor_area,
        building_year=row.building_year,
        period_year=row.period_year,
        period_quarter=row.period_quarter,
        price_category=row.price_category,
        ring=ring,
        land_unit_price=row.land_unit_price,
    )


async def fetch_cases(
    session: AsyncSession,
    ctx: RingContext,
    target: TargetProperty,
    criteria: Criteria,
) -> list[Case]:
    result = await session.execute(build_case_query(ctx, target, criteria))
    return [to_case(row, criteria.ring) for row in result.scalars().all()]


@dataclass(frozen=True)
class DistrictOption:
    """フォームのプルダウン用。"""

    code: str
    name: str
    municipality: str
    count: int

    @property
    def label(self) -> str:
        return f"{self.municipality} {self.name}（{self.count}件）"


async def list_districts(session: AsyncSession) -> list[DistrictOption]:
    """取込済みデータに存在する地区の一覧（件数つき）。"""
    stmt = (
        select(
            Transaction.district_code,
            Transaction.district_name,
            Transaction.municipality,
            func.count(Transaction.id),
        )
        .where(Transaction.district_code != "")
        .group_by(Transaction.district_code, Transaction.district_name, Transaction.municipality)
        .order_by(Transaction.municipality, Transaction.district_name)
    )
    result = await session.execute(stmt)
    return [
        DistrictOption(code=code, name=name, municipality=muni, count=count)
        for code, name, muni, count in result.all()
    ]
